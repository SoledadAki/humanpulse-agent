#!/usr/bin/env python3
"""HumanPulse follow-up cron script (no-agent watchdog mode).

Hermes cron contract (no_agent=True): non-empty stdout is delivered verbatim
to the user; empty stdout means silence.

This script does two things on every tick:

1. RECORD — scans the proactive cron job's output directory for a message
   that was delivered AFTER our last recorded proactive ping.  When found
   (and it isn't [SILENT]), calls ``record_proactive_sent()`` to update the
   state and seed the staged follow-up cycle.  This closes the loop between
   the proactive cron agent and the follow-up state machine.

2. POLL — calls ``followup_tick()``.  When a follow-up stage is due, the
   stage text is printed (so the cron system delivers it) and committed;
   otherwise nothing is printed and the tick stays silent.

The proactive job id is resolved from ~/.hermes/cron/jobs.json by name
("humanpulse-proactive"), so it keeps working if the id changes.
"""

import json
import os
import sys
import traceback
from pathlib import Path

_SITE = "/usr/local/lib/hermes-agent/venv/lib/python3.11/site-packages"
if _SITE not in sys.path:
    sys.path.insert(0, _SITE)

PROACTIVE_JOB_NAME = "humanpulse-proactive"
CRON_JOBS = Path.home() / ".hermes" / "cron" / "jobs.json"
CRON_OUTPUT = Path.home() / ".hermes" / "cron" / "output"


def _find_proactive_job_id() -> str | None:
    try:
        if not CRON_JOBS.exists():
            return None
        data = json.loads(CRON_JOBS.read_text(encoding="utf-8"))
        for job in data.get("jobs", []):
            if job.get("name") == PROACTIVE_JOB_NAME:
                return job.get("id")
    except Exception:
        pass
    return None


def _extract_response(text: str) -> str:
    """Pull the actual message out of a cron output report.

    Cron saves the full run record (header + prompt + skill + response) to
    the output .md.  The delivered proactive message is the ``## Response``
    section; anything before it is bookkeeping and must not be treated as
    the message.  Falls back to the whole text when the section marker is
    absent (e.g. legacy/direct writes).
    """
    marker = "## Response"
    idx = text.find(marker)
    if idx == -1:
        return text.strip()
    return text[idx + len(marker):].strip()


def _latest_delivered_proactive_text(job_id: str) -> tuple[str, float] | None:
    """Return (text, mtime) of the newest NON-EMPTY, NON-SILENT output file.

    Proactive cron writes a 0-byte record file on skipped ticks (and the
    cron header may be absent on silent runs).  We must ignore those so a
    skip never overwrites the real proactive message with an empty string.
    """
    try:
        job_dir = CRON_OUTPUT / job_id
        if not job_dir.exists():
            return None
        files = sorted(job_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
        for newest in files:
            raw = newest.read_text(encoding="utf-8").strip()
            text = _extract_response(raw)
            if not text:
                continue
            upper = text.strip().upper()
            if upper in {"[SILENT]", "SILENT", "NO_REPLY", "NO REPLY"} or upper.startswith("[SILENT]"):
                continue
            return text, newest.stat().st_mtime
        return None
    except Exception:
        return None


def _record_if_needed() -> None:
    """If the proactive job delivered a new message, record it + seed follow-ups."""
    try:
        from gateway.platforms.humanpulse_bridge import (
            _load_state,
            record_proactive_sent,
        )
    except Exception:
        return
    job_id = _find_proactive_job_id()
    if not job_id:
        return
    latest = _latest_delivered_proactive_text(job_id)
    if not latest:
        return
    text, mtime = latest
    upper = text.strip().upper()
    if upper in {"[SILENT]", "SILENT", "NO_REPLY", "NO REPLY"} or upper.startswith("[SILENT]"):
        return
    state = _load_state()
    try:
        import datetime
        last_at = state.get("last_proactive_at") or ""
        if last_at:
            last_epoch = datetime.datetime.fromisoformat(last_at).timestamp()
        else:
            last_epoch = 0.0
    except Exception:
        last_epoch = 0.0
    if mtime <= last_epoch + 1:
        return
    record_proactive_sent(text)


def main() -> int:
    try:
        _record_if_needed()
        from gateway.platforms.humanpulse_bridge import followup_tick
        text = followup_tick()
        if text:
            print(text, flush=True)
    except Exception:
        print(f"[HumanPulse error] followup tick failed:\n{traceback.format_exc()}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
