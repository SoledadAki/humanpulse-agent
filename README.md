# HumanPulse Agent

> A portable human-like interaction layer for companion chat and roleplay agents.
> Built first for Hermes and AstrBot, with reusable guidance for Codex and Claude Code.

HumanPulse Agent packages the behavior that makes an agent feel present in an
ongoing conversation: awareness of elapsed time, natural reply length and
punctuation, independent message bubbles, proactive openings, and staged
follow-ups when the user does not reply.

The core runtime uses only the Python standard library. Hermes receives a full
gateway and cron adapter; other hosts can implement the same six-function
contract without adopting Hermes internals.

## Features

- Time-aware continuity: continuous chat, short pauses, same-day returns,
  overnight gaps, and longer absences.
- Human-like expression: context-sensitive length, varied rhythm, less
  mechanical punctuation, and no forced question at the end of every reply.
- Real message bubbles: QQ and WeChat can receive multiple independent
  messages instead of one message containing several newline-separated parts.
- Proactive conversation: idle checks, cooldowns, quiet hours, daily limits,
  context-aware opening angles, and zero-token silence when no message should
  be generated.
- No-reply follow-ups: the delivered proactive message becomes stage 0;
  the host chooses a bounded 0–3 follow-up count, uses slightly varied timing,
  generates each continuation in the active persona, discards missed stages,
  and cancels the remainder on any user reply.
- Safe fallback: if the skill or bridge is missing, Hermes keeps its original
  behavior instead of failing the gateway.

## Host contract

The portable Hermes bridge exposes six operations. AstrBot and other chat
hosts can implement the same contract:

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

## Architecture

```text
Inbound user message
  -> proactive reply note + time context
  -> update user activity / cancel follow-up
  -> model reply
  -> QQ/WeChat independent bubble delivery

Proactive cron
  -> proactive_state_for_agent()
  -> empty: skip model call
  -> eligible: time/context/angle prompt -> model writes a natural opening
  -> record_proactive_sent(delivered_text)

Follow-up cron
  -> followup_tick()
  -> due text: deliver verbatim
  -> None: stay silent
```

## Hermes installation

Installing only `SKILL.md` changes model guidance but does not activate the
runtime features. Hermes also needs the gateway bridge, the final delivery
hook, and two cron jobs.

From an installed skill directory:

```bash
python3 adapters/hermes/patch_gateway.py
```

The patcher is idempotent. It copies both gateway bridges, patches
`gateway/platforms/base.py` and `gateway/run.py`, and installs the two cron
scripts under `~/.hermes/scripts/`. It creates `.humanpulse.bak` backups before
changing existing gateway files.

On the first use after upgrading, `state.py` also repairs legacy state: it
unwraps an accidental `followup.state` envelope, removes oversized cron
reports from `last_proactive_text`, and leaves the follow-up cycle idle instead
of sending polluted text.

Then create the proactive and follow-up jobs from
[`references/hermes-cron-wiring.md`](skills/companion_agent/references/hermes-cron-wiring.md),
restart the Hermes gateway, and verify:

```bash
python3 scripts/verify_humanpulse.py
```

Hermes upgrades reinstall `site-packages`, so run `patch_gateway.py` again
after `hermes update`. Existing cron jobs and HumanPulse state are kept.

The proactive prompt uses the current local period, whether the daily window
has just opened, bounded recent chat history, optional summary and memory
fields, and the last five proactive messages. It selects an opening angle such
as a period-aware greeting, continuing an open question, sharing an observation,
changing away from a repeated topic, or checking in naturally. The model still
chooses the wording and may return `[SILENT]` when there is no believable topic.

Detailed wiring: [`adapters/hermes/README.md`](skills/companion_agent/adapters/hermes/README.md)

## Install with an agent

Give this repository URL and the following request to an agent that has local
filesystem access:

```text
Install HumanPulse Agent from https://github.com/SoledadAki/humanpulse-agent
for my current host.

If this is Hermes:
1. Install skills/companion_agent as a Hermes skill.
2. Run adapters/hermes/patch_gateway.py with the Hermes Python environment.
3. Inspect existing cron jobs, then create or update the two jobs documented in
   references/hermes-cron-wiring.md without creating duplicates.
4. Do not change credentials, persona prompts, unrelated configuration, or
   other platforms.
5. Restart the gateway only after telling me it is required.
6. Run scripts/verify_humanpulse.py and report each failed check exactly.

If this is AstrBot, implement the six-function host contract from
adapters/astrbot/README.md using the installed AstrBot plugin API.
For Codex or Claude Code, install only the development skill and do not pretend
that proactive scheduling or message transport exists unless a host provides it.
```

## AstrBot, Codex, and Claude Code

AstrBot is a primary runtime target. Its plugin should call the six host
functions from its inbound handler and scheduler, then send every bubble with
the platform's real outbound API. See
[`adapters/astrbot/README.md`](skills/companion_agent/adapters/astrbot/README.md).

Codex and Claude Code can load `SKILL.md` as development guidance for building
or maintaining a chat host. They are not message transports or schedulers by
themselves.

## Verification

```powershell
python -m unittest discover -s tests -v
```

The repository has no third-party runtime dependency.

## 中文简介

HumanPulse Agent 是一个面向 Hermes、AstrBot 的通用 AI 拟人感行为层，也可
作为 Codex 和 Claude Code 的开发 skill。它把时间感、常人化字数和标点、
真实多气泡发送、自然主动发话、主动消息后的接话提示，以及用户未回复时的
分阶段追问整理成可移植的宿主契约。

Hermes 适配器已经覆盖完整链路，不再只是“库里有功能但没人调用”。AstrBot
可以按相同六函数契约接入，核心运行时不绑定具体平台或 LLM SDK。
