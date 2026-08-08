"""Independent bubble delivery for Hermes cron messages.

Hermes gateway replies and cron deliveries use different send paths. Cron
hosts should call ``send_cron_reply`` from their final platform delivery point
so HumanPulse proactive and follow-up output gets the same short bubbles as a
normal reply.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

try:
    from gateway.platforms.base_bubble_bridge import send_human_reply
except Exception:  # pragma: no cover - defensive
    send_human_reply = None


async def send_cron_reply(
    text: str,
    *,
    send_one: Callable[[str], Awaitable[object]],
    is_cancelled: Callable[[], bool] | None = None,
) -> dict:
    """Split a cron result into independently delivered bubbles."""
    if send_human_reply is None:
        await send_one(text)
        return {"status": "sent", "sent": [text], "remaining": []}
    return await send_human_reply(
        text,
        send_one=send_one,
        is_cancelled=is_cancelled or (lambda: False),
    )
