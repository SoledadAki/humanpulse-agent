# Hermes wiring pitfalls — learned the hard way

实战踩坑记录（2026-08-07 ~ 08-12，本机验证）。任何 `hermes update` 后重打
补丁、或排查 HumanPulse 不工作/行为异常时，先看这份清单。

## 1. Hermes 接入基础

- **`spec_from_file_location` 用裸模块名会弄坏 `@dataclass`。**
  以模块名 `runtime` 加载 `runtime.py` 会报
  `'NoneType' object has no attribute '__dict__'`，因为 dataclass 机制按
  `cls.__module__` 去 `sys.modules` 解析。必须用带命名空间的名字
  （`humanpulse_runtime`、`humanpulse_state`），并把它注册到 `sys.modules`
  同名键下。
- **Gateway 不热加载 site-packages 改动。** 运行中的 `hermes gateway run`
  内存里还是旧字节码。改完 `run.py`/bridge 后必须重启 gateway 才生效；
  用 `ps -o lstart` 对比文件 mtime 确认是新进程。
- **API-only 前缀必须配 `_persist_user_message_override`。** 在 `run_sync`
  里往 `message` 前插隐藏上下文（`[HumanPulse context]`）时，先把干净原稿
  存进 `_persist_user_message_override = message`，否则前缀会被写进会话记录，
  之后被当成用户原话重放。
- **`hermes update` 会清光所有 site-packages 编辑**（气泡 bridge +
  humanpulse bridge + run.py patch）。重打：`adapters/hermes/patch_gateway.py`
  （幂等，留 `.bak`），再跑 `scripts/verify_humanpulse.py`。

## 2. Cron 数据脚本与投递路径

- **Cron 数据脚本空 stdout = 零 token。** Hermes cron 在数据脚本没输出时
  完全跳过 AI 调用。这是天然的闸门：`proactive_state_for_agent()` 在
  `decide_proactive()` 判定 skip 时返回 `""`，安静 tick 零成本。
- **Cron 投递有两条路径，气泡补丁必须两条都盖。**
  `cron/scheduler.py::_deliver_result()` 在 gateway 运行时走 LIVE adapter
  路径（DeliveryRouter + `router._deliver_to_platform`），gateway 挂了才
  回退 standalone 路径（新 `asyncio.run`）。最早的气泡补丁只盖了
  standalone，所以 proactive/follow-up 在正常运行时还是整块一条
  （2026-08-08 实测）。LIVE 路径也必须把 `humanpulse*` 任务路由到
  `_HUMANPULSE_BUBBLE_SENDER`，每个气泡回调走同一个
  `router._deliver_to_platform`（保留 topic 路由）。
  `verify_humanpulse.py` 会断言两条分支都存在。
- **`humanpulse_followup.py` 的扫描要忽略 cron 记录噪音。**
  proactive 任务的输出目录有完整运行记录（header + prompt + skill +
  response）还有跳过 tick 留下的 0 字节文件。naive 读最新文件会拿到空跳过
  或整篇报告当消息。要跳过空/`[SILENT]` 文件，只用 `## Response` 段
  （`_extract_response`）。
- **`_extract_response` 必须按行首锚定 `^## Response$`，不能 `find("## Response")`。**
  SKILL.md 正文里就有 "## Response" 字样（文档引用），naive `find` 会匹配
  文档而非真实段头，把一大坨报告当"消息"存进 `last_proactive_text`
  （实测 1366 字符）。用 MULTILINE 正则锚定行首。

## 3. Follow-up 状态机（2026-08-07 修复，回归测试守护）

- **`poll_followup` 差一错误：** `stage` 是 1-based（"第 N 段"），`plan` 是
  0-based 列表。用 `plan[stage]` 会跳过第一段、永远丢最后一段。正确：
  `plan[stage - 1]`；`stage > len(plan)`（不是 `>=`）才算完——
  `stage == len(plan)` 仍是最后一段的有效值。
- **`commit_followup` 差一错误：** `stage + 1 < len(plan)` 会丢掉最后一段
  （3 段只发 2 段）。正确：`stage < len(plan)` 表示还有下一段。
- **Cron tick 节奏 vs grace 窗口：** 默认 `FollowupPolicy` grace 5 分钟，
  10 分钟一次的 cron 会越过 grace 导致 `MISSED_STAGE` 丢弃。bridge 的
  `record_proactive_sent()` 用调优策略（grace 8 分钟，间隔
  30/10/5 分钟），追问 cron 必须 **每 5 分钟** tick。铁律：
  `tick_interval < grace_minutes`。
- **`followup_tick()` 必须持久化内部扁平 follow-up dict，不是 commit 包装。**
  `commit_followup()` 返回 `{"status": ..., "state": <扁平 dict>}`。
  把整个返回值存成 `state["followup"]` 会嵌套，下次 `poll_followup()`
  看到 `status="committed"` 而不是 `"active"`，循环在第 1 段后卡死。
  正确：`state["followup"] = committed.get("state", committed)`。
- **自然追问文本来自模型，不是硬编码模板。** "刚说到一半……" 无法承接
  proactive 消息（"莫少早上好！" 该接 "还没醒吗"，不是 "刚说到一半"）。
  追问 cron 用 **agent 模式**（不是 `no_agent`）：脚本到点打印上下文块
  （proactive 原文 + "第 N/M 段追问"），agent 据此生成真正追问。空 stdout
  仍是零 token。

## 4. 主动消息回复注记（2026-08-08 修复）

- **顺序铁律：`build_proactive_reply_note()` 必须在
  `update_user_activity()` 之前调用。** 注记判断"用户这条消息是不是在回我
  的主动消息"，比较 `last_user_at >= last_proactive_at`。gateway 先盖
  `last_user_at` 就会永远"用户已经在 ping 之后说过话"，注记永不触发。
  `run.py` 补丁里带了注释说明；`hermes update` 重打时保持顺序。
- **Cron `mirror_delivery` 默认关——主动/追问消息不进会话历史。**
  不镜像的话，下一条用户回复看不到 assistant 的主动消息，回复注记指向
  不存在的"上一条 assistant 消息"，agent 无法自然续接。修法：给两个
  humanpulse cron 任务设 `attach_to_session: true`（`hermes cron edit`
  CLI 不暴露该字段——直接编辑 `~/.hermes/cron/jobs.json`）。
  `patch_gateway.py` 会自动做这件事，只改这两个任务，其他 cron 不动。

## 5. 主动轮次不能叠在未回复的追问循环上（2026-08-08 修复）

- 症状：约定节奏是 1 主动 + 2 追问 = 3 条，但 08-08 收到 4 条
  （14:43 ping，15:13 + 15:23 追问，16:14 又来一条主动）。
  根因：`decide_proactive()` 只查 60 分钟冷却 + 日限额，从不查用户是否
  回过上一轮，于是 45 分钟一次的主动 cron 在追问循环结束后立刻开新轮。
- 修法一：`decide_proactive()` 增加两个 skip 原因——`FOLLOWUP_ACTIVE`
  （`state["followup"]["status"] == "active"` 时不叠新轮）和
  `USER_NOT_REPLIED`（`last_user_at < last_proactive_at` 时用户没回过
  上一轮，新轮会重复唠叨）。用户回复后（`last_user_at >=
  last_proactive_at`）才重新可发。
- 修法二：`proactive_state_for_agent()` 附带上次主动文本
  （`你上次主动发过的消息：<text>`）+ 提示接续话题而非重新问候。
- 测试注意：`proactive_state_for_agent()` 用默认 `ProactivePolicy`
  （min_idle=120），把 `last_user_at` 设成"现在"会被
  `USER_RECENTLY_ACTIVE` 跳过返回 `""`。要种 `last_proactive_at` 5 小时前、
  `last_user_at` 3 小时前（idle ≥ 120 且仍 `>= last_proactive_at`）。

## 6. 跨日回滚死锁（2026-08-09 修复）

- 症状：一整天零主动消息，cron 正常 tick。根因：`proactive_count_today`
  只在 `record_proactive_sent()` 里重置，而那要等真的发出消息才执行。
  昨天用满 `daily_limit=4` 且用户没回 → 计数永不回滚 → 每次 tick 都撞
  `DAILY_LIMIT` → 脚本无输出 → 不发消息 → 回滚代码永远到不了：
  死锁，`today_date` 卡在昨天。
- 修法：`proactive_state_for_agent()` 在 `state["today_date"] != today`
  时先回滚（写 `today_date`、清零计数、`_save_state`）**再**调
  `decide_proactive()`。只改内存 dict 不落盘，下一次 tick 还是旧文件。
- 测试注意：跨日解锁断言只在非静默时段稳定（函数用默认策略含静默闸）。
  00:00–08:00 回滚照常发生但 tick 因 `QUIET_HOURS` 正确跳过，所以那段只
  断言重置（today_date/count），08:00 后才断言非空状态块。

## 7. 静默时段闸门（2026-08-08 增加）

- 本机活跃窗口 **08:00–24:00**（`ProactivePolicy`:
  `quiet_hours_start="00:00"`, `quiet_hours_end="08:00"`）。
- **主动 cron**：`decide_proactive()` 窗口外返回
  `QUIET_HOURS`，数据脚本无输出，零 token。原默认 23:00–08:00，按莫少
  要求（"早上八点到晚上24点"）改成 00:00–08:00。
- **追问 cron 不会免费获得静默闸。** `poll_followup()` 不懂静默时段——
  不设闸，02:00 到点的追问会在半夜发出去。闸加在 bridge 的
  `followup_tick()`：静默时段内到点（或 60 秒内），把该段**推迟到下一个
  活跃窗口起点**（`_postpone_followup`，下一 08:00）并返回 `None`，
  不 claim 不 commit。
- 关键设计：推迟，不丢弃。直接跳过 tick 会让该段越过 grace 被判
  `MISSED_STAGE`；推迟 `next_stage_at` 到 08:00 保住循环，用户醒来收到
  自然的"早上好，醒了吗？"而不是丢段。
- 测试注意：不能依赖墙钟。种"1 分钟前到点"的段在静默时段会被 POSTPONE
  而不是 claim，断言会 flaky。两种手法：(1) 临时 monkeypatch
  `hb._in_quiet_hours = lambda *a, **k: False`；(2) 静默闸测试按
  `now_local.time() < quiet_end` 分支——静默段断言推迟行为，非静默段直接
  断言 `_in_quiet_hours` 谓词。

## 8. Cron 投递：LIVE 路径绕过气泡分段（2026-08-08 发现）

- `cron/scheduler.py::_deliver_result()` 两条路径：
  - **LIVE adapter 路径**（gateway 运行时 = 正常生产）：整块文本一次性走
    `DeliveryRouter._deliver_to_platform()` → 主动/追问 cron 消息是一条
    合并消息，没有气泡。
  - **Standalone 路径**（gateway 挂掉时的回退）：`humanpulse*` 任务名 +
    `_HUMANPULSE_BUBBLE_SENDER` → `send_human_reply()` 的气泡闸在这里。
    补丁看着在文件里，但几乎从不执行。
- 症状（莫少 08-08 报告）：主动消息挤在一个框里，正常回复却正确气泡。
  先查 cron 投递走了哪条路径，别急着怪气泡 bridge 或 `split_reply_bubbles()`。
- 修法：LIVE 路径的 `if text_to_send:` 块（`router._deliver_to_platform(...)`
  调用处）镜像 standalone 闸——`humanpulse*` 且 `_HUMANPULSE_BUBBLE_SENDER`
  可用且平台是 QQBOT/WEIXIN 时，调度一个协程调 `send_human_reply()`，
  send_one 回调把每个气泡走**同一个** `router._deliver_to_platform(
  route_target, bubble, route_metadata)`（保留 topic/线程路由，不落回
  `_send_to_platform`）。气泡发送器必须经 `safe_schedule_threadsafe` 跑在
  gateway loop 里。
- 验证：`verify_humanpulse.py` 断言 LIVE 分支存在（查 `_live_bubble_send`、
  `send_coro`、standalone `asyncio.run(coro)`）。

## 9. 重复投递：LIVE 未确认 → standalone 重发（2026-08-09 修复）

- 症状：莫少收到两条一模一样的主动消息（"为什么你连续说了两句一样的话"）。
- 根因：`_deliver_result()` 把未确认的 LIVE 投递当失败，回退 standalone
  又发一遍相同文本。日志证据：
  ```
  cron.scheduler: Job '<id>': live adapter send to qqbot:<session> returned
  unconfirmed result (None, error=no response from adapter), falling back to standalone
  ```
- 触发：QQBot WebSocket 会话挂掉（当天日志有 `WebSocket closed: code=4009
  reason=Session timed out`）。LIVE 在超时前拿不到 ACK，scheduler 判失败，
  standalone 再发 → 重复。
- 排查链（别先怪生成）：1) `~/.hermes/logs/agent.log` grep
  `returned unconfirmed result`；2) cron 输出目录运行记录只有一份（生成只
  跑一次，重复发生在投递层）；3) state.json 只加一次 count（脚本没双跑）；
  4) gateway.log 找附近的 4009/session-timeout。
- 修法（已按莫少批准"总之修复"落地到 site-packages `scheduler.py`）：
  LIVE 路径遇未确认结果时记 `live_unconfirmed_hash = hash(text)` 再放行；
  standalone 块开头若哈希相同，记 "skipping standalone resend" 并
  `continue`。取舍：LIVE 真失败会丢这一条（比必现重复便宜，下个 tick 续上）。
- 注意：这个补丁在 site-packages（不在 patch_gateway.py 模板里），
  `hermes update` 会清掉，需要手动重打（grep `live_unconfirmed_hash`）。
