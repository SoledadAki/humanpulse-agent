# Hermes adapter

Hermes is the reference runtime host for HumanPulse Agent. A complete install
has four parts: the skill files, gateway context injection, independent bubble
delivery, and two cron jobs. Copying `SKILL.md` alone only changes model
guidance and does not activate the runtime.

## Install or re-apply

Run this from the installed skill directory with the Hermes Python environment:

```bash
python3 adapters/hermes/patch_gateway.py
```

The idempotent patcher:

1. Copies `base_bubble_bridge.py` into `gateway/platforms/`.
2. Patches `gateway/platforms/base.py` so QQ and WeChat send each bubble with
   a separate transport call and stop when a new user message interrupts.
3. Copies `humanpulse_bridge.py` into `gateway/platforms/`.
4. Patches `gateway/run.py` to inject time context and proactive reply context,
   while retaining bounded recent history for later proactive generation
   without persisting hidden context into the transcript.
5. Copies `humanpulse_proactive.py` and `humanpulse_followup.py` into
   `~/.hermes/scripts/`.

Existing files receive `.humanpulse.bak` backups. Use `--dry-run` to inspect
changes or `--site-packages PATH` when Hermes is installed in a nonstandard
location.

Create or update the two jobs from
[`../../references/hermes-cron-wiring.md`](../../references/hermes-cron-wiring.md),
restart the gateway, then run:

```bash
python3 scripts/verify_humanpulse.py
```

Run the patcher again after `hermes update`, because pip reinstalling Hermes
replaces edits under `site-packages`.

## Inbound message order

The gateway must use this order for every real user turn:

```python
note = build_proactive_reply_note()
time_context = build_hidden_time_context(history)
update_user_activity(history)
```

The note must be read first. `update_user_activity(history)` records the current
user turn and intentionally clears the pending proactive-reply signal. The
history copy is bounded and is used only to give the proactive cron enough
conversation context to choose a natural opening.

The combined HumanPulse context is API-only. The original user message is
stored through Hermes' `_persist_user_message_override`, so hidden context is
never written to the transcript and cannot be replayed as if the user said it.

## QQ and WeChat bubbles

A newline inside one model response is still one platform message. The patch
hooks Hermes' final `_process_message_background()` delivery point and routes
QQ/WeChat through `send_human_reply()`. Every bubble calls the real adapter
sender independently, waits for a short bounded typing delay, and checks the
interrupt event before continuing.

Other Hermes platforms keep the original single-send path. If the skill bridge
cannot load, all platforms safely fall back to Hermes' original behavior.

## Six-function contract

The gateway bridge dynamically loads `runtime.py` and `state.py` from
`companion-agent`, `companion_agent`, or `humanpulse-agent` under the Hermes
skills directory. It exposes:

```text
update_user_activity
build_hidden_time_context
build_proactive_reply_note
proactive_state_for_agent
record_proactive_sent
followup_tick
```

State defaults to `~/.hermes/humanpulse/state.json` and can be overridden with
`HUMANPULSE_STATE_FILE`. Writes are atomic. Missing skill files degrade to
safe no-op results. When eligible, `proactive_state_for_agent()` includes the
local period, recent context, optional summary/memory, recent proactive
messages, and the selected opening angle.

The state loader repairs the two known legacy failures on load: nested
`followup.state` commit envelopes are flattened, and oversized cron reports
are rejected as proactive messages. A polluted old state therefore becomes
idle instead of being injected into the next conversation.

Full source-level details are in
[`../../references/hermes-gateway-humanpulse-wiring.md`](../../references/hermes-gateway-humanpulse-wiring.md).
