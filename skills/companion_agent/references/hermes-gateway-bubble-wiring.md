# Hermes gateway (QQ/WeChat) bubble delivery — wiring detail

Wired 2026-08-07. Problem: QQ and WeChat merged model-generated
multi-paragraph replies into a single platform message. Fix: route the
platform's real outbound send through `send_human_reply()` once per bubble.

## The outbound send chain (where a reply becomes a platform message)

```
BasePlatformAdapter._process_message_background()   gateway/platforms/base.py
  └─ text_content send site (~line 5112)
       └─ _send_with_bubbles()                       NEW method (base.py)
            └─ send_human_reply()                    companion-agent skill
                 └─ send_one = _send_with_retry()    base.py (retry wrapper)
                      └─ self.send()                 platform adapter
                           ├─ QQ:    qqbot/adapter.py::send()
                           │          → _send_c2c_text / _send_group_text / _send_guild_text
                           └─ WeChat: weixin.py::send()
```

- QQ adapter's `send()` also calls `truncate_message()` but that only splits
  by LENGTH (MAX_MESSAGE_LENGTH), not by semantic bubbles — so multi-paragraph
  replies still merged. `split_reply_bubbles()` fixes the semantics.

## Files changed

| File | Change |
|------|--------|
| `~/.hermes/skills/companion-agent/adapters/hermes/send_bubbles.py` | Added (was missing locally; project's reference bridge) |
| `~/.hermes/skills/companion-agent/adapters/hermes/README.md` | Updated to match upstream (QQ/WeChat segmented delivery section) |
| `<site-packages>/gateway/platforms/base.py` | Added `_send_with_bubbles()`; text send site now calls it |
| `<site-packages>/gateway/platforms/base_bubble_bridge.py` | NEW bridge: loads `send_human_reply` from skill dir |

site-packages = `/usr/local/lib/hermes-agent/venv/lib/python3.11/site-packages/`

## Why the bridge module exists

The gateway runs from site-packages; `~/.hermes/skills/` is NOT on
`sys.path`, so `base.py` cannot `from skills.companion_agent...`.
`base_bubble_bridge.py` locates the skill dir (default + profile layouts),
loads `runtime.py` + `adapters/hermes/send_bubbles.py` via
`importlib.util.spec_from_file_location`, registers `runtime` under the name
`send_bubbles.py` expects, and re-exports `send_human_reply`. If the skill is
missing, `send_human_reply` stays `None` and the gateway falls back to the
original single-send path.

## Design decisions (do not regress)

1. **Platform-scoped**: `_process_message_background` is the shared path for
   ALL platforms. Bubble splitting is gated to
   `Platform.QQBOT` / `Platform.WEIXIN` — Telegram/Discord/etc. keep
   single-send behavior.
2. **Never join bubbles**: `send_human_reply()` calls `send_one()` per bubble;
   each bubble hits `_send_with_retry` → `self.send()` independently.
3. **Typing pause**: `typing_delay_seconds()` = 0.3–1.0s bounded gap between
   bubbles.
4. **Cancel on new user message**: `is_cancelled` checks
   `interrupt_event.is_set()` (set by `interrupt_session_activity()`, called
   from `run.py::_interrupt_and_clear_session` when a new inbound message
   interrupts) plus `session_key in self._pending_messages` (dict).
5. **No config/credential changes**: wiring is code-only.
6. `SendResult(success=True)` is returned even when bubbles were cancelled —
   cancellation is a normal stop, not a delivery failure. `message_id` stays
   None, which is fine because the ephemeral-delete path only triggers for
   system notices with `_ephemeral_ttl > 0`.

## Verification

Run `scripts/verify_bubble_delivery.py` from the site-packages dir with the
Hermes venv python. It exercises: multi-bubble split, per-bubble send
independence, typing gaps, cancel-on-interrupt, Telegram single-send fallback,
and skill-missing fallback.

## Gotcha: hermes update wipes site-packages edits

The gateway edits live in the pip-installed site-packages (not a git repo).
`hermes update` re-installs and overwrites them. After an update: re-apply the
`base.py` method + call-site change and the bridge module, then re-run the
verification script. (The skill files under `~/.hermes/skills/` survive.)
