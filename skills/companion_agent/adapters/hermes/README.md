# Hermes adapter

Hermes is the only supported runtime host for this skill. A complete install
has four parts: the skill files, gateway context injection, independent bubble
delivery, and two cron jobs. Copying `SKILL.md` alone only changes model
guidance and does not activate the runtime.

## One-shot install / re-apply

Run from this skill directory with the Hermes Python environment:

```bash
python3 adapters/hermes/install.py
```

The installer is idempotent and does all of:

1. Copies this skill into `~/.hermes/skills/companion-agent/` (backing up an
   existing dir once as `companion-agent.bak.HUMANPULSE`).
2. Runs `patch_gateway.py`, which:
   - copies `base_bubble_bridge.py`, `humanpulse_bridge.py`, and
     `cron_bubble_bridge.py` into `<site-packages>/gateway/platforms/`;
   - patches `gateway/platforms/base.py` so QQ and WeChat send each bubble
     with a separate transport call and stop when a new user message
     interrupts (skips when already patched / Hermes has it natively);
   - patches `gateway/run.py` to inject hidden time context and proactive
     reply context on every user turn, retaining bounded recent history
     without persisting hidden context into the transcript;
   - treats Hermes >= 0.18.2 cron scheduler as already-wired when
     `_HUMANPULSE_BUBBLE_SENDER` is present (both live-adapter and standalone
     cron delivery paths route `humanpulse*` jobs through independent
     bubbles — a manual fix applied 2026-08-09 survives as native presence);
   - copies the cron scripts into `~/.hermes/scripts/` and enables session
     mirroring only for the two HumanPulse jobs in `~/.hermes/cron/jobs.json`.
3. Creates the two cron jobs (`humanpulse-proactive`, `humanpulse-followup`)
   if they do not already exist (see `../../references/hermes-cron-wiring.md`).
4. Removes the `HERMES_HUMANPULSE_CONTEXT` / `HERMES_BUBBLE_DELIVERY`
   disable switches from `~/.hermes/.env` if a previous disable left them.
5. Runs `scripts/verify_humanpulse.py` and reports every failure.

Existing gateway files receive `.humanpulse.bak` backups. Use `--dry-run` to
inspect changes, or `--site-packages PATH` on the patcher when Hermes is
installed in a nonstandard location.

After `hermes update` (pip reinstall), run `install.py` again — Hermes
reinstalling replaces edits under `site-packages`.

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

Hermes follow-ups use agent mode rather than fixed no-agent text. The gate
stays silent when no stage is due; when due, it gives the model the original
proactive message, delivered follow-ups, recent context, stage number, and a
safe fallback direction. The model writes the actual message in the current
persona's voice. The default cycle chooses a variable 0–2 follow-up count and
slightly jittered intervals around 26–36, 8–13, and 4–7 minutes.

Cron delivery is a separate Hermes path from normal gateway replies. The
scheduler routes HumanPulse job results through
`gateway.platforms.cron_bubble_bridge.send_cron_reply()` (or the native
`_HUMANPULSE_BUBBLE_SENDER` path in >= 0.18.2), which calls the real platform
sender once per short bubble.

## Troubleshooting

Read `../../references/hermes-pitfalls.md` first — it documents every failure
mode hit on this machine (duplicate delivery, cross-day rollover deadlock,
follow-up state machine off-by-ones, quiet-hours gating, cron dual-path
bubble bypass) with the exact fixes and regression assertions.

Full source-level details are in
[`../../references/hermes-gateway-humanpulse-wiring.md`](../../references/hermes-gateway-humanpulse-wiring.md)
and
[`../../references/hermes-gateway-bubble-wiring.md`](../../references/hermes-gateway-bubble-wiring.md).
