# Hermes Gateway Integration

The important integration point is the final platform delivery layer, not the
model prompt. If Hermes returns one string containing several sentences, a
single call to `self.send(text)` will always produce one QQ/WeChat message,
even when the string contains newlines.

The host should split immediately before delivery:

```python
from skills.companion_agent.adapters.hermes.gateway_bubble_bridge import (
    send_with_fallback,
)


async def _send_with_bubbles(self, text: str, interrupt_event):
    return await send_with_fallback(
        text,
        send_one=self._send_with_retry,
        interrupt_event=interrupt_event,
    )
```

Then the normal background message path should select this method only for QQ
and WeChat, while Telegram, Discord, and other platforms keep the original
single-send path:

```python
if self.platform_name in {"QQBOT", "WEIXIN"}:
    await self._send_with_bubbles(text, interrupt_event)
else:
    await self._send_with_retry(text)
```

`send_one` must call the existing platform-aware retry function. Do not call
the raw QQ/WeChat API from the skill, and do not join the bubbles before that
callback. The bridge provides:

- one outbound call per bubble;
- a bounded 0.3–1.0 second pause between bubbles;
- cancellation when `interrupt_event` is set;
- a single-message fallback if the skill is unavailable;
- no changes to credentials, configuration, or unrelated platforms.

The exact Hermes file and method names may change between releases. Keep this
bridge as the stable integration contract and adapt only the small hook in
`BasePlatformAdapter._process_message_background()`.
