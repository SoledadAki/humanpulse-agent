"""Framework-neutral runtime for natural companion behavior.

The host owns scheduling, delivery, persistence, and cancellation. This module
only turns those host facts into stable context and validates model output.
It intentionally uses the standard library so it can be copied into plugins.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
import hashlib
import json
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


# ``local`` means the host machine's current system timezone. Hosts may still
# pass an explicit IANA name when a profile needs a different timezone.
DEFAULT_TIMEZONE = "local"
DEFAULT_MAX_BUBBLES = 5
DEFAULT_MAX_BUBBLE_CHARS = 180
DEFAULT_MAX_STAGES = 3
DEFAULT_MAX_CONTEXT_MESSAGES = 12
DEFAULT_MAX_CONTEXT_CHARS = 320


@dataclass(frozen=True)
class ProactivePolicy:
    """Guardrails applied before a host asks the model to initiate a chat."""

    min_idle_minutes: int = 180
    min_interval_minutes: int = 60
    daily_limit: int = 4
    quiet_hours_start: str = "23:00"
    quiet_hours_end: str = "08:00"
    timezone: str = DEFAULT_TIMEZONE


@dataclass(frozen=True)
class FollowupPolicy:
    """Timing limits for no-reply proactive follow-ups."""

    enabled: bool = True
    max_stages: int = DEFAULT_MAX_STAGES
    grace_minutes: int = 5
    stale_claim_minutes: int = 10
    intervals_minutes: tuple[tuple[int, int], ...] = (
        (26, 36),
        (8, 13),
        (4, 7),
        (2, 4),
    )


def _local_timezone() -> timezone | ZoneInfo:
    """Return the timezone configured by the operating system."""

    return datetime.now().astimezone().tzinfo or timezone.utc


def _timezone(name: str | None) -> timezone | ZoneInfo:
    """Resolve an explicit IANA timezone or the host's local timezone."""

    value = str(name or DEFAULT_TIMEZONE).strip()
    if not value or value.lower() == DEFAULT_TIMEZONE:
        return _local_timezone()
    try:
        return ZoneInfo(value)
    except ZoneInfoNotFoundError:
        return _local_timezone()


def _timezone_label(name: str | None, zone: timezone | ZoneInfo) -> str:
    if str(name or DEFAULT_TIMEZONE).strip().lower() != DEFAULT_TIMEZONE:
        return str(name)
    return getattr(zone, "key", None) or zone.tzname(datetime.now()) or "local"


def _localize(value: object, zone: timezone | ZoneInfo) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(zone)


def _now(now: datetime | None, zone: timezone | ZoneInfo) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(zone)


def _elapsed(start: datetime | None, end: datetime) -> str:
    if start is None:
        return "暂无"
    seconds = max(0, int((end - start).total_seconds()))
    if seconds < 60:
        return "不到1分钟"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}分钟"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}小时" + (f"{minutes}分钟" if minutes else "")
    days, hours = divmod(hours, 24)
    return f"{days}天" + (f"{hours}小时" if hours else "")


def _period(hour: int) -> str:
    if hour < 5:
        return "凌晨"
    if hour < 7:
        return "清晨"
    if hour < 10:
        return "早上"
    if hour < 12:
        return "上午"
    if hour < 14:
        return "中午"
    if hour < 18:
        return "下午"
    if hour < 23:
        return "晚上"
    return "深夜"


def _quiet_hours(current: time, start: time, end: time) -> bool:
    if start == end:
        return False
    if start < end:
        return start <= current < end
    return current >= start or current < end


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _stable_unit(seed: str) -> float:
    number = int.from_bytes(hashlib.blake2s(seed.encode("utf-8"), digest_size=2).digest(), "big")
    return number / 65535


def _followup_delay_minutes(stage: int, seed: str, policy: FollowupPolicy) -> int:
    ranges = policy.intervals_minutes or ((26, 36),)
    low, high = ranges[min(max(stage - 1, 0), len(ranges) - 1)]
    return round(low + (high - low) * _stable_unit(seed))


def _stage_text(stage: object) -> str:
    if isinstance(stage, dict):
        bubbles = stage.get("bubbles")
        if isinstance(bubbles, list):
            return "\n".join(str(item or "").strip() for item in bubbles if str(item or "").strip())
    return str(stage or "").strip()


def start_followup_cycle(
    stages: list[dict | str],
    *,
    started_at: datetime,
    cycle_id: str = "cycle",
    policy: FollowupPolicy | None = None,
) -> dict:
    """Create persisted state for a staged proactive message cycle."""

    policy = policy or FollowupPolicy()
    current = _now(started_at, timezone.utc)
    limit = max(1, min(11, int(policy.max_stages)))
    plan = [text for item in stages[:limit] if (text := _stage_text(item))]
    state = {
        "cycle_id": str(cycle_id),
        "status": "waiting_for_user",
        "stages": plan,
        "stage_index": 0,
        "next_stage_at": "",
        "claim_id": "",
        "claim_started_at": "",
        "stop_reason": "",
    }
    if policy.enabled and len(plan) > 1:
        state["status"] = "active"
        state["next_stage_at"] = _iso(
            current
            + timedelta(
                minutes=_followup_delay_minutes(1, f"{cycle_id}|1", policy)
            )
        )
    return state


def poll_followup(
    state: dict,
    *,
    now: datetime | None = None,
    policy: FollowupPolicy | None = None,
) -> dict:
    """Claim the next no-reply stage, or report why no stage should be sent."""

    from copy import deepcopy
    policy = policy or FollowupPolicy()
    updated = deepcopy(state)
    if not policy.enabled or updated.get("status") != "active":
        return {"status": "idle", "state": updated}
    current = _now(now, timezone.utc)
    due = _localize(updated.get("next_stage_at"), timezone.utc)
    if due is None or current < due:
        return {"status": "not_due", "state": updated}
    if current - due > timedelta(minutes=max(0, policy.grace_minutes)):
        updated.update(
            status="waiting_for_user",
            next_stage_at="",
            stages=[],
            claim_id="",
            claim_started_at="",
            stop_reason="MISSED_STAGE",
        )
        return {"status": "missed", "state": updated}
    claim_started = _localize(updated.get("claim_started_at"), timezone.utc)
    if updated.get("claim_id") and claim_started and current - claim_started <= timedelta(minutes=max(0, policy.stale_claim_minutes)):
        return {"status": "claimed_elsewhere", "state": updated}
    stage = int(updated.get("stage_index", 0)) + 1
    plan = updated.get("stages") or []
    if stage >= len(plan):
        updated.update(status="waiting_for_user", next_stage_at="", stages=[])
        return {"status": "finished", "state": updated}
    claim_id = f"{updated.get('cycle_id') or 'cycle'}:{stage}"
    updated.update(
        claim_id=claim_id,
        claim_started_at=_iso(current),
    )
    return {
        "status": "claimed",
        "state": updated,
        "claim_id": claim_id,
        "stage": stage,
        "text": str(plan[stage]),
    }


def commit_followup(
    state: dict,
    claim_id: str,
    *,
    delivered: bool,
    now: datetime | None = None,
    policy: FollowupPolicy | None = None,
) -> dict:
    """Commit a claimed stage and schedule the next one after delivery."""

    from copy import deepcopy
    policy = policy or FollowupPolicy()
    updated = deepcopy(state)
    if updated.get("claim_id") != claim_id:
        return {"status": "claim_lost", "state": updated}
    if not delivered:
        updated["claim_id"] = ""
        updated["claim_started_at"] = ""
        return {"status": "delivery_failed", "state": updated}
    current = _now(now, timezone.utc)
    stage = int(str(claim_id).rsplit(":", 1)[-1])
    updated["stage_index"] = stage
    updated["claim_id"] = ""
    updated["claim_started_at"] = ""
    plan = updated.get("stages") or []
    if stage + 1 < len(plan):
        updated["next_stage_at"] = _iso(
            current
            + timedelta(
                minutes=_followup_delay_minutes(
                    stage + 1,
                    f"{updated.get('cycle_id') or 'cycle'}|{stage + 1}",
                    policy,
                )
            )
        )
        updated["status"] = "active"
        return {"status": "committed", "state": updated}
    updated.update(status="waiting_for_user", next_stage_at="", stages=[])
    return {"status": "finished", "state": updated}


def stop_followup(state: dict, reason: str = "USER_REPLIED") -> dict:
    """Stop pending follow-ups when the user replies or the host becomes busy."""

    from copy import deepcopy
    updated = deepcopy(state)
    updated.update(
        status="waiting_for_user",
        next_stage_at="",
        stages=[],
        claim_id="",
        claim_started_at="",
        stop_reason=str(reason or "STOPPED"),
    )
    return updated


def build_time_context(
    messages: list[dict],
    *,
    now: datetime | None = None,
    timezone_name: str = DEFAULT_TIMEZONE,
    personal_landmarks: list[str] | None = None,
) -> str:
    """Build hidden, human-readable temporal context for a model prompt."""

    zone = _timezone(timezone_name)
    current = _now(now, zone)
    previous_user = next(
        (
            _localize(item.get("timestamp"), zone)
            for item in reversed(messages)
            if item.get("role") in {"owner", "user"}
        ),
        None,
    )
    previous_assistant = next(
        (
            _localize(item.get("timestamp"), zone)
            for item in reversed(messages)
            if item.get("role") == "assistant"
        ),
        None,
    )
    crossed_days = (current.date() - previous_user.date()).days if previous_user else 0
    gap = (current - previous_user).total_seconds() if previous_user else None
    if gap is None:
        continuity = "暂无上一轮记录"
    elif gap <= 15 * 60:
        continuity = "连续聊天"
    elif gap <= 2 * 60 * 60:
        continuity = "短暂间隔后继续"
    elif crossed_days == 0:
        continuity = "同一天稍后回来"
    elif crossed_days == 1:
        continuity = "隔了一夜，已经是第二天"
    elif crossed_days < 7:
        continuity = "隔了几天回来"
    else:
        continuity = "隔了较长时间回来"

    weekdays = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")
    landmark_text = "；".join(str(item).strip() for item in (personal_landmarks or []) if str(item).strip())
    return "\n".join(
        (
            f"当前本地时间：{current:%Y-%m-%d %H:%M}，{weekdays[current.weekday()]}，{_timezone_label(timezone_name, zone)}",
            f"当前时段：{_period(current.hour)}",
            f"距离用户上一条消息：{_elapsed(previous_user, current)}",
            f"距离上次完整对话：{_elapsed(previous_assistant, current)}",
            f"日期关系：{'未跨日' if crossed_days <= 0 else f'已跨 {crossed_days} 个自然日'}",
            f"连续性判断：{continuity}",
            f"近期个人日期：{landmark_text or '无明确日期记忆'}",
        )
    )


def split_reply_bubbles(
    text: str,
    *,
    enabled: bool = True,
    max_bubbles: int = DEFAULT_MAX_BUBBLES,
    max_chars: int = DEFAULT_MAX_BUBBLE_CHARS,
) -> list[str]:
    """Split a model reply into bounded chat bubbles without inventing text."""

    clean = str(text or "").strip()
    if not clean:
        return []
    if not enabled:
        return [clean]
    bubble_limit = max(1, min(12, int(max_bubbles)))
    char_limit = max(20, int(max_chars))
    sentences: list[str] = []
    for line in re.split(r"\n+", clean):
        line = line.strip()
        if not line:
            continue
        start = 0
        for match in re.finditer(r"[。！？!?]+[”’）】」』》]*", line):
            sentences.append(line[start : match.end()].strip())
            start = match.end()
        if tail := line[start:].strip():
            sentences.append(tail)

    bubbles: list[str] = []
    for sentence in sentences or [clean]:
        phrase = sentence
        while len(phrase) > char_limit:
            window = phrase[: char_limit + 1]
            cut = max((window.rfind(mark) + 1 for mark in "，,；;：: "), default=0)
            cut = cut if cut >= char_limit // 2 else char_limit
            bubbles.append(phrase[:cut].strip())
            phrase = phrase[cut:].strip()
        if phrase:
            bubbles.append(phrase)
    if len(bubbles) > bubble_limit:
        bubbles = [*bubbles[: bubble_limit - 1], " ".join(bubbles[bubble_limit - 1 :])]
    return bubbles


def decide_proactive(
    state: dict,
    *,
    now: datetime | None = None,
    policy: ProactivePolicy | None = None,
) -> dict:
    """Return a deterministic send/skip decision for the host scheduler."""

    policy = policy or ProactivePolicy()
    zone = _timezone(policy.timezone)
    current = _now(now, zone)
    if not state.get("enabled", True):
        return {"action": "skip", "reason_code": "DISABLED"}
    if state.get("busy"):
        return {"action": "skip", "reason_code": "CONVERSATION_BUSY"}
    try:
        quiet_start = time.fromisoformat(policy.quiet_hours_start)
        quiet_end = time.fromisoformat(policy.quiet_hours_end)
    except ValueError:
        quiet_start, quiet_end = time(23), time(8)
    if _quiet_hours(current.time().replace(tzinfo=None), quiet_start, quiet_end):
        return {"action": "skip", "reason_code": "QUIET_HOURS"}
    count_today = int(state.get("proactive_count_today", 0))
    state_date = str(state.get("today_date") or "")
    if state_date and state_date != current.date().isoformat():
        count_today = 0
    if count_today >= max(0, policy.daily_limit):
        return {"action": "skip", "reason_code": "DAILY_LIMIT"}

    last_user = _localize(state.get("last_user_at"), zone)
    if last_user is not None:
        idle = (current - last_user).total_seconds() / 60
        if idle < max(0, policy.min_idle_minutes):
            return {"action": "skip", "reason_code": "USER_RECENTLY_ACTIVE", "idle_minutes": round(idle, 1)}

    last_proactive = _localize(state.get("last_proactive_at"), zone)
    if last_proactive is not None:
        interval = (current - last_proactive).total_seconds() / 60
        if interval < max(0, policy.min_interval_minutes):
            return {"action": "skip", "reason_code": "PROACTIVE_COOLDOWN", "interval_minutes": round(interval, 1)}
    window_date = str(state.get("proactive_window_date") or "")
    return {
        "action": "consider",
        "reason_code": "ELIGIBLE",
        "period": _period(current.hour),
        "idle_minutes": round(
            max(0, (current - last_user).total_seconds() / 60), 1
        ) if last_user is not None else None,
        "window_opening": window_date != current.date().isoformat(),
    }


def _context_line(value: object, limit: int = DEFAULT_MAX_CONTEXT_CHARS) -> str:
    clean = " ".join(str(value or "").split())
    return clean[: max(40, int(limit))].rstrip()


def _context_history(state: dict) -> list[dict]:
    raw = state.get("recent_history") or []
    if not isinstance(raw, list):
        return []
    result = []
    for item in raw[-DEFAULT_MAX_CONTEXT_MESSAGES:]:
        if not isinstance(item, dict):
            continue
        role = "user" if item.get("role") in {"user", "owner"} else "assistant"
        text = _context_line(item.get("text") or item.get("content"))
        if text:
            result.append({"role": role, "text": text, "timestamp": item.get("timestamp", "")})
    return result


def choose_proactive_angle(
    state: dict,
    *,
    now: datetime | None = None,
    policy: ProactivePolicy | None = None,
    seed: str = "",
) -> str:
    """Choose a model-facing opening angle from time and available context."""

    policy = policy or ProactivePolicy()
    decision = decide_proactive(state, now=now, policy=policy)
    if decision.get("action") != "consider":
        return ""
    if decision.get("window_opening"):
        return (
            f"这是今天当前陪伴时段开启后的第一条消息；结合{decision['period']}自然问候，"
            "使用符合时段的问候，但不要机械报出时间"
        )

    history = _context_history(state)
    latest_user = next((item for item in reversed(history) if item["role"] == "user"), None)
    latest_text = str(latest_user.get("text") or "") if latest_user else ""
    if latest_text and any(mark in latest_text for mark in "？?吗呢呀吧"):
        return "接住用户最近留下的疑问或未展开内容，像自然续上话题，不要重新盘问"

    recent_text = str(state.get("last_proactive_text") or "").strip()
    if recent_text:
        return "避开最近主动消息的主题、开头和句式，从当前时段与人设换一个自然角度"

    options = (
        "分享一个此刻自然冒出的想法或小观察，不假装浏览了不存在的外部内容",
        "从当前人设会感兴趣的话题轻轻开场，并结合近期聊天而不是凭空发问",
        "用一句很短的创意表达或想象片段开场，保持符合人设的语气",
        "因为想起用户而自然冒泡，可以表达想念或陪伴感，但不要制造压力",
    )
    digest = hashlib.blake2s(
        f"{seed}|{decision.get('period')}|{latest_text}|{recent_text}".encode("utf-8"),
        digest_size=2,
    ).digest()
    return options[int.from_bytes(digest, "big") % len(options)]


def choose_followup_count(
    state: dict,
    *,
    now: datetime | None = None,
    seed: str = "",
) -> int:
    """Choose 0-2 follow-ups from persona, timing, and conversational openness."""

    override = state.get("followup_count")
    if isinstance(override, int) and override >= 0:
        return min(2, override)
    level = str(state.get("proactive_level") or "normal").lower()
    ranges = {
        "restrained": (0, 1),
        "normal": (0, 2),
        "clingy": (1, 2),
        "custom": (0, 2),
    }
    low, high = ranges.get(level, ranges["normal"])
    zone = _timezone(str(state.get("timezone") or DEFAULT_TIMEZONE))
    current = _now(now, zone)
    if _period(current.hour) in {"凌晨", "深夜"}:
        high = min(high, 1)
    latest = str(state.get("last_proactive_text") or "")
    history = state.get("recent_history") or []
    latest_user = next(
        (
            str(item.get("text") or item.get("content") or "")
            for item in reversed(history)
            if isinstance(item, dict) and item.get("role") in {"user", "owner"}
        ),
        "",
    )
    if any(mark in latest + latest_user for mark in "？?吗呢呀吧"):
        low = min(high, low + 1)
    if high <= low:
        return low
    digest = hashlib.blake2s(
        f"{seed}|{level}|{current.date().isoformat()}|{latest}|{latest_user}".encode("utf-8"),
        digest_size=2,
    ).digest()
    return low + int.from_bytes(digest, "big") % (high - low + 1)


def build_proactive_prompt(
    state: dict,
    *,
    now: datetime | None = None,
    policy: ProactivePolicy | None = None,
    seed: str = "",
) -> str:
    """Build a compact model prompt for a context-aware proactive opening."""

    policy = policy or ProactivePolicy()
    decision = decide_proactive(state, now=now, policy=policy)
    if decision.get("action") != "consider":
        return ""
    history = _context_history(state)
    history_text = "\n".join(
        f"{item['role']}: {item['text']}" for item in history[-8:]
    ) or "（暂无近期聊天记录）"
    proactive = state.get("recent_proactive_messages") or []
    if not isinstance(proactive, list):
        proactive = []
    proactive_text = "\n".join(
        _context_line(item.get("text") if isinstance(item, dict) else item, 180)
        for item in proactive[-5:]
    ) or _context_line(state.get("last_proactive_text"), 180) or "（暂无）"
    summary = _context_line(state.get("conversation_summary"), 700) or "（暂无较早会话摘要）"
    memory = _context_line(state.get("memory_text"), 900) or "（暂无长期记忆）"
    persona = _context_line(state.get("persona_context"), 500) or "（由宿主人格提示提供）"
    angle = choose_proactive_angle(state, now=now, policy=policy, seed=seed)
    return (
        "HumanPulse 主动消息上下文（仅供生成，不要向用户解释这些字段）\n"
        f"当前时段：{decision.get('period', '当前')}\n"
        f"距用户上次发言：{decision.get('idle_minutes') or '暂无'} 分钟\n"
        f"本轮主动角度：{angle}\n"
        f"主动程度：{_context_line(state.get('proactive_level') or 'normal', 40)}\n"
        f"人格补充：{persona}\n"
        f"较早会话摘要：{summary}\n"
        f"最近聊天：\n{history_text}\n"
        f"最近主动消息（避免重复）：\n{proactive_text}\n"
        f"相关长期记忆：{memory}\n\n"
        "请以角色身份自然发起 1-2 个短气泡；每个气泡只表达一个小意思，"
        "结合本轮角度和真实上下文，"
        "不要提定时器、扫描、技能、沉默时长或系统机制。没有自然内容时输出 [SILENT]。"
    )


def normalize_proactive_response(
    raw: str | dict,
    *,
    max_stages: int = DEFAULT_MAX_STAGES,
    max_bubbles: int = 5,
    max_chars: int = 120,
) -> dict:
    """Validate and normalize model JSON into the portable proactive protocol."""

    candidate = str(raw or "").strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        candidate = "\n".join(lines[1:-1]).strip() if len(lines) >= 3 else ""
    try:
        payload = raw if isinstance(raw, dict) else json.loads(candidate)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"action": "skip", "reason_code": "INVALID_OUTPUT"}
    if not isinstance(payload, dict):
        return {"action": "skip", "reason_code": "INVALID_OUTPUT"}
    if payload.get("action") == "skip":
        return {"action": "skip", "reason_code": str(payload.get("reason_code") or "NO_NATURAL_TOPIC")}

    stages: list[dict] = []
    for item in (payload.get("stages") or [])[: max(1, min(11, int(max_stages)))]:
        if not isinstance(item, dict) or not isinstance(item.get("bubbles"), list):
            continue
        bubbles = [str(value or "").strip()[:max_chars] for value in item["bubbles"][:max_bubbles]]
        bubbles = [value for value in bubbles if value]
        if bubbles:
            stages.append({"bubbles": bubbles})
    if not stages and str(payload.get("message") or "").strip():
        stages = [{"bubbles": [str(payload["message"]).strip()[:max_chars]]}]
    if not stages:
        return {"action": "skip", "reason_code": "INVALID_OUTPUT"}
    return {"action": "send", "stages": stages}
