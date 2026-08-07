# Codex adapter

Codex can use this package as a reusable development skill. It is useful when
building or reviewing a chat host that needs time-aware continuity, segmented
delivery, and proactive-message guardrails.

Install the skill into a Codex skills directory by copying the whole
`skills/companion_agent` directory and keeping its `SKILL.md` at the skill root.
For a repository-local setup, the target shape is:

```text
.codex/
└── skills/
    └── companion-agent/
        ├── SKILL.md
        ├── runtime.py
        ├── schema/
        └── examples/
```

Codex can then use the skill when implementing an AstrBot plugin, Hermes
workflow, or another transport adapter. The reference runtime is ordinary
Python and should be imported or vendored by the actual host; Codex itself is
not the message transport or proactive scheduler.

For durable repository-specific commands and test rules, use `AGENTS.md` in the
host project. Keep those instructions separate from this reusable behavior
skill.
