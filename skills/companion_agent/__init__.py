"""Portable companion-agent behavior primitives."""

from .runtime import (
    FollowupPolicy,
    ProactivePolicy,
    build_time_context,
    commit_followup,
    decide_proactive,
    normalize_proactive_response,
    poll_followup,
    split_reply_bubbles,
    start_followup_cycle,
    stop_followup,
)

__all__ = [
    "ProactivePolicy",
    "FollowupPolicy",
    "build_time_context",
    "commit_followup",
    "decide_proactive",
    "normalize_proactive_response",
    "poll_followup",
    "split_reply_bubbles",
    "start_followup_cycle",
    "stop_followup",
]
