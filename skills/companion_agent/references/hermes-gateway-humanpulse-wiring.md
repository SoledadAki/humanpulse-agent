# Hermes gateway HumanPulse wiring — full reference

This document records exactly how the HumanPulse (companion-agent) wiring is
applied to the Hermes gateway, so it can be re-applied after any `hermes
update` (pip reinstall wipes site-packages edits) and ported to other hosts.

## Architecture

```
 user message ──► Hermes gateway run_sync
                     │
                     ├─► humanpulse_bridge.update_user_activity(history)
                     │       records last_user_at, bounded recent history,
                     │       stops pending follow-up
                     │
                     ├─► humanpulse_bridge.build_hidden_time_context(history)
                     │       build_time_context() → hidden prompt prefix
                     │
                     ├─► humanpulse_bridge.build_proactive_reply_note()
                     │       "user is probably replying to my proactive ping"
                     │
                     └─► agent.run_conversation([HumanPulse context]… + user msg)

 cron humanpulse-proactive (45m, agent mode)
   └─ script humanpulse_proactive.py
        └─ proactive_state_for_agent()
             empty stdout  → cron skips AI call (zero tokens)
             context prompt → cron agent crafts a natural opening line

 cron humanpulse-followup (5m, agent mode)
   └─ script humanpulse_followup.py
        ├─ detect proactive job's newest output → record_proactive_sent()
        └─ followup_prompt_for_agent()
             non-empty stdout → agent generates a contextual follow-up
             empty stdout     → skip model call

HumanPulse cron delivery
  └─ scheduler final platform send
       └─ cron_bubble_bridge.send_cron_reply()
            └─ one real platform send per short bubble
```

## Files

| Path | Role |
|---|---|
| `~/.hermes/skills/companion-agent/runtime.py` | framework-neutral runtime (time sense, proactive, follow-up, bubbles) |
| `~/.hermes/skills/companion-agent/state.py` | JSON state persistence |
| `<site-packages>/gateway/platforms/humanpulse_bridge.py` | loads the skill at runtime; host-facing API |
| `<site-packages>/.../cron/scheduler.py` | routes live and standalone HumanPulse cron delivery through bubbles |
| `<site-packages>/gateway/run.py` | `run_sync` injection (marked `# HumanPulse (companion-agent)`) |
| `~/.hermes/scripts/humanpulse_proactive.py` | proactive cron data script |
| `~/.hermes/scripts/humanpulse_followup.py` | follow-up watchdog script |
| `~/.hermes/humanpulse/state.json` | state file (override `HUMANPULSE_STATE_FILE`) |
| `~/.hermes/skills/companion-agent/adapters/hermes/patch_gateway.py` | idempotent re-apply |
| `~/.hermes/skills/companion-agent/scripts/verify_humanpulse.py` | 15-assertion E2E verify |

## run.py injection detail

Inserted inside `run_sync` (the per-user-message closure in
`GatewayRunner._run_agent_inner`), right after the interrupted-turn safety
note block and before `_approval_session_key = …`:

```python
# HumanPulse (companion-agent): hidden time context + proactive
# reply note.  API-only — the original message is preserved for
# persistence via _persist_user_message_override, exactly like the
# auto-continue note above.  Every function degrades to a safe
# no-op when the skill bridge is not installed.
try:
    from gateway.platforms.humanpulse_bridge import (
             update_user_activity as _hp_update_user_activity,
        build_hidden_time_context as _hp_time_ctx,
        build_proactive_reply_note as _hp_reply_note,
    )
    _hp_note = _hp_reply_note()
    _hp_ctx = _hp_time_ctx(history)
    _hp_update_user_activity(history)
    if isinstance(message, str) and (_hp_ctx or _hp_note):
        if _persist_user_message_override is None:
            _persist_user_message_override = message
        _hp_prefix = "\n\n".join(p for p in (_hp_ctx, _hp_note) if p)
        message = f"[HumanPulse context]\n{_hp_prefix}\n\n{message}"
except Exception:
    pass
```

Why this placement:
- Runs for every real inbound user turn (QQ/WeChat and all platforms).
- `_persist_user_message_override` keeps the stored transcript clean — the
  HumanPulse prefix is API-only, never persisted, never replayed as user text.
- `history` is the raw transcript rows (with `timestamp`), which is exactly
  what `build_time_context()` consumes.
- The proactive reply note is read before `update_user_activity(history)` records the
  current turn; otherwise the new timestamp would make the note look already
  answered and clear it before the model sees it.

## Cron wiring

```bash
hermes cron add --name humanpulse-proactive --schedule "every 45m" \
  --script humanpulse_proactive.py --skill companion-agent --deliver origin \
  "你是一个带 HumanPulse 时间感/主动聊天能力的陪伴角色（琴音）。…"

hermes cron add --name humanpulse-followup --schedule "every 5m" \
  --script humanpulse_followup.py --skill companion-agent --deliver origin \
  "根据 HumanPulse 追问上下文生成一条自然的后续消息；遵循当前人设，不默认撒娇，不提内部机制，没有自然内容时输出 [SILENT]。"
```

- Proactive job uses `script` (data collection) + agent mode: when the script
  prints nothing, Hermes cron skips the AI call entirely (zero tokens). When
  it prints the eligibility status, the agent crafts the message.
- Follow-up job uses agent mode: empty script stdout skips the model call;
  non-empty stdout is a hidden generation context. It scans both job output
  directories, extracts only the final standalone `## Response` section, and
  records delivered text for the next stage. Both HumanPulse jobs use
  `attach_to_session=true` so delivered messages are mirrored into the real
  QQ/WeChat session.

## Re-apply after `hermes update`

```bash
cd ~/.hermes/skills/companion-agent
python3 adapters/hermes/patch_gateway.py   # idempotent: skip if already patched
python3 scripts/verify_humanpulse.py       # 20/20 PASS expected
```

Note: `patch_gateway.py` anchors on the exact interrupted-turn note text in
`run.py`. If a future Hermes version changes that block, the script prints a
clear failure and you patch manually (the anchor diff is in the script).

## Porting to another Hermes-like host

Host contract (keep small):

1. On every inbound user message, before generating the reply:
   - read `build_proactive_reply_note()` first;
   - `update_user_activity(history)`
   - inject `build_hidden_time_context(history)` + `build_proactive_reply_note()`
     as hidden (non-persisted) context.
2. Periodic proactive check: if `proactive_state_for_agent()` returns
   non-empty, it already contains the current period, recent context, optional
   memory/summary, recent proactive messages, and an opening angle. Ask the
   model for a natural line (or `[SILENT]`); after delivery call
   `record_proactive_sent(text)`.
3. Periodic follow-up poll: `followup_tick()`; deliver returned text.
4. Persist the state dict anywhere (default JSON file at
   `~/.hermes/humanpulse/state.json`).
