#!/usr/bin/env python3
"""HumanPulse proactive cron script (data-collection mode).

Hermes cron contract: the script's stdout is injected into the agent prompt.
Empty stdout -> Hermes cron skips the AI call entirely (zero tokens).

So this script prints:
  * nothing              -> decide_proactive() says skip (user recently
                            active / quiet hours / daily limit / cooldown)
  * a status block       -> a proactive message is eligible; the cron agent
                            reads the status and crafts a natural opening line

After the agent replies, the follow-up cron job (humanpulse_followup.py)
detects the delivered output, records it via ``record_proactive_sent()``,
and starts the staged follow-up cycle.
"""

import os
import sys
import traceback

# Make sure the gateway site-packages is importable even when this script is
# run by a different interpreter (cron uses the Hermes venv python).
_SITE = "/usr/local/lib/hermes-agent/venv/lib/python3.11/site-packages"
if _SITE not in sys.path:
    sys.path.insert(0, _SITE)


def main() -> int:
    try:
        from gateway.platforms.humanpulse_bridge import proactive_state_for_agent
    except Exception as exc:
        print(f"[HumanPulse error] bridge import failed: {exc}", flush=True)
        return 1

    try:
        status = proactive_state_for_agent()
    except Exception:
        print(f"[HumanPulse error] proactive_state_for_agent failed:\n{traceback.format_exc()}", flush=True)
        return 1

    if status:
        print(status, flush=True)
    # Empty stdout -> cron skips the AI call. Nothing more to do.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
