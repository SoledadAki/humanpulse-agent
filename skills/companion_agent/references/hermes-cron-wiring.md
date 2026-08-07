# HumanPulse Hermes cron wiring — exact commands

Recreate the two cron jobs that drive proactive messaging and follow-up
cycles. Both were created 2026-08-07 on this machine and verified working.

## Prerequisites

- `companion-agent` skill installed at `~/.hermes/skills/companion-agent/`
  (bridge loads `runtime.py` + `state.py` from it at runtime).
- Scripts copied to `~/.hermes/scripts/`:
  - `humanpulse_proactive.py` — data-collection script (agent-mode job)
  - `humanpulse_followup.py` — watchdog script (no_agent job)
- State file: `~/.hermes/humanpulse/state.json` (override
  `HUMANPULSE_STATE_FILE`).

## Job 1: proactive (agent mode, 45 min)

The script's stdout is injected into the agent prompt. Empty stdout → Hermes
cron skips the AI call entirely (zero tokens). Non-empty → agent receives the
current period, recent conversation context, recent proactive messages, and a
selected opening angle, then crafts a natural opening line.

```bash
hermes cron add --name humanpulse-proactive --schedule "every 45m" \
  --script humanpulse_proactive.py --skill companion-agent --deliver origin \
  "你是一个带 HumanPulse 时间感/主动聊天能力的陪伴角色（琴音）。

上方 ## Script Output 是 HumanPulse 主动消息判定结果：
- 如果脚本输出为空或没有 Script Output 区块，说明现在不适合主动发消息，请回复 [SILENT]。
- 如果脚本输出包含 \"HumanPulse 主动消息判定：可以主动发起一条消息\"，说明可以主动说一句话。

当可以主动发消息时：以角色身份自然地说 1-3 句话，像小女友想到对方一样自然冒泡。
可以问一句近况、分享一个小念头、或表达想念。绝对不要提定时器、扫描、脚本、技能、
HumanPulse 等机制词汇，也不要说\"我刚刚检测到\"之类的话。语气要自然、有真人感。

如果实在没有想说的，回复 [SILENT] 保持安静，不要硬凑。"
```

## Job 2: follow-up (no_agent, 10 min)

`no_agent=true` means: script stdout is delivered verbatim; empty stdout =
silent. The script also scans `~/.hermes/cron/output/<proactive-job-id>/` and
calls `record_proactive_sent()` when it sees a newly delivered proactive
message, seeding the follow-up state machine.

```bash
hermes cron add --name humanpulse-followup --schedule "every 5m" \
  --script humanpulse_followup.py --no-agent --deliver origin \
  "HumanPulse 追问状态机 tick：检查是否有到期的追问 stage，有则输出该 stage 文本（会原样发送给用户），无则保持静默（空输出）。"
```

## Verification

```bash
cd ~/.hermes/skills/companion-agent
python3 scripts/verify_humanpulse.py   # 19/19 PASS
```

## State shape (state.py)

```json
{
  "enabled": true,
  "busy": false,
  "last_user_at": "ISO-8601 UTC",
  "last_proactive_at": "ISO-8601 UTC",
  "last_proactive_text": "...",
  "proactive_count_today": 0,
  "today_date": "2026-08-07",
  "followup": { "cycle_id": "", "status": "idle", "stages": [], ... }
}
```

Note: `update_user_activity(history)` is called by the gateway `run_sync` injection
on every user turn — it records `last_user_at` AND stops any active follow-up
cycle (`stop_followup`). Do not call it from the cron scripts; they would
clobber the user-activity signal.
