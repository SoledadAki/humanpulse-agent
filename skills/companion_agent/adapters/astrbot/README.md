# AstrBot adapter

AstrBot plugins should keep platform code thin and call the portable runtime
from their scheduled task and message handler. The runtime has no third-party
dependency, so it can be vendored into a plugin or imported from this project.

```python
from skills.companion_agent import (
    ProactivePolicy,
    build_time_context,
    decide_proactive,
    normalize_proactive_response,
    split_reply_bubbles,
)

decision = decide_proactive(
    {
        "enabled": True,
        "busy": False,
        "last_user_at": last_user_at,
        "last_proactive_at": last_proactive_at,
        "proactive_count_today": count_today,
    },
    policy=ProactivePolicy(min_idle_minutes=180, daily_limit=4),
)
```

Only proceed to model generation when `decision["action"] == "consider"`.
Return the model's proactive JSON through `normalize_proactive_response()`;
send each normalized bubble with AstrBot's normal message API. Keep the
conversation key and timestamps in AstrBot's storage, and cancel a pending
stage when a new inbound message is received.

This is an adapter recipe rather than a bundled AstrBot plugin because AstrBot
plugin APIs and event registration differ across releases. The portable
contract stays stable while the host-specific glue remains small.
