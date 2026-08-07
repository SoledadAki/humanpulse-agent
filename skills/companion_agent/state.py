"""HumanPulse state persistence — framework-neutral, JSON-file based.

The host owns where the state file lives.  By default it uses
``~/.hermes/humanpulse/state.json`` (Hermes home), but any host can point
``HUMANPULSE_STATE_FILE`` at its own location (e.g. astrbot plugin dir).

State shape (all timestamps ISO-8601 UTC):

.. code-block:: python

    {
        "enabled": True,
        "busy": False,
        "last_user_at": "2026-08-07T09:30:00+00:00",
        "last_proactive_at": "2026-08-07T10:15:00+00:00",
        "last_proactive_text": "刚刚突然想到你……",
        "recent_proactive_messages": [{"text": "刚刚突然想到你……", "status": "delivered"}],
        "recent_followup_messages": [],
        "last_followup_output_mtime": 0.0,
        "followup_count": -1,
        "last_followup_count": 2,
        "recent_history": [{"role": "user", "text": "今天有点累"}],
        "conversation_summary": "",
        "memory_text": "",
        "persona_context": "",
        "proactive_level": "normal",
        "proactive_window_date": "2026-08-07",
        "timezone": "Asia/Shanghai",
        "proactive_count_today": 2,
        "today_date": "2026-08-07",
        "followup": {
            "cycle_id": "proactive-1754...",
            "status": "active",          # idle | active | waiting_for_user
            "stages": ["...", "..."],
            "stage_index": 0,
            "next_stage_at": "...",
            "claim_id": "",
            "claim_started_at": "",
            "stop_reason": "",
        },
    }
"""

from __future__ import annotations

import json
import os
from pathlib import Path

MAX_STORED_PROACTIVE_CHARS = 600

DEFAULT_STATE_FILE = Path(
    os.environ.get("HUMANPULSE_STATE_FILE", Path.home() / ".hermes" / "humanpulse" / "state.json")
)

DEFAULT_STATE: dict = {
    "enabled": True,
    "busy": False,
    "last_user_at": "",
    "last_proactive_at": "",
        "last_proactive_text": "",
        "recent_proactive_messages": [],
        "recent_followup_messages": [],
        "last_followup_output_mtime": 0.0,
        "followup_count": -1,
        "last_followup_count": 0,
    "recent_history": [],
    "conversation_summary": "",
    "memory_text": "",
    "persona_context": "",
    "proactive_level": "normal",
    "proactive_window_date": "",
    "timezone": "Asia/Shanghai",
    "proactive_count_today": 0,
    "today_date": "",
    "followup": {
        "cycle_id": "",
        "status": "idle",
        "stages": [],
        "stage_index": 0,
        "next_stage_at": "",
        "claim_id": "",
        "claim_started_at": "",
        "stop_reason": "",
    },
}


def state_path() -> Path:
    return DEFAULT_STATE_FILE


def load_state() -> dict:
    """Load persisted state, merging over defaults so new fields never break."""
    state = json.loads(json.dumps(DEFAULT_STATE))
    try:
        if DEFAULT_STATE_FILE.exists():
            raw = json.loads(DEFAULT_STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                _deep_merge(state, raw)
    except Exception:
        pass
    normalized = _normalize_state(state)
    if normalized != state:
        save_state(normalized)
    return normalized


def save_state(state: dict) -> None:
    state = _normalize_state(state)
    DEFAULT_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = DEFAULT_STATE_FILE.with_suffix(DEFAULT_STATE_FILE.suffix + ".tmp")
    temp.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temp.replace(DEFAULT_STATE_FILE)


def reset_state() -> dict:
    fresh = json.loads(json.dumps(DEFAULT_STATE))
    save_state(fresh)
    return fresh


def _deep_merge(base: dict, override: dict) -> None:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def normalize_proactive_text(value: object) -> str:
    """Accept only a short delivered message, not a cron report or prompt."""
    text = " ".join(str(value or "").split()).strip()
    if "## Response" in text:
        text = text.split("## Response", 1)[1].strip()
    upper = text.upper()
    if (
        not text
        or len(text) > MAX_STORED_PROACTIVE_CHARS
        or upper in {"[SILENT]", "SILENT", "NO_REPLY", "NO REPLY"}
        or upper.startswith("[SILENT]")
        or text.startswith("#")
        or "## SCRIPT OUTPUT" in upper
    ):
        return ""
    return text


def _default_followup() -> dict:
    return json.loads(json.dumps(DEFAULT_STATE["followup"]))


def _normalize_followup(value: object) -> dict:
    followup = value if isinstance(value, dict) else {}
    # Repair the old commit envelope: {"status": ..., "state": {...}}.
    for _ in range(2):
        nested = followup.get("state") if isinstance(followup, dict) else None
        if not isinstance(nested, dict):
            break
        followup = nested
    normalized = _default_followup()
    normalized.update(followup)
    normalized.pop("state", None)
    return normalized


def _normalize_state(state: dict) -> dict:
    normalized = json.loads(json.dumps(state if isinstance(state, dict) else DEFAULT_STATE))
    raw_text = normalized.get("last_proactive_text")
    clean_text = normalize_proactive_text(raw_text)
    normalized["followup"] = _normalize_followup(normalized.get("followup"))

    recent = normalized.get("recent_proactive_messages")
    if not isinstance(recent, list):
        recent = []
    cleaned_recent = []
    for item in recent[-5:]:
        item_text = item.get("text") if isinstance(item, dict) else item
        item_text = normalize_proactive_text(item_text)
        if item_text:
            cleaned_recent.append(
                {"text": item_text, "status": "delivered"}
            )
    normalized["recent_proactive_messages"] = cleaned_recent

    recent_followups = normalized.get("recent_followup_messages")
    if not isinstance(recent_followups, list):
        recent_followups = []
    normalized["recent_followup_messages"] = [
        {"text": clean, "status": "delivered"}
        for item in recent_followups[-5:]
        if (clean := normalize_proactive_text(item.get("text") if isinstance(item, dict) else item))
    ]

    if raw_text and not clean_text:
        normalized["last_proactive_text"] = ""
        normalized["last_proactive_at"] = ""
        normalized["recent_proactive_messages"] = []
        normalized["followup"] = _default_followup()
    else:
        normalized["last_proactive_text"] = clean_text
    return normalized
