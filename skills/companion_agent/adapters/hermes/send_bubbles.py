"""Reference sender for Hermes-like hosts.

The host supplies the actual QQ/WeChat send function. One call to
``send_human_reply`` produces multiple platform messages when the text needs
segmentation; it never joins the bubbles back into one outbound payload.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
import sys

try:
    from skills.companion_agent.runtime import split_reply_bubbles
except ImportError:  # copied beside runtime.py into a host skill directory
    try:
        from runtime import split_reply_bubbles
    except ImportError:
        skill_root = Path(__file__).resolve().parents[2]
        if str(skill_root) not in sys.path:
            sys.path.insert(0, str(skill_root))
        from runtime import split_reply_bubbles


SendOne = Callable[[str], Awaitable[str | None]]
IsCancelled = Callable[[], bool]


def typing_delay_seconds(text: str) -> float:
    """Keep a short, bounded pause between independently sent bubbles."""

    return min(1.0, max(0.3, 0.15 + len(text) * 0.03))


async def send_human_reply(
    text: str,
    send_one: SendOne,
    *,
    enabled: bool = True,
    max_bubbles: int = 5,
    max_chars: int = 180,
    is_cancelled: IsCancelled | None = None,
) -> dict:
    """Send each bubble separately through the host's platform adapter."""

    bubbles = split_reply_bubbles(
        text,
        enabled=enabled,
        max_bubbles=max_bubbles,
        max_chars=max_chars,
    )
    sent: list[str] = []
    for index, bubble in enumerate(bubbles):
        if is_cancelled is not None and is_cancelled():
            return {"status": "cancelled", "sent": sent, "remaining": bubbles[index:]}
        if index:
            await asyncio.sleep(typing_delay_seconds(bubble))
        await send_one(bubble)
        sent.append(bubble)
    return {"status": "sent", "sent": sent, "remaining": []}
