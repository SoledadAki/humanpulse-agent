# Claude Code adapter

Claude Code can load HumanPulse Agent from a project-local or user-level
skills directory. Keep the standard layout and place `SKILL.md` at the skill
root:

```text
.claude/
└── skills/
    └── companion-agent/
        ├── SKILL.md
        ├── runtime.py
        ├── schema/
        └── examples/
```

Use it when Claude Code is implementing or reviewing a companion host. The
skill provides behavior rules and a dependency-free reference runtime; the
host still owns persistence, scheduling, platform delivery, and cancellation.
Keep the six-function contract from `SKILL.md` intact so the same behavior can
move between Hermes, AstrBot, and another host.

Do not put the full behavior contract in `CLAUDE.md`. Keep `CLAUDE.md` for
project-level instructions such as test commands and architecture, and let
this skill remain portable across repositories.
