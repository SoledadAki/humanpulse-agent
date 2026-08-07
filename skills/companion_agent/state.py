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

DEFAULT_STATE_FILE = Path(
    os.environ.get("HUMANPULSE_STATE_FILE", Path.home() / ".hermes" / "humanpulse" / "state.json")
)

DEFAULT_STATE: dict = {
    "enabled": True,
    "busy": False,
    "last_user_at": "",
    "last_proactive_at": "",
    "last_proactive_text": "",
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
    return state


def save_state(state: dict) -> None:
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
