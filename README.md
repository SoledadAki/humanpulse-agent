# Companion Agent — Hermes 专属版

> Human-like interaction layer for companion chat on Hermes: time-aware
> continuity, natural reply pacing, real QQ/WeChat bubbles, safe proactive
> conversation, and staged no-reply follow-ups.
>
> 本项目已精简为 **Hermes 唯一适配**：不再适配 AstrBot / Codex / Claude
> Code。安装是一键的，不需要手工魔改 Hermes 代码。

HumanPulse packages the behavior that makes an agent feel present in an
ongoing conversation: awareness of elapsed time, natural reply length and
punctuation, independent message bubbles, proactive openings, and staged
follow-ups when the user does not reply.

The core runtime uses only the Python standard library. Hermes receives the
full gateway + cron adapter.

The default local-time policy is active from `08:00` inclusive through `23:00`
exclusive, with quiet hours from `23:00` to `08:00`. The timezone defaults to
the host machine's local system timezone and is carried into proactive
scheduling; hosts can override it per state or policy with an IANA timezone.

## Features

- Time-aware continuity: continuous chat, short pauses, same-day returns,
  overnight gaps, and longer absences.
- Human-like expression: context-sensitive length, varied rhythm, less
  mechanical punctuation, and no forced question at the end of every reply.
- Real message bubbles: QQ and WeChat receive multiple independent
  messages instead of one message containing several newline-separated parts.
- Proactive conversation: idle checks, cooldowns, quiet hours, daily limits,
  context-aware opening angles, and zero-token silence when no message should
  be generated.
- No-reply follow-ups: the delivered proactive message becomes stage 0;
  the host chooses a bounded 0–2 follow-up count, uses slightly varied timing,
  generates each continuation in the active persona, discards missed stages,
  and cancels the remainder on any user reply.
- Safe fallback: if the skill or bridge is missing, Hermes keeps its original
  behavior instead of failing the gateway.

## One-shot install

```bash
cd skills/companion_agent
python3 adapters/hermes/install.py
```

The installer is idempotent and covers the full wiring:

1. Copies the skill into `~/.hermes/skills/companion-agent/`.
2. Applies the gateway patch (hidden time-context injection in `run.py`,
   QQ/WeChat bubble delivery in `base.py`, cron bubble routing, bridge
   modules, cron scripts, `attach_to_session` on the two HumanPulse jobs).
3. Creates the two cron jobs (`humanpulse-proactive` every 45m,
   `humanpulse-followup` every 5m) if missing.
4. Removes the old disable switches from `.env`.
5. Runs the verification suite.

After `hermes update`, re-run the installer — pip reinstalls wipe the
site-packages edits.

## Architecture

```text
Inbound user message
  -> proactive reply note + time context
  -> update user activity / cancel follow-up
  -> model reply
  -> QQ/WeChat independent bubble delivery

Proactive cron (45m)
  -> proactive_state_for_agent()
  -> empty: skip model call
  -> eligible: time/context/angle prompt -> model writes a natural opening
  -> record_proactive_sent(delivered_text)

Follow-up cron (5m)
  -> followup_tick()
  -> due text: deliver verbatim (agent generates in persona)
  -> None: stay silent
```

## Host contract

The Hermes bridge exposes six operations. Hosts implementing the same contract
can reuse the runtime:

| Function | Host responsibility |
|---|---|
| `update_user_activity(history=None)` | Record an inbound turn, retain bounded recent context, and cancel pending follow-ups. |
| `build_hidden_time_context(history)` | Build non-user-visible temporal context for the model. |
| `build_proactive_reply_note()` | Tell the model that the next user turn may answer the last proactive message. |
| `proactive_state_for_agent()` | Return a context-aware eligibility prompt, or empty output to skip the model call. |
| `record_proactive_sent(text)` | Record a delivered proactive message and seed its follow-up cycle. |
| `followup_tick()` | Return one due follow-up message, or `None` to stay silent. |

For inbound messages, read `build_proactive_reply_note()` before calling
`update_user_activity(history)`. Otherwise the current user turn clears the note
before the model can see it. Hidden context must remain API-only and must not
be persisted or replayed as user text.

## Documentation

- `skills/companion_agent/adapters/hermes/README.md` — Hermes install/wiring
- `skills/companion_agent/references/hermes-cron-wiring.md` — exact cron commands
- `skills/companion_agent/references/hermes-gateway-humanpulse-wiring.md` — gateway wiring detail
- `skills/companion_agent/references/hermes-gateway-bubble-wiring.md` — bubble delivery detail
- `skills/companion_agent/references/hermes-pitfalls.md` — 实战踩坑记录（必读）

## Verification

```bash
cd skills/companion_agent
python3 scripts/verify_humanpulse.py       # wiring-level suite
python3 scripts/verify_patch_gateway.py    # patcher round-trip suite
python3 scripts/verify_bubble_delivery.py  # bubble delivery suite
```

The repository has no third-party runtime dependency.

## 中文简介

Companion Agent 是一个面向 Hermes 的 AI 拟人感行为层。它把时间感、常人化
字数和标点、真实多气泡发送、自然主动发话、主动消息后的接话提示，以及用户
未回复时的分阶段追问整理成可移植的宿主契约。Hermes 适配器覆盖完整链路，
一键安装即用；不再适配其他 agent。
