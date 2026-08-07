#!/usr/bin/env python3
"""HumanPulse follow-up cron gate for Hermes agent mode.

The script never sends a fixed follow-up directly.  Empty stdout means the
cron job skips the model call.  When a stage is due, stdout contains a compact
generation prompt; Hermes' agent produces the visible message using the
original proactive message, previous follow-ups, recent context, and the
active persona.

The two HumanPulse jobs should set ``attach_to_session=True``.  Hermes then
mirrors the generated delivery into the real QQ/WeChat session, so the next
reply can see what the agent actually said.
"""

from __future__ import annotations

import datetime
import json
import re
import sys
import traceback
from pathlib import Path

_SITE = "/usr/local/lib/hermes-agent/venv/lib/python3.11/site-packages"
if _SITE not in sys.path:
    sys.path.insert(0, _SITE)

PROACTIVE_JOB_NAME = "humanpulse-proactive"
FOLLOWUP_JOB_NAME = "humanpulse-followup"
CRON_JOBS = Path.home() / ".hermes" / "cron" / "jobs.json"
CRON_OUTPUT = Path.home() / ".hermes" / "cron" / "output"


def _find_job_id(name: str) -> str | None:
    try:
        if not CRON_JOBS.exists():
            return None
        data = json.loads(CRON_JOBS.read_text(encoding="utf-8"))
        for job in data.get("jobs", []):
            if job.get("name") == name:
                return job.get("id")
    except Exception:
        pass
    return None


def _extract_response(text: str) -> str:
    """Extract the last standalone ``## Response`` section from a cron report."""
    matches = list(re.finditer(r"(?m)^[ \t]*##[ \t]+Response[ \t]*$", text))
    if not matches:
        return text.strip()
    return text[matches[-1].end():].strip()


def _latest_delivered_text(job_id: str) -> tuple[str, float] | None:
    try:
        job_dir = CRON_OUTPUT / job_id
        if not job_dir.exists():
            return None
        files = sorted(job_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
        for path in files:
            text = _extract_response(path.read_text(encoding="utf-8").strip())
            if not text:
                continue
            upper = text.upper()
            if upper in {"[SILENT]", "SILENT", "NO_REPLY", "NO REPLY"} or upper.startswith("[SILENT]"):
                continue
            return text, path.stat().st_mtime
    except Exception:
        pass
    return None


def _record_proactive_if_needed() -> None:
    try:
        from gateway.platforms.humanpulse_bridge import _load_state, record_proactive_sent
    except Exception:
        return
    job_id = _find_job_id(PROACTIVE_JOB_NAME)
    latest = _latest_delivered_text(job_id) if job_id else None
    if not latest:
        return
    text, mtime = latest
    state = _load_state()
    try:
        last_at = state.get("last_proactive_at") or ""
        last_epoch = datetime.datetime.fromisoformat(last_at).timestamp() if last_at else 0.0
    except Exception:
        last_epoch = 0.0
    if mtime > last_epoch + 1:
        record_proactive_sent(text)


def _record_followup_if_needed() -> None:
    try:
        from gateway.platforms.humanpulse_bridge import (
            _load_state,
            record_followup_generated,
        )
    except Exception:
        return
    job_id = _find_job_id(FOLLOWUP_JOB_NAME)
    latest = _latest_delivered_text(job_id) if job_id else None
    if not latest:
        return
    text, mtime = latest
    state = _load_state()
    if state.get("followup", {}).get("status") != "active":
        return
    if mtime <= float(state.get("last_followup_output_mtime") or 0) + 1:
        return
    record_followup_generated(text, mtime=mtime)


def main() -> int:
    try:
        _record_proactive_if_needed()
        _record_followup_if_needed()
        from gateway.platforms.humanpulse_bridge import followup_prompt_for_agent
        prompt = followup_prompt_for_agent()
        if prompt:
            print(prompt, flush=True)
    except Exception:
        print(f"[HumanPulse error] follow-up gate failed:\n{traceback.format_exc()}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
