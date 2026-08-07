# Hermes adapter

Hermes can load the portable behavior directly as an Agent Skill. Copy or
symlink the `companion_agent` directory into the Hermes skills directory, then
include the rendered `SKILL.md` in the agent's skill selection.

The host loop should:

1. Build `messages` and the hidden time context with `build_time_context()`.
2. Call `decide_proactive()` from the scheduler.
3. Ask the model for the JSON protocol only when the decision is `ELIGIBLE`.
4. Validate the result with `normalize_proactive_response()`.
5. Deliver bubbles in order and cancel later stages as soon as the user replies.

### QQ/WeChat segmented delivery

The model output alone does not create separate platform messages. Hermes must
call its QQ/WeChat sender once per bubble. Use
`send_bubbles.py` as the reference bridge:

```python
from skills.companion_agent.adapters.hermes.send_bubbles import send_human_reply

result = await send_human_reply(
    model_text,
    send_one=hermes_send_message,
    is_cancelled=lambda: conversation_has_new_user_message(),
)
```

`hermes_send_message` must be the actual QQ or WeChat outbound API. Do not pass
one joined string to it. The helper sends each bubble independently, adds a
short bounded typing pause, and stops before the next bubble when the user
speaks.

For proactive follow-ups, use `start_followup_cycle()`, `poll_followup()`, and
`commit_followup()` around the same sender. A new inbound message should call
`stop_followup()` before generating its reply.

Hermes owns the transport and persistence. This skill deliberately does not
assume a Telegram, Discord, QQ, or web-specific API.

Example import when running from the repository root:

```python
from skills.companion_agent import (
    decide_proactive,
    normalize_proactive_response,
    poll_followup,
    start_followup_cycle,
    stop_followup,
)
```
