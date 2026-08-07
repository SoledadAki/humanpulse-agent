# Companion Agent Runtime

> A framework-neutral behavior layer for human-like agent chat and roleplay.
> Built for Hermes and AstrBot, reusable from Codex and Claude Code.

Companion Agent Runtime turns the hard-to-reuse parts of natural agent
conversation into a portable skill and a dependency-free Python runtime.
It focuses on **anthropomorphic feel**: the pacing, timing, length, rhythm, and
initiative that make roleplay and companion chat feel less like a form and more
like a conversation.

## What it provides

- **Human-like expression** — context-sensitive reply length, varied rhythm,
  natural bubbles, and conversational punctuation instead of a full stop on
  every line.
- **Time-aware continuity** — distinguishes continuous chat, short pauses,
  returning later the same day, coming back after a night, and longer absences.
- **Segmented replies** — supports natural multi-bubble responses without
  breaking code, lists, URLs, or tightly connected explanations.
- **Proactive conversation** — quiet hours, cooldowns, idle checks, daily
  limits, safe silence, and a structured JSON response protocol.
- **No-reply follow-ups** — staged follow-up cycles that wait for user activity,
  retry only when appropriate, stop immediately when the user replies, and do
  not catch up missed stages.
- **Portable integration** — no transport, database, scheduler, or LLM SDK is
  required by the core runtime.

## Integration targets

Hermes and AstrBot are the primary runtime hosts. They can map the skill and
runtime calls to their own message APIs, persistence, and schedulers.

Codex and Claude Code can load the same skill as development agents. They are
useful for implementing, reviewing, and maintaining a companion host, but they
are not treated as message transports by this project.

## Architecture

```text
                 model / roleplay prompt
                           │
                           ▼
     ┌────────────────────────────────────────┐
     │ Companion Agent Skill                   │
     │ human-like style · time · initiative   │
     └────────────────────────────────────────┘
                           │
                           ▼
     ┌────────────────────────────────────────┐
     │ Portable Python runtime                │
     │ context · bubbles · proactive state    │
     │ follow-up claim · cancel · commit      │
     └────────────────────────────────────────┘
                           │
             ┌─────────────┴─────────────┐
             ▼                           ▼
        Hermes / AstrBot          other chat hosts
        scheduler + transport     Discord · QQ · Web · ...
```

The host remains responsible for message delivery, persistence, permissions,
and calling the follow-up state machine on a schedule. The skill defines the
behavior contract; it does not pretend to be a platform bot.

## Quick start

Copy `skills/companion_agent/` into the host's skill directory and keep
`SKILL.md` at the skill root. For Hermes, use its user or project skills
directory. For AstrBot, import or vendor `runtime.py` from a thin plugin
adapter.

For Codex and Claude Code, install the same directory as a development skill:

```text
.codex/skills/companion-agent/SKILL.md
.claude/skills/companion-agent/SKILL.md
```

The runtime exposes small, host-agnostic operations:

```python
from skills.companion_agent import (
    build_time_context,
    decide_proactive,
    normalize_proactive_response,
    start_followup_cycle,
    poll_followup,
    commit_followup,
    stop_followup,
)
```

## Follow-up cycle

The default no-reply follow-up windows are intentionally human-paced:

```text
35–50 minutes → 15–25 minutes → 3–8 minutes → 1–3 minutes
```

The host should persist the state returned by `start_followup_cycle()`, call
`poll_followup()` from its scheduler, send a claimed stage, then call
`commit_followup()`. Any new user message should call `stop_followup()` first.
Stages missed beyond the grace period are discarded rather than sent late.

## Safety and boundaries

Roleplay can add voice, mood, fictional reactions, and character continuity. It
must not invent real-world actions, private memories, browsing, or shared
experiences. Human-like expression means believable pacing and wording, not
deception about what the agent is or what it has actually done.

## Local verification

```powershell
python -m unittest tests.test_companion_skill -v
```

The repository intentionally has no third-party runtime dependency.

## 中文简介

这是一个面向 Hermes、AstrBot 的通用 AI 拟人感行为层，也可以被 Codex 和
Claude Code 作为开发 skill 使用。它把时间感、聊天节奏、常人化字数、
口语化标点、分段式回复、主动发话和无回复追问整理成可复用协议。

项目不绑定具体聊天平台，也不负责发送消息；宿主负责调度、存储、权限和
消息投递，skill 负责“应该怎样自然地聊”。
