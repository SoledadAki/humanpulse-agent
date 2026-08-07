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

Hermes owns the transport and persistence. This skill deliberately does not
assume a Telegram, Discord, QQ, or web-specific API.

Example import when running from the repository root:

```python
from skills.companion_agent import decide_proactive, normalize_proactive_response
```
