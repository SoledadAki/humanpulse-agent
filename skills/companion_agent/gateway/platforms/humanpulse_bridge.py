"""Bridge from the Hermes gateway to the humanpulse-agent (companion-agent)
runtime — time awareness, proactive messaging, and follow-up cycles.

Mirrors the pattern of ``base_bubble_bridge.py``: the humanpulse skill lives
in the user's skills directory (not on the gateway's import path), so this
module locates it at runtime, loads ``runtime.py`` + ``state.py`` as isolated
modules, and re-exports the functions the gateway and cron scripts need.  If
the skill is not installed every function degrades to a safe no-op and the
gateway keeps working unchanged.

Host contract (Hermes gateway + Hermes cron):

* ``update_user_activity(history=None)`` — call at the start of every real
  user turn (inside gateway ``run_sync``). It records ``last_user_at``, keeps
  a bounded recent history for proactive generation, and stops any active
  follow-up cycle because the user replied.
* ``build_hidden_time_context(history)`` — render ``build_time_context()``
  output to append to the ephemeral system prompt.  Never shown to the user.
* ``build_proactive_reply_note()`` — when the most recent assistant message
  was a proactive ping that the user has not yet answered, return a short
  hidden note so the model can treat the next user message as a reply to it.
* ``proactive_state_for_agent()`` — used by the proactive cron job's
  data-collection script: returns a context-aware prompt when a proactive
  message is eligible, or an empty string (which makes Hermes cron skip the
  AI call entirely — zero tokens when nothing should be said).
* ``record_proactive_sent(text)`` — called by the cron agent (or the
  follow-up script) after a proactive message has actually been delivered;
  updates ``last_proactive_at`` / count and seeds the follow-up cycle.
* ``followup_tick()`` — portable no-agent fallback: poll and return a due
  stage text, otherwise ``None``.
* ``followup_prompt_for_agent()`` — Hermes agent-mode adapter: poll a due
  stage and return a persona-aware generation prompt.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Skill discovery
# ---------------------------------------------------------------------------

_HOME = Path(os.path.expanduser("~"))
_HERMES_HOME = Path(os.environ.get("HERMES_HOME", _HOME / ".hermes"))

_SKILL_CANDIDATES = (
    _HOME / ".hermes" / "skills" / "companion-agent",
    _HOME / ".hermes" / "skills" / "companion_agent",
    _HOME / ".hermes" / "skills" / "humanpulse-agent",
    _HERMES_HOME / "skills" / "companion-agent",
    _HERMES_HOME / "skills" / "companion_agent",
    _HERMES_HOME / "skills" / "humanpulse-agent",
)

# Shared state lives under the Hermes home so it survives skill updates and
# is per-profile.
STATE_FILE = Path(
    os.environ.get("HUMANPULSE_STATE_FILE", _HERMES_HOME / "humanpulse" / "state.json")
)

_runtime = None
_state_mod = None


def _locate_skill_dir() -> Path | None:
    candidates = list(_SKILL_CANDIDATES)
    profiles = _HERMES_HOME / "profiles"
    try:
        if profiles.is_dir():
            for profile in profiles.iterdir():
                for name in ("companion-agent", "companion_agent", "humanpulse-agent"):
                    candidates.append(profile / "skills" / name)
    except OSError:
        pass
    for candidate in candidates:
        if (candidate / "runtime.py").exists():
            return candidate
    return None


def _load_modules():
    global _runtime, _state_mod
    if _runtime is not None and _state_mod is not None:
        return True
    skill_dir = _locate_skill_dir()
    if skill_dir is None:
        return False
    try:
        import importlib.util

        def _load(name: str) -> object:
            path = skill_dir / f"{name}.py"
            # Use a namespaced module name so dataclass/typing introspection
            # (which resolves cls.__module__ against sys.modules) finds the
            # module; a bare "runtime" would collide with other packages and
            # a mismatched sys.modules key breaks @dataclass.
            mod_name = f"humanpulse_{name}"
            spec = importlib.util.spec_from_file_location(mod_name, path)
            if spec is None or spec.loader is None:
                raise ImportError(f"cannot load {path}")
            module = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = module
            spec.loader.exec_module(module)
            return module

        _runtime = _load("runtime")
        # state.py is optional in the skill; fall back to a tiny inline
        # store when the skill ships without it (runtime-only installs).
        try:
            _state_mod = _load("state")
        except Exception:
            _state_mod = None
        return True
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("humanpulse bridge: failed to load skill runtime: %s", exc)
        _runtime = None
        _state_mod = None
        return False


# ---------------------------------------------------------------------------
# State persistence (inline fallback when skill has no state.py)
# ---------------------------------------------------------------------------

def _default_state() -> dict:
    return {
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
        "quiet_hours_start": "23:00",
        "quiet_hours_end": "08:00",
        "min_idle_minutes": 180,
        "min_interval_minutes": 60,
        "daily_limit": 4,
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


def _load_state() -> dict:
    if _state_mod is not None and hasattr(_state_mod, "load_state"):
        try:
            return _state_mod.load_state() or _default_state()
        except Exception:
            pass
    try:
        if STATE_FILE.exists():
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                merged = _default_state()
                merged.update(data)
                return merged
    except Exception:
        pass
    return _default_state()


def _save_state(state: dict) -> None:
    if _state_mod is not None and hasattr(_state_mod, "save_state"):
        try:
            _state_mod.save_state(state)
            return
        except Exception:
            pass
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("humanpulse bridge: failed to persist state: %s", exc)


def _compact_history(history: list | None) -> list[dict]:
    """Keep enough recent context for proactive generation without growing state forever."""
    result = []
    for item in (history or [])[-12:]:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        if role not in {"user", "owner", "assistant"}:
            continue
        text = " ".join(str(item.get("content") or item.get("text") or "").split())
        if not text:
            continue
        result.append(
            {
                "role": "user" if role in {"user", "owner"} else "assistant",
                "text": text[:320],
                "timestamp": str(item.get("timestamp") or ""),
            }
        )
    return result


def _normalize_proactive_text(value: object) -> str:
    normalizer = getattr(_state_mod, "normalize_proactive_text", None)
    if normalizer is not None:
        try:
            return str(normalizer(value) or "").strip()
        except Exception:
            pass
    text = " ".join(str(value or "").split()).strip()
    if "## Response" in text:
        text = text.split("## Response", 1)[1].strip()
    upper = text.upper()
    if not text or len(text) > 600 or upper.startswith("[SILENT]") or "## SCRIPT OUTPUT" in upper:
        return ""
    return text


# ---------------------------------------------------------------------------
# Public host-facing functions
# ---------------------------------------------------------------------------

def update_user_activity(history: list | None = None) -> None:
    """Record that the user just spoke and cancel any pending follow-up."""
    if not _load_modules():
        return
    try:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        state = _load_state()
        state["last_user_at"] = now
        state["busy"] = False
        compacted = _compact_history(history)
        if compacted:
            state["recent_history"] = compacted
        stop = getattr(_runtime, "stop_followup", None)
        if stop is not None and state.get("followup", {}).get("status") == "active":
            state["followup"] = stop(state["followup"], reason="USER_REPLIED")
        _save_state(state)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("humanpulse update_user_activity failed: %s", exc)


def build_hidden_time_context(history: list | None = None) -> str:
    """Render the hidden temporal context for the model prompt (empty = off)."""
    if not _load_modules():
        return ""
    try:
        builder = getattr(_runtime, "build_time_context", None)
        if builder is None:
            return ""
        messages = []
        for msg in history or []:
            role = msg.get("role")
            if role not in {"user", "assistant"}:
                continue
            messages.append(
                {
                    "role": "assistant" if role == "assistant" else "user",
                    "text": str(msg.get("content") or ""),
                    "timestamp": msg.get("timestamp"),
                }
            )
        return builder(messages).strip()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("humanpulse build_time_context failed: %s", exc)
        return ""


def build_proactive_reply_note() -> str:
    """Hidden note when the user is likely replying to our own proactive ping."""
    if not _load_modules():
        return ""
    try:
        state = _load_state()
        text = str(state.get("last_proactive_text") or "").strip()
        sent_at = state.get("last_proactive_at") or ""
        user_at = state.get("last_user_at") or ""
        if not text or not sent_at:
            return ""
        # The user has not spoken since we pinged them — the next message is
        # very likely a reply to our proactive message.
        if user_at and user_at >= sent_at:
            return ""
        return (
            "[HumanPulse context] 你刚刚主动发了一条消息（内容见上一条 assistant 消息）。"
            "用户接下来这句很可能是针对你那条主动消息的回复，请优先当作回复来接话，"
            "不要当成全新话题。"
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("humanpulse build_proactive_reply_note failed: %s", exc)
        return ""


def proactive_state_for_agent() -> str:
    """Compact status for the proactive cron script. Empty => skip AI call.

    The cron data-collection script prints this; when empty, Hermes cron
    skips the agent entirely (no tokens spent).
    """
    if not _load_modules():
        return ""
    try:
        decider = getattr(_runtime, "decide_proactive", None)
        prompt_builder = getattr(_runtime, "build_proactive_prompt", None)
        policy_type = getattr(_runtime, "ProactivePolicy", None)
        if decider is None or policy_type is None:
            return ""
        state = _load_state()
        policy = policy_type(
            timezone=str(state.get("timezone") or "Asia/Shanghai"),
            quiet_hours_start=str(state.get("quiet_hours_start") or "23:00"),
            quiet_hours_end=str(state.get("quiet_hours_end") or "08:00"),
            min_idle_minutes=int(state.get("min_idle_minutes") or 180),
            min_interval_minutes=int(state.get("min_interval_minutes") or 60),
            daily_limit=int(state.get("daily_limit") or 4),
        )
        decision = decider(state, policy=policy)
        if decision.get("action") != "consider":
            return ""
        if prompt_builder is not None:
            return prompt_builder(
                state,
                policy=policy,
                seed=f"{state.get('last_user_at', '')}|{state.get('proactive_count_today', 0)}",
            )
        # Build temporal context from persisted activity even though cron has
        # no live conversation history object.
        synthetic_history = []
        if state.get("last_user_at"):
            synthetic_history.append(
                {"role": "user", "content": "", "timestamp": state["last_user_at"]}
            )
        if state.get("last_proactive_at"):
            synthetic_history.append(
                {
                    "role": "assistant",
                    "content": state.get("last_proactive_text") or "",
                    "timestamp": state["last_proactive_at"],
                }
            )
        time_ctx = build_hidden_time_context(synthetic_history)
        return (
            "HumanPulse 主动消息判定：可以主动发起一条消息。\n"
            f"{time_ctx}\n"
            "请以角色身份自然地说一句话（1-3 句），不要提定时器/扫描/技能。"
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("humanpulse proactive_state_for_agent failed: %s", exc)
        return ""


def record_proactive_sent(text: str) -> None:
    """Record a delivered proactive message and seed the follow-up cycle."""
    if not _load_modules():
        return
    try:
        now = datetime.now(timezone.utc)
        state = _load_state()
        try:
            zone = ZoneInfo(str(state.get("timezone") or "Asia/Shanghai"))
        except ZoneInfoNotFoundError:
            zone = timezone.utc
        today = now.astimezone(zone).date().isoformat()
        if state.get("today_date") != today:
            state["today_date"] = today
            state["proactive_count_today"] = 0
        clean_text = _normalize_proactive_text(text)
        if not clean_text:
            logger.warning("humanpulse ignored non-message proactive output")
            return
        state["last_proactive_at"] = now.isoformat(timespec="seconds")
        state["last_proactive_text"] = clean_text
        state["proactive_window_date"] = today
        recent = state.get("recent_proactive_messages")
        if not isinstance(recent, list):
            recent = []
        recent.append(
            {
                "text": state["last_proactive_text"],
                "timestamp": state["last_proactive_at"],
                "status": "delivered",
            }
        )
        state["recent_proactive_messages"] = recent[-5:]
        state["proactive_count_today"] = int(state.get("proactive_count_today", 0)) + 1
        starter = getattr(_runtime, "start_followup_cycle", None)
        if starter is not None:
            # Follow-up cadence is tuned for the 5-minute cron tick:
            #   stage1: ~26-36 min after the ping
            #   stage2: ~8-13 min later
            #   stage3: ~4-7 min later (last soft nudge)
            # grace is 8 min so a tick that lands just after due still
            # claims the stage instead of dropping it.
            _FP = getattr(_runtime, "FollowupPolicy", None)
            count_selector = getattr(_runtime, "choose_followup_count", None)
            followup_count = (
                int(count_selector(state, now=now, seed=state["last_proactive_at"]))
                if count_selector is not None
                else 3
            )
            followup_count = max(0, min(3, followup_count))
            policy = _FP(
                enabled=True,
                max_stages=1 + followup_count,
                grace_minutes=8,
                stale_claim_minutes=10,
                intervals_minutes=((26, 36), (8, 13), (4, 7)),
            )
            state["last_followup_count"] = followup_count
            followup = starter(
                [
                    {"bubbles": [state["last_proactive_text"]]},
                    {"bubbles": ["刚说到一半……其实我还有句话想跟你说"]},
                    {"bubbles": ["那个……你是不是在忙呀？"]},
                    {"bubbles": ["好啦不打扰你了，等你忙完记得回来找我"]},
                ][: 1 + followup_count],
                started_at=now,
                cycle_id=f"proactive-{int(now.timestamp())}",
                policy=policy,
            )
            state["followup"] = followup
        _save_state(state)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("humanpulse record_proactive_sent failed: %s", exc)


def followup_tick() -> str | None:
    """Poll follow-up state; return the stage text to deliver or None.

    Portable fallback for hosts without agent-mode cron generation: return a
    due stage verbatim, or ``None`` for silence.
    """
    if not _load_modules():
        return None
    try:
        poll = getattr(_runtime, "poll_followup", None)
        commit = getattr(_runtime, "commit_followup", None)
        if poll is None or commit is None:
            return None
        state = _load_state()
        result = poll(state.get("followup", {}))
        if result.get("status") != "claimed":
            return None
        text = str(result.get("text") or "").strip()
        if not text:
            return None
        committed = commit(
            result["state"], result["claim_id"], delivered=True
        )
        state["followup"] = committed.get("state", result["state"])
        _save_state(state)
        return text
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("humanpulse followup_tick failed: %s", exc)
        return None


def record_followup_generated(text: str, mtime: float | None = None) -> None:
    """Remember a follow-up that the host's agent actually delivered."""
    if not _load_modules():
        return
    try:
        clean_text = _normalize_proactive_text(text)
        if not clean_text:
            return
        state = _load_state()
        recent = state.get("recent_followup_messages")
        if not isinstance(recent, list):
            recent = []
        recent.append({"text": clean_text, "status": "delivered"})
        state["recent_followup_messages"] = recent[-5:]
        if mtime is not None:
            state["last_followup_output_mtime"] = float(mtime)
        _save_state(state)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("humanpulse record_followup_generated failed: %s", exc)


def followup_prompt_for_agent() -> str | None:
    """Claim one due stage and return a persona-aware generation prompt."""
    if not _load_modules():
        return None
    try:
        poll = getattr(_runtime, "poll_followup", None)
        commit = getattr(_runtime, "commit_followup", None)
        if poll is None or commit is None:
            return None
        state = _load_state()
        result = poll(state.get("followup", {}))
        if result.get("status") != "claimed":
            return None
        plan = result["state"].get("stages") or []
        original = _normalize_proactive_text(plan[0] if plan else "")
        fallback = _normalize_proactive_text(result.get("text"))
        if not original or not fallback:
            return None
        committed = commit(result["state"], result["claim_id"], delivered=True)
        state["followup"] = committed.get("state", result["state"])
        _save_state(state)
        stage = int(result.get("stage", 1))
        total = max(1, len(plan) - 1)
        return (
            "HumanPulse 无回复追问生成上下文（仅供生成，不要向用户解释）\n"
            f"原主动消息：{original}\n"
            f"已经发送的追问：{json.dumps((state.get('recent_followup_messages') or [])[-4:], ensure_ascii=False)}\n"
            f"近期聊天：{json.dumps((state.get('recent_history') or [])[-8:], ensure_ascii=False)}\n"
            f"当前是第 {stage}/{total} 次追问；安全递进方向参考：{fallback}\n\n"
            "请严格遵循当前角色的人格、关系距离和表达习惯，生成一条自然的后续消息。"
            "不要默认使用撒娇；可以是关心、轻松玩笑、继续前文、简短提醒、表达想念，"
            "或者自然收尾，具体取决于角色和上下文。不要重复原主动消息或已经发送的追问，"
            "不要提定时器、脚本、技能、模型或沉默时长，不要施压、威胁、制造负罪感，"
            "不要使用自伤或死亡威胁。只输出用户可见的 1-3 句短消息；没有自然内容时输出 [SILENT]。"
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("humanpulse followup_prompt_for_agent failed: %s", exc)
        return None


def followup_has_pending() -> bool:
    """True when a follow-up stage is waiting to be delivered (cron gate)."""
    if not _load_modules():
        return False
    try:
        state = _load_state()
        return state.get("followup", {}).get("status") == "active"
    except Exception:
        return False
