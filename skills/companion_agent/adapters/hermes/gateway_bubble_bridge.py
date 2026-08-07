"""Gateway-facing bridge for Hermes platform adapters.

Hermes owns the final platform send function. This module keeps the HumanPulse
integration at that boundary instead of changing model prompts or platform
adapters directly.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import asyncio

try:
    from .send_bubbles import send_human_reply
except ImportError:  # copied as a flat bridge module into a host package
    from send_bubbles import send_human_reply


SendOne = Callable[[str], Awaitable[str | None]]


async def send_with_bubbles(
    text: str,
    send_one: SendOne,
    *,
    interrupt_event: asyncio.Event | None = None,
    enabled: bool = True,
    max_bubbles: int = 5,
    max_chars: int = 180,
) -> dict:
    """Send a response as independent platform messages."""

    return await send_human_reply(
        text,
        send_one,
        enabled=enabled,
        max_bubbles=max_bubbles,
        max_chars=max_chars,
        is_cancelled=interrupt_event.is_set if interrupt_event is not None else None,
    )


async def send_with_fallback(
    text: str,
    send_one: SendOne,
    *,
    interrupt_event: asyncio.Event | None = None,
    enabled: bool = True,
    max_bubbles: int = 5,
    max_chars: int = 180,
) -> dict:
    """Use bubble delivery when available and preserve the host's old path otherwise."""

    try:
        return await send_with_bubbles(
            text,
            send_one,
            interrupt_event=interrupt_event,
            enabled=enabled,
            max_bubbles=max_bubbles,
            max_chars=max_chars,
        )
    except (ImportError, ModuleNotFoundError):
        if interrupt_event is not None and interrupt_event.is_set():
            return {"status": "cancelled", "sent": [], "remaining": [text]}
        message_id = await send_one(text)
        return {"status": "fallback", "sent": [text], "remaining": [], "message_id": message_id}
