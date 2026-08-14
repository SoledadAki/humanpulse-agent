#!/usr/bin/env python3
"""HumanPulse follow-up cron script (agent-gated data-collection mode).

Hermes cron contract (agent mode): the script's stdout is injected into the
agent prompt.  Empty stdout -> Hermes cron SKIPS the AI call entirely
(zero tokens); non-empty stdout -> the agent sees it and generates the
follow-up message, which is then delivered (and mirrored into history when
attach_to_session=True).

What this script does on every tick:

1. RECORD — scans the proactive cron job's output directory for a message
   that was delivered AFTER our last recorded proactive ping.  When found
   (and it isn't [SILENT]), calls ``record_proactive_sent()`` to update the
   state and seed the staged follow-up cycle.

2. GATE — calls ``followup_tick()`` which claims the due stage and advances
   the state machine (optimistic commit — the agent is expected to deliver
   the message this tick).  When a stage is claimed, prints a compact context
   block (the proactive message + which stage this is) so the agent can
   generate a NATURAL follow-up that continues the proactive message.
   When nothing is due, prints nothing (zero tokens).

The proactive job id is resolved from ~/.hermes/cron/jobs.json by name
("humanpulse-proactive"), so it keeps working if the id changes.
"""

import datetime
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

    IMPORTANT: must match the section header on its own line (``^## Response``)
    — a naive ``str.find`` also matches prose inside the loaded skill content
    ("extract only the `## Response` section …") and would return a huge
    chunk of the report instead of the real message.
    """
    import re as _re

    match = _re.search(r"^## Response\s*$", text, flags=_re.MULTILINE)
    if match is None:
        return text.strip()
    return text[match.end():].strip()


def _latest_delivered_proactive_text(job_id: str) -> tuple[str, float] | None:
    """Return (text, mtime) of the newest NON-EMPTY, NON-SILENT output file."""
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


def _find_own_job_id() -> str | None:
    try:
        if not CRON_JOBS.exists():
            return None
        data = json.loads(CRON_JOBS.read_text(encoding="utf-8"))
        for job in data.get("jobs", []):
            if job.get("name") == "humanpulse-followup":
                return job.get("id")
    except Exception:
        pass
    return None


def _previous_followup_texts(limit: int = 3) -> list[str]:
    """Return texts of follow-ups already delivered by this job (newest last).

    Reads our own cron output directory (agent mode writes a full run record
    per tick) and extracts the delivered ``## Response`` message from each.
    The current cycle's own delivery files are included so the NEXT stage can
    continue the conversation rather than restarting it.
    """
    job_id = _find_own_job_id()
    if not job_id:
        return []
    try:
        job_dir = CRON_OUTPUT / job_id
        if not job_dir.exists():
            return []
        files = sorted(job_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
        texts: list[str] = []
        for f in files:
            raw = f.read_text(encoding="utf-8").strip()
            text = _extract_response(raw)
            if not text:
                continue
            upper = text.strip().upper()
            if upper in {"[SILENT]", "SILENT", "NO_REPLY", "NO REPLY"} or upper.startswith("[SILENT]"):
                continue
            if text.startswith("[HumanPulse error]"):
                continue
            texts.append(text)
            if len(texts) >= limit:
                break
        return list(reversed(texts))
    except Exception:
        return []


def main() -> int:
    try:
        _record_if_needed()

        from gateway.platforms.humanpulse_bridge import _load_state, followup_tick

        # followup_tick() claims a due stage (optimistic commit) and returns
        # the template text.  We do NOT print the template: instead we print
        # a context block so the agent generates a natural follow-up that
        # continues the proactive message.  Nothing due -> print nothing ->
        # Hermes cron skips the AI call (zero tokens).
        state = _load_state()
        followup = state.get("followup") or {}
        if followup.get("status") != "active":
            return 0

        result = followup_tick()
        if not result:
            return 0

        proactive_text = (state.get("last_proactive_text") or "").strip()
        stage_index = int(followup.get("stage_index") or 0) + 1
        total = len(followup.get("stages") or [])
        print("HumanPulse 追问判定：可以发出下一条追问。", flush=True)
        if proactive_text:
            print(f"你之前主动发的那条消息是：{proactive_text}", flush=True)
        # Include the already-delivered follow-ups so the next one CONTINUES
        # them instead of restarting the topic (continuity requirement).
        prev = _previous_followup_texts()
        if prev:
            print("你之前已经发过的追问（按时间顺序）：", flush=True)
            for i, t in enumerate(prev, 1):
                print(f"{i}. {t}", flush=True)
        print(
            f"这是第 {stage_index}/{total} 段追问（你还没等到对方的回复）。"
            "请以角色身份自然地说 1-2 句话，要紧接上面你主动消息和之前追问的话题，"
            "像同一个人的连续语气：不要重新开头、不要重复说过的话，"
            "越到后面的追问越带撒娇和焦急（不要提定时器/扫描/技能/HumanPulse）。",
            flush=True,
        )
    except Exception:
        print(f"[HumanPulse error] followup tick failed:\n{traceback.format_exc()}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
