# AstrBot adapter

AstrBot is a primary runtime target, but this repository does not freeze one
specific AstrBot plugin API version. Keep the plugin glue thin and implement
the same six-function host contract used by the Hermes bridge.

## Required wiring

| Function | AstrBot integration point |
|---|---|
| `update_user_activity(history)` | Start of every inbound handler; retain bounded recent context. |
| `build_hidden_time_context(history)` | Hidden model context for the current turn. |
| `build_proactive_reply_note()` | Read before updating user activity. |
| `proactive_state_for_agent()` | Scheduled proactive eligibility check; empty means no model call. |
| `record_proactive_sent(text)` | Call only after the proactive message is delivered. |
| `followup_tick()` | Short periodic task; deliver returned text verbatim. |

The inbound order is important:

```python
note = build_proactive_reply_note()
time_context = build_hidden_time_context(history)
update_user_activity(history)
```

Inject `note` and `time_context` as non-persisted context. Do not prepend them
to the user message stored by AstrBot.

## Delivery

Use `split_reply_bubbles()` for normal model replies, then call AstrBot's real
message API once per bubble. Stop before the next bubble when a newer user
message arrives. Joining bubbles with newlines still produces one message on
QQ and WeChat clients.

For proactive messages, let `proactive_state_for_agent()` choose a context-aware
opening angle from the local period, recent history, optional summary/memory,
and recent proactive messages. Call `record_proactive_sent(text)` only after
successful delivery. Run `followup_tick()` frequently enough that its polling interval is
shorter than the configured grace period. Any inbound user turn must cancel
the pending cycle through `update_user_activity(history)`.

## Portable runtime

Vendor or import `runtime.py`, `state.py`, and the host-facing bridge functions.
The runtime has no third-party dependencies. Point `HUMANPULSE_STATE_FILE` at
an AstrBot-owned data path or replace the JSON persistence functions with the
plugin's storage API while keeping the state shape stable.

Installing only `SKILL.md` does not register AstrBot event handlers, scheduler
jobs, storage, or platform send calls. Those host hooks are required for the
features to run.
