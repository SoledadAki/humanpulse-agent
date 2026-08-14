#!/usr/bin/env python3
"""Verify the companion-agent bubble delivery wiring in the Hermes gateway.

Run from the Hermes site-packages root with the Hermes venv python:

    cd /usr/local/lib/hermes-agent/venv/lib/python3.11/site-packages
    /usr/local/lib/hermes-agent/venv/bin/python3 \
        ~/.hermes/skills/companion-agent/scripts/verify_bubble_delivery.py

Exit code 0 = all checks passed. Non-zero = something regressed
(most likely a `hermes update` wiped the site-packages edits — see
references/hermes-gateway-bubble-wiring.md).
"""

from __future__ import annotations

import asyncio
import sys
import types

SITE_PACKAGES = "/usr/local/lib/hermes-agent/venv/lib/python3.11/site-packages"
if SITE_PACKAGES not in sys.path:
    sys.path.insert(0, SITE_PACKAGES)

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILURES.append(name)


async def main() -> int:
    try:
        from gateway.platforms.base import SendResult, BasePlatformAdapter
        from gateway.config import Platform
    except Exception as exc:  # noqa: BLE001
        check("gateway imports", False, str(exc))
        return 1

    # 1. Bridge loads send_human_reply from the skill dir.
    try:
        from gateway.platforms.base_bubble_bridge import send_human_reply
    except Exception as exc:  # noqa: BLE001
        send_human_reply = None
        check("base_bubble_bridge import", False, str(exc))
    check("bridge exposes send_human_reply", callable(send_human_reply))

    # 2. base.py has the wired method.
    check(
        "_send_with_bubbles on BasePlatformAdapter",
        hasattr(BasePlatformAdapter, "_send_with_bubbles"),
    )

    class FakeAdapter:
        """Minimal stand-in: real code looks up _send_with_retry dynamically."""

        name = "fake-qq"
        platform = Platform.QQBOT
        _pending_messages = {}

        def __init__(self) -> None:
            self.sent: list[str] = []
            self._send_with_bubbles = BasePlatformAdapter._send_with_bubbles.__get__(
                self, FakeAdapter
            )

        async def _send_with_retry(self, *, chat_id, content, reply_to=None, metadata=None):
            self.sent.append(content)
            return SendResult(success=True, message_id=f"msg-{len(self.sent)}")

    ev = types.SimpleNamespace(source=types.SimpleNamespace(chat_id="qq-user-1"))

    # 3. Multi-paragraph text -> independent bubbles (never one joined string).
    a = FakeAdapter()
    await a._send_with_bubbles(
        event=ev, session_key="k1", text="第一句。第二句！\n\n第三段？",
        reply_to=None, metadata={}, interrupt_event=asyncio.Event(),
    )
    check("QQ splits into >1 bubble", len(a.sent) >= 2, f"sent={a.sent!r}")
    check("bubbles are not joined", all(len(b) < 12 for b in a.sent), f"sent={a.sent!r}")

    # 4. New user message interrupts -> remaining bubbles cancelled.
    a2 = FakeAdapter()
    interrupt = asyncio.Event()
    orig = a2._send_with_retry
    count = {"n": 0}

    async def _retry_with_interrupt(**kw):
        count["n"] += 1
        if count["n"] == 1:
            interrupt.set()  # simulate user speaking after first bubble
        return await orig(**kw)

    a2._send_with_retry = _retry_with_interrupt
    await a2._send_with_bubbles(
        event=ev, session_key="k2", text="第一段。第二段。第三段。第四段。",
        reply_to=None, metadata={}, interrupt_event=interrupt,
    )
    check("cancel-on-interrupt stops after 1", len(a2.sent) == 1, f"sent={a2.sent!r}")

    # 5. Non-QQ/WeChat platforms keep single-send behavior.
    class FakeTelegram(FakeAdapter):
        platform = Platform.TELEGRAM

    tg = FakeTelegram()
    await tg._send_with_bubbles(
        event=ev, session_key="k3", text="你好呀！今天过得怎么样？\n\n我给你讲个故事……",
        reply_to=None, metadata={}, interrupt_event=asyncio.Event(),
    )
    check("Telegram keeps single-send", len(tg.sent) == 1, f"sent={tg.sent!r}")

    # 6. WeChat also enabled.
    class FakeWeixin(FakeAdapter):
        platform = Platform.WEIXIN

    wx = FakeWeixin()
    await wx._send_with_bubbles(
        event=ev, session_key="k4", text="第一条。第二条！",
        reply_to=None, metadata={}, interrupt_event=asyncio.Event(),
    )
    check("WeChat splits too", len(wx.sent) == 2, f"sent={wx.sent!r}")

    return 0 if not FAILURES else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
