# Companion Agent Skill — Hermes 专属

一个面向 **Hermes** 的 AI 陪聊拟人感 skill，解决五个常见问题：

- 有时间感：知道连续聊天、短暂离开、隔夜回来和较长时间未见面的区别。
- 分段式对话：把长回复拆成自然气泡，支持主动消息的多阶段递进。
- 聊天自然：优先接住最新消息，减少模板句、连续追问和无依据脑补。
- 自然主动发话：有空窗、冷却、安静时段和每日上限，没自然话题时允许沉默。
- 上下文主动发话：根据当前时段、近期聊天、摘要、记忆和最近主动消息选择
  不同开场角度，避免重复。
- 无回复追问：主动消息发出后按阶段等待，用户不回复才继续，用户一开口
  立即取消后续阶段。

运行时只依赖 Python 标准库，不绑定任何 LLM SDK。宿主（Hermes）负责调度、
存储、发送、权限和用户插话取消；skill 负责上下文协议、行为规则和输出校验。

## 目录

- `SKILL.md` — 模型行为说明
- `runtime.py` — 纯标准库参考实现（时间感、气泡、主动判定、追问状态机）
- `state.py` — 原子 JSON 状态存储
- `gateway/platforms/` — Hermes 网关 bridge 源文件（打补丁时复制进
  site-packages）
- `adapters/hermes/` — Hermes 完整接线（install.py 一键安装、
  patch_gateway.py 补丁脚本、cron 脚本）
- `references/` — 接线文档 + 实战踩坑记录（hermes-pitfalls.md）
- `scripts/` — 验证套件（verify_humanpulse / verify_patch_gateway /
  verify_bubble_delivery）
- `schema/`、`examples/` — 主动消息 JSON Schema 与示例

本项目只适配 Hermes（QQ/微信气泡 + cron）。其他 agent 适配已移除。

## 一键安装

```bash
python3 adapters/hermes/install.py
```

幂等：复制 skill、打 gateway 补丁、建 cron（跳过已存在）、清 .env 禁用
开关、跑验证。`hermes update` 后重跑即可。

## 六函数宿主契约

`update_user_activity(history)`、`build_hidden_time_context`、
`build_proactive_reply_note`、`proactive_state_for_agent`、
`record_proactive_sent`、`followup_tick`。

主动消息不是固定模板：`build_proactive_prompt()` 会给宿主一个当前时段、
近期上下文和本轮主动角度，模型据此决定具体说什么，也可以在没有自然内容
时保持静默。

仅加载 `SKILL.md` 不会自动产生时间感、主动消息或追问。必须运行
`adapters/hermes/install.py` 完成网关接线。

## 本地检查

```bash
python3 -m unittest tests.test_companion_skill -v
python3 scripts/verify_humanpulse.py
python3 scripts/verify_patch_gateway.py
python3 scripts/verify_bubble_delivery.py
```

## 发布定位

仓库名：`humanpulse-agent`（本仓库）的 Hermes 专属版。核心卖点：
框架无关、无依赖、时间感、真实多气泡发送、分阶段主动对话、可取消发送
和主动行为安全边界。
