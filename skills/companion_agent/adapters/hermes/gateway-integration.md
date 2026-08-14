# Hermes Gateway Integration

The important integration point is the final platform delivery layer, not the
model prompt. If Hermes returns one string containing several sentences, a
single call to `self.send(text)` will always produce one QQ/WeChat message,
even when the string contains newlines.

**Current wiring (Hermes-only repo):** you do NOT edit Hermes by hand. Run
`adapters/hermes/install.py` — it copies the bridge modules into
site-packages, patches `gateway/run.py` (hidden time context + proactive
reply note) and `gateway/platforms/base.py` (bubble delivery), and installs
the cron wiring. The documents below describe the integration points for
reference:

- `references/hermes-gateway-humanpulse-wiring.md` — inbound hidden-context
  injection + six-function host contract
- `references/hermes-gateway-bubble-wiring.md` — outbound bubble delivery
- `references/hermes-pitfalls.md` — 实战踩坑记录（必读）

## The outbound send chain (where a reply becomes a platform message)

```
BasePlatformAdapter._process_message_background()   gateway/platforms/base.py
  └─ text_content send site
       └─ _send_with_bubbles()                      patched in by install.py
            └─ send_human_reply()                   companion-agent skill
                 └─ send_one = _send_with_retry()   base.py (retry wrapper)
                      └─ self.send()                platform adapter
                           ├─ QQ:    qqbot/adapter.py::send()
                           └─ WeChat: weixin.py::send()
```

`send_one` must call the existing platform-aware retry function. Do not call
the raw QQ/WeChat API from the skill, and do not join the bubbles before that
callback. The bridge provides:

- one outbound call per bubble;
- a bounded 0.3–1.0 second pause between bubbles;
- cancellation when `interrupt_event` is set;
- a single-message fallback if the skill is unavailable;
- no changes to credentials, configuration, or unrelated platforms.

The exact Hermes file and method names may change between releases. Keep the
bridge as the stable integration contract and re-run `install.py` after any
`hermes update`.
