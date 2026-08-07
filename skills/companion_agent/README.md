# Companion Agent Skill

一个框架无关的 AI 陪聊拟人感 skill，解决五个常见问题：

- 有时间感：知道连续聊天、短暂离开、隔夜回来和较长时间未见面的区别。
- 分段式对话：把长回复拆成自然气泡，支持主动消息的多阶段递进。
- 聊天自然：优先接住最新消息，减少模板句、连续追问和无依据脑补。
- 自然主动发话：有空窗、冷却、安静时段和每日上限，没自然话题时允许沉默。
- 上下文主动发话：根据当前时段、近期聊天、摘要、记忆和最近主动消息选择不同开场角度，避免重复。
- 无回复追问：主动消息发出后按阶段等待，用户不回复才继续，用户一开口立即取消后续阶段。

它不绑定 QQ、微信、Discord 或任何 LLM SDK。宿主负责调度、存储、发送、
权限和用户插话取消；skill 负责上下文协议、行为规则和输出校验。

## 目录

`SKILL.md` 是模型行为说明；`runtime.py` 是纯标准库参考实现；`state.py`
提供原子 JSON 状态存储；`gateway/`、`scripts/` 和 `adapters/hermes/`
组成 Hermes 的完整接线；`schema/` 是主动消息 JSON Schema；其他
`adapters/` 提供 AstrBot、Codex 和 Claude Code 接入方式。

其中 Hermes/AstrBot 是运行时宿主接入；Codex/Claude Code 是开发代理接入，
用于指导它们实现或维护真正的聊天宿主。

## 六函数宿主契约

`update_user_activity(history)`、`build_hidden_time_context`、
`build_proactive_reply_note`、`proactive_state_for_agent`、
`record_proactive_sent`、`followup_tick`。

主动消息不是固定模板：`build_proactive_prompt()` 会给宿主一个当前时段、
近期上下文和本轮主动角度，模型据此决定具体说什么，也可以在没有自然内容
时保持静默。

仅加载 `SKILL.md` 不会自动产生时间感、主动消息或追问。运行时宿主必须把
这六个函数接入消息入口、隐藏上下文、调度器和真实消息发送链路。

## 本地检查

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_companion_skill -v
```

## 发布定位

这个目录可以独立拆成 GitHub 仓库，也可以先作为 LiaoData 的可移植子项目。
仓库名：`humanpulse-agent`。项目介绍可强调：框架无关、无依赖、时间感、
真实多气泡发送、分阶段主动对话、可取消发送和主动行为安全边界。
