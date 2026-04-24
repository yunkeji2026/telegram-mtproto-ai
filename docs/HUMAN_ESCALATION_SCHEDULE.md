# 人工转接 · 排班与例外

## 周模板 `work_hours`

- 键：`mon` … `sun`（大小写不敏感亦可）。
- 值：`[["HH:MM","HH:MM"], ...]`。
- **跨午夜**：若 `start > end`（如 `["22:00","06:00"]`），表示**当日晚间**至**次日早间**（次日早间由「前一日」规则承接，无需在次日再写一段）。

## 按日例外 `work_exceptions`

- 键：本地时区下的 **`YYYY-MM-DD`**。
- `[]` 或 `null`：**该日全天不营业**，并取消使用前一日跨午夜在周模板上的「早间承接」（若前一日也在例外中已定义）。
- 非空数组：该日**仅使用**这些区间（覆盖当日周模板）；可含跨午夜（语义同周模板）。

## 时区

- 使用 `human_escalation.timezone`（IANA，如 `Asia/Shanghai`）。

## 与 `duty_mode`

- `schedule` / `schedule_or_manual` / `schedule_and_manual` 会调用 `is_within_work_hours(..., work_exceptions)`。

## API

- `GET /api/human-escalation/verify`（需登录）  
  返回当前进程内 **`config_manager.config["human_escalation"]`** 的摘要：`helper_loaded` / `store_loaded`（Bot 进程内为 `true`）、`effective`（enabled、阈值、时区、agents 数量等）。  
  **用途**：保存「转接配置」后点设置页 **⑨ → 验证配置已加载**，确认内存与 YAML 已写入且 Bot 侧会 `reload_config` 同一 dict（测试环境 `telegram_client=None` 时 helper 为 `false` 属正常）。

- `GET /api/human-escalation/schedule-status`（需登录）  
  返回：`in_schedule`、`manual_shift`、`duty_effective`、`minutes_until_next_open` / `minutes_until_next_close`（**两阶段**粗估：先按分钟扫 `schedule_estimate_fine_horizon_hours` 近端窗口，再按 `schedule_estimate_step_minutes` 粗步进至最长约 7 天）、`schedule_estimate_fine_horizon_hours`、`schedule_status_cache_ttl_sec`、`schedule_estimates_cached`（本请求是否命中估计缓存）、`local_time` 等。

- **估计缓存**：`schedule_status_cache_ttl_sec`（默认 30，0=关闭，最大 300）内，对「是否在班 + 下一开/关 + `active_teams`」按 **配置哈希 + UTC 分钟桶** 去重；`manual_shift` / `duty_effective` / `local_time` 仍每次现算。保存 `human_escalation` 配置后会清空缓存。

- `GET /api/human-escalation/mention-round-robin`（需登录）  
  只读调试：全局轮询计数 `global_idx`，以及最近更新的 per-chat 行 `per_chat`（`chat_id` / `idx` / `updated_at`）。

## 排查：明明重复多问却没有 @ 人工

1. **看 `logs/app.log` 里 `ai_chat_assistant.human_escalation` 行**：达到阈值后会打 `人工转接检查: 已达重复阈值`，若未追加会打 `人工转接未追加: …`（原因：冷却 / 无客服 / 非值班 / mention 为空）。历史上模块曾用 `src.utils.human_escalation` 作为 logger 名，**与主程序挂在 `ai_chat_assistant` 上的 FileHandler 不同树**，导致这些 INFO **进不了 app.log**，看起来像「没有任何转接日志」——已在代码中改为 `ai_chat_assistant.human_escalation`。
2. **本轮没有 AI 正文**：`skills.cooldown.per_content`（或 `per_user`）命中时 Skill 会「跳过回复」→ 无 `reply_final` → **不会**追加 @。连发同一句测转接时建议 **`per_content: 0`**。
3. **转接冷却**：默认 **`escalation_cooldown_scope: per_normalized_question`**（SQLite 表 `escalation_cooldown_by_norm`）——仅对**同一归一化问句**在 `cooldown_sec` 内抑制再次 @；用户换一句新问题并重复够阈值后，**仍可再次 @**，避免「A 问题刚 @ 过，B 问题也被全局冷却」的体验问题。若需恢复旧行为（同群同用户任意问题共一条冷却），设 **`per_user_chat`**。日志：`人工转接未追加: 转接冷却中 ... scope=per_normalized_question norm_tail=...`
4. **仅 username 的客服**：后缀中使用 **`<a href="https://t.me/username">@username</a>`**（HTML），比裸 `@` 更易触发客户端可点通知；生产环境仍建议配置 **`user_id`** 使用 `tg://user?id=`。
5. **Skill「追问#N」≠ 人工转接计数**：前者在 Skill/会话侧；后者在 SQLite `repeat_streak`，按**归一化后**问句维度统计，二者数字不必一致。
6. **文案被算成「不同问题」**：句末标点归一 + NFKC + 去除零宽字符，减少「看起来一样」但计数分裂。
7. **阈值与时间窗**：需 `repeat_threshold` 次、同归一化问句、且在 `repeat_window_sec` 内；成功 @ 后会 `reset` 当前问句计数。

## 用户问句与消息定位

触发转接（达到重复阈值且成功追加 @ 人工）时，可在后缀中附带**用户本轮问句**，并将问句做成**可点击链接**，便于人工一键跳到对应会话与消息：

- **公开群/频道**（有 `username`）：`https://t.me/<username>/<message_id>`
- **无 username 的超级群**：`https://t.me/c/<internal_id>/<message_id>`（`-100xxxxxxxxxx` → `xxxxxxxxxx`）
- **私聊**（正数 `chat_id`）：`tg://openmessage?chat_id=...&message_id=...`（客户端内打开）

配置项（`config.yaml` → `human_escalation`）：

- `include_user_question_link`：默认 `true`，关闭则仅保留原有 @ 文案，不附带问句行。
- `user_question_max_len`：问句单行展示最大字符（默认 `200`），超出截断并加 `…`。
- `user_question_line_prefix`：问句行前缀文案，默认 `原文：`。

## 私聊转发用户原话（`forward_user_message_to_agents`）

- 默认 **`true`**：在群内 **成功发出** 带转接后缀的 AI 回复后，客户端会用 MTProto **`forward_messages`** 把**用户触发转接的那条群消息**转发到当前被 @ 的每位客服私聊（与 `mention_mode` 一致，例如轮询时只转发给本轮那一名）。
- 客服在私聊里看到「转发自某群」的条子，点进即可回到群内对应消息节点；可与上文「问句可点链接」叠加使用。
- 设为 **`false`** 可关闭（仅群内 @ + 问句链接，不再私聊转发）。
- **不会转发的情况**：非值班仅回 `message_off_shift`、无 `user_message_id`、或客服条目既无 `user_id` 也无 `username`。转发失败会打 `人工转接: 转发至客服 peer=... 失败` 警告日志（权限/未互聊等）。

## 私聊「一键定位」跟进（`forward_private_jump_hint`）

- 默认 **`true`**：在每条 **私聊转发** 之后，再向同一客服发一条 **HTML 说明**，内含与群内转接后缀相同的 **`build_telegram_message_link` 直达 URL**（`https://t.me/.../msg`、`https://t.me/c/.../msg` 或 `tg://openmessage?...`），并附带 **内联按钮**「打开群内该条消息」。
- **原因**：部分 Telegram 客户端对「转发自群组」的预览条**不能稳定点回**具体消息；显式 `t.me/c/<internal>/msg` 与按钮在私聊里可点性更好。
- 设为 **`false`** 则仅保留转发条（旧行为）。
- 若 **转发失败** 但仍能生成链接，仍会尝试发送该条定位提示，避免客服完全丢失入口。
- 若无法生成 URL（极少见），会发文字说明请依赖转发顶栏或群邀请链接。

## `mention_mode`

- `all`：后缀中 @ 全部客服（默认）。
- `single_round_robin`：每次只 @ 一名，SQLite 轮询（`mention_round_robin` 表）。
- `single_random`：每次只 @ 一名，随机。

- **`mention_round_robin_scope`**：`global`（默认）全群/全会话共一条计数；`per_chat` 按 `chat_id` 分表 `mention_round_robin_chat`（多群独立轮询）。

## `schedule_estimate_step_minutes`

- 用于「下一开/关窗」粗估在**细扫窗口之后**的步长（默认 15，可 1–60 分钟），越小越细、步数越多。

## `schedule_estimate_fine_horizon_hours`

- 近端**按分钟**扫描的小时数（默认 24，可 0–168）。**0** 表示关闭细扫（仅用粗步进 + **回扫**）。
- 与 `schedule_estimate_step_minutes` 配合：细扫覆盖 `1..N` 分钟后，粗步进从 **对齐到步长网格** 的第一个采样点继续；一旦粗采样命中「进入/离开排班」，再向前回扫至多 `step_minutes-1` 分钟，与 `is_within_work_hours` 的**首个**变点一致（并修正旧实现中 `N` 与 `N+step` 之间的漏检）。

## `schedule_status_cache_ttl_sec`

- `schedule-status` 接口内对「是否在班 + 下一开/关 + 各分组 `active_teams`」的短时缓存（秒）。**0** 关闭；默认 **30**；上限 **300**（服务端钳制）。
- 键包含 **整段 `human_escalation` 配置的稳定哈希** 与 **UTC 分钟桶**，故跨秒连点可命中缓存，跨分钟自动换桶；手动值班与 `duty_effective` 仍每次计算。

## 分组 `agent_teams`

- JSON 数组，每项可含：`id`、`name`、`agents`（与全局 `agents` 同结构）、可选 `work_hours` / `work_exceptions`。
- 缺省字段继承全局 `work_hours` / `work_exceptions`。
- 当前时间下，对**每队**单独做 `is_within_work_hours`；命中队伍的 `agents` **合并去重**后作为 @ 对象。
- `team_fallback_to_global`（默认 `true`）：若**没有任何队伍**命中，则回退到全局 `agents` / 单客服字段；为 `false` 时无人命中则**不 @**（列表为空）。
- **`team_pick_mode`**：`union`（默认）多队命中则合并去重；`first_match` 仅取**配置顺序中**第一支命中的队伍（避免多队重叠时全 @）。

## Web 设置页：最小配置（推荐顺序）

- 页面顶部 **「同问多次 → @ 人工（最小配置）」** 单卡内按 **1～4** 步：开关与次数 → 值班（默认「始终可 @」）→ 客服表 → 单客服兜底与话术。
- **一键最小可用默认值**：启用 + 3 次/10 分钟 + `duty_mode=always`（不自动填客服）。
- **保存转接配置** 紧接最小卡下方；**⑨ 验证** 在保存条之后，便于「保存 → 验证」。

## Web 设置页：快速场景（向导）

- 在 **② 值班模式与时区** 卡片内提供 **快速场景** 按钮（始终可 @ / 仅手动值班 / 仅按周排班 / 排班或手动 / 排班且手动）。
- **始终 / 仅手动**：只改 `duty_mode`，并取消勾选「仅值班时才追加 @」（⑩），避免与显式 `duty_mode` 混淆。
- **后三项**：会设置对应 `duty_mode`，并用「工作日 mon–fri 09:00–18:00」**覆盖** ⑥ `work_hours`（若当前周模板非空会先 `confirm`）；自动切到 **完整** 视图并滚动到周模板文本框。**仍需保存**「转接配置」后生效。
- 不修改 agents、分组、例外日。

## Web 设置页：精简 / 完整视图

- 设置页「人工客服转接」顶部可选 **精简（推荐）** / **完整（分组 · 排班 · JSON）**，写入浏览器 `localStorage`（键 `he_ui_view_mode`）。
- **精简**：隐藏 **④～⑧**；**③** 保留表格，**agents 原始 JSON 编辑区**隐藏（保存时仍从表格写入 JSON，与后端一致）。
- **完整**：显示全部区块。URL 参数 **`?he=full`** / **`?he=simple`** 可强制写入上述偏好并应用（便于文档链接）。
- **⑨ 验证 / 排班自检**、**⑩ 单客服兜底** 在两种视图下均显示。

## Web 设置页分区（⑤–⑧、⑩）

- **⑤ 分队策略与排班估计**：说明条解释合并/首队、轮询范围、粗估步长、细扫、API 缓存；最后一行「缓存」跨两列对齐，避免右侧留白。
- **⑥ 周模板**：快捷生成在浅色面板内；说明周键、覆盖行为、空 `{}` 含义。
- **⑦ 按日例外**：日期与时段在面板内；`work_exceptions` 支持「检查 JSON」；文本框 **input** 约 0.45s 防抖后自动重验；**未填写** / **空 `{}`** / **含日期键** 三种成功文案互斥，避免与内容不一致。
- **⑧ @ 与展示**：分隔符与 `mention_mode` 带简短说明。
- **⑩ 兼容单客服**：「检查本区」校验用户名（勿带 `@`、无空格）与 `user_id`；用户名 **blur** 时自动去掉前导 `@`。

## 轻量可视化

- 设置页提供「工作日 mon–fri」「工作日+午休」「清空」按钮，仅**写入** `work_hours` 文本框 JSON，仍需保存配置后生效。
- **例外日快速添加**：选择日期后点「将选中日标为全天休息（[]）」，合并写入 `work_exceptions` 文本框（仍需保存）。
- **例外日时段**：同一日期下可选开始/结束时间，点「添加时段到选中日」追加 `[[HH:MM","HH:MM"], ...]`（若原为 `[]` 全天休息，会改为该时段覆盖）。
- **客服列表 agents**：表格编辑 `user_id` / `username` / `display_name`；保存「转接配置」前会自动写入 JSON；各 JSON 区块可点「检查 JSON」做前端格式校验（保存成功后会再跑一遍）。
- 「排班自检」可拉取 `schedule-status`；「轮询快照」调用 `mention-round-robin` 查看 SQLite 计数。
