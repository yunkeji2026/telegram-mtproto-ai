# 坐席工作台技术调研与架构备忘（2025–2026）

> 实施 Phase 5 前的技术选型调研。以**可落地、少运维**为优先，不引入与本项目规模不匹配的复杂度。

## 1. 实时传输：SSE vs WebSocket（结论：继续 SSE + HTTP POST 混合）

| 维度 | SSE（当前） | WebSocket |
|------|------------|-----------|
| 方向 | 服务端 → 客户端（够用） | 全双工 |
| 代理/CDN | 原生 HTTP，穿透性好 | 需 Upgrade 配置 |
| 重连 | EventSource 内置 | 需自实现 backoff |
| 坐席写操作 | 独立 POST（claim/presence/send） | 可走同连接 |
| 运维成本 | 低 | 中高（心跳、背压、粘性连接） |

**行业共识（2025–2026）**：Agent/聊天类产品默认用 **SSE 承担读路径**（token 流、状态推送、收件箱刷新），**HTTP POST 承担写路径**（取消、审批、claim、presence）。仅当需要语音全双工、协作编辑、高频 mid-stream 打断时才上 WebSocket。

**本项目决策**：
- 保持 `/api/workspace/stream`（SSE）+ EventBus 扇出
- 新增写路径：`POST /api/workspace/presence`、`POST /api/workspace/claim`
- 不在 Phase 5 引入 WebSocket（避免 nginx/ALB Upgrade 与多实例粘性负担）

参考：
- [Agent Streaming Transport 2026](https://agentmarketcap.ai/blog/2026/04/11/agent-streaming-transport-layer-websockets-sse-2026)
- [Streaming Agent Architecture in Production](https://agentmarketcap.ai/blog/2026/04/07/streaming-agent-architecture-sse-websocket-production)
- [FastAPI SSE Practical Guide](https://agentflow.10xscale.ai/blog/streaming-agent-responses-fastapi-sse)

## 2. 多坐席协作：租约锁（Lease）而非永久锁

**问题**：两人同时打开同一会话 → 重复回复、AI/人工模式互相覆盖。

**推荐模式**（来自 2026 多 Agent 协作实践）：
- **File-lock / conversation-claim**：操作前 acquire，他人看到 locked 并等待或强制接管
- **租约 TTL**：15 分钟无心跳自动释放（reaper 模式），防坐席崩溃占锁
- **事件总线**：`lock.released` / `conversation_claim` 经 SSE 广播，列表即时更新

**本项目决策**：
- `conversation_claims` 表：`(conversation_id, agent_id, expires_at)`
- 打开会话 → 自动 claim；每 60s heartbeat renew；离开/关闭 → release
- 冲突时：显示「xxx 处理中」，允许「强制接管」（主管/同账号策略可后续收紧）

## 3. Durable Session（远期，Phase 6+）

当部署**多实例** FastAPI 时，进程内 EventBus 无法跨节点扇出。升级路径：

1. **Tier 1**：SQLite/Postgres 存会话事实（已有 InboxStore）
2. **Tier 2**：Redis Pub/Sub 或 Ably 做跨实例事件中继
3. **Tier 3**：SSE relay 带 `Last-Event-ID` 断点续传（replay  missed events）

Phase 5 **不引入 Redis**；claim/presence 落 InboxStore SQLite，单实例已足够。

参考：[Streaming Infrastructure Behind Real-Time Agent UIs](https://tianpan.co/blog/2026-04-10-streaming-real-time-agent-uis-sse-backpressure-reconnection)

## 4. 可观测：web 漏斗迷你指标（Phase 5-2）

不新建重型 BI；在工作台顶栏展示今日：
- web 会话数 / 入站消息数
- AI vs 人工 outbound 比
- 引流注入次数 / LINE 转化阶段计数

数据源：InboxStore（`platform=web`）+ ContactsStore（`channel=web` journey 阶段分布）。

## 5. Phase 5-3：入站自动翻译（已实施）

**策略**：仅在坐席打开会话（`GET /api/unified-inbox/thread`）时按需翻译，**不在 ingest 全量触发**。

| 控制项 | 默认 | 说明 |
|--------|------|------|
| `enabled` | `false` | 灰度开关 |
| `max_per_thread` | 8 | 单次最多译 N 条入站 |
| `max_chars` | 400 | 超长跳过 |
| `source_langs` | `[]` | 空白名单=所有非中文 |

译文写入 `InboxStore.messages.translated_text`，复用 TranslationService L1/L2 缓存 + 翻译记忆。

## 5b. pre-chat 留资 + 身份去重合并（Phase 5-4）

聊天开始前的留资表单（姓名/手机/邮箱…）采集客户强标识，并用其跨渠道**去重合并身份**：
老客户曾用同一手机/邮箱在 LINE/Messenger 联系过 → 自动并入同一 `Contact`，坐席档案右栏
显示「老客户」徽标 + 关联渠道。

**存储**：新增 `contact_attributes(contact_id, attr_key, attr_value, updated_at)` 表
（`CREATE TABLE IF NOT EXISTS`，老库自动建无需 ALTER），按 `(attr_key, attr_value)` 建索引支持反查。

**合并策略（保守，复用现有 relink/merge_review 原语）**：

| 命中情况 | 动作 | 置信度 |
|----------|------|--------|
| phone/email 在**其它单个** Contact 唯一命中 | `relink_channel_identity`（auto） | 0.95 |
| 多个 Contact 命中同一标识 | 入 `merge_review_queue`（人工） | 0.6 |
| 无命中 | 仅写属性到当前 Contact | — |

**入口**：`POST /chat/api/profile`（公网，HMAC token；走 `/chat/*` CSRF 豁免）→
`ContactGateway.capture_lead(...)`。手机号规整保留 `+` 与数字、去分隔符；邮箱小写并粗校验。
已留资访客重连时 `session` 不再下发表单（`prechat.enabled=false`）。

## 5c. 坐席手动合并 / 拆分 / 审核队列（Phase 5-5）

5-4 打通了「自动合并 + 多命中入队」，5-5 补上坐席侧可视化闭环：

**手动合并 / 拆分**（工作台档案右栏「身份管理」）：
- `GET /api/workspace/contacts/overview` 返回当前会话 Contact 的渠道身份 + 按 phone/email
  共享反查的可合并候选；
- `POST /api/workspace/contacts/merge` 复用 `relink_channel_identity(linked_via=manual)`；
- `POST /api/workspace/contacts/split` 新增 `ContactStore.split_channel_identity` 原语
  （把误并身份拆出为独立 Contact+Journey；孤岛身份拒绝拆分；两侧落 split 审计事件）。
  注意：journey_events 按 journey 而非 ci 记录，拆分**不回搬历史事件**，只修正归属与未来归因。

**审核队列**（工作台顶栏「待审合并 N」徽标 + 弹窗）：
- `GET /api/workspace/merge-reviews` 列出 pending，并附候选/目标两侧档案摘要供对比；
- `POST /api/workspace/merge-reviews/{id}` `{action: approve|reject}` 复用
  `MergeService.approve_review/reject_review`（approve 内含幂等短路 + relink 失败保 pending 可重试）。

所有端点经 `/api/workspace` 前缀，已在 `admin._agent_api_allowed` 放行给 `agent` 角色。

## 5d. Contact 360 全景视图（Phase 6-1）

独立页 `GET /workspace/contact/{contact_id}`，把一个客户的所有信息聚成一页：

- **跨渠道消息时间线**：遍历 Contact 名下每个渠道身份 → `conv_id(channel, account, external_id)`
  → `InboxStore.list_messages`，合并按 `ts` 升序，总量上限 `msg_limit`（默认 60，最近 N 条，
  避免大客户拉全量）。每条按渠道着色 + 显示译文（若有）。
- **留资 + 渠道身份 + 事件历史**（读 `journey_events`：合并/拆分/引流轨迹）。
- **任意 Contact 搜索合并**：`GET /api/workspace/contacts/search`（复用 `ContactStore.search_contacts`，
  支持名称/ID/渠道 external_id）→ `POST /api/workspace/contacts/merge-contact`（contact 级，
  把 source 全部身份并入 target，复用逐个 `relink_channel_identity`，最后回收孤岛）。
- 工作台档案右栏「查看全景」直达本页。

API：`GET /api/workspace/contact/{contact_id}` 一次返回 `{contact, timeline, events, candidates}`。

## 5e. 客户列表 / CRM 入口（Phase 6-2）

以「客户为中心」的浏览入口，补齐只能从会话反查 360 的缺口。

- 页 `GET /workspace/contacts`；顶栏「会话 / 客户」导航（`workspace_base.html`，全工作台页共享）。
- API `GET /api/workspace/contacts/list`：分页 + 阶段(`stage`)/留资(`has_lead`)/关键词(`q`) 筛选
  + 漏斗阶段汇总。底层 `ContactStore.list_contacts_overview` 用**单次 JOIN + 子查询**返回
  contact+journey+渠道(GROUP_CONCAT)+是否留资，**避免 N+1**。
- 列表行渠道徽标 + 漏斗阶段 + 亲密度，点击直达 Contact 360。

**时间线分页游标（6-1 的优化补强）**：
- `InboxStore.list_recent_messages(limit, before_ts)` 取**最近** N 条（与 `list_messages` 取最旧相反），
  修正了 6-1「大客户时间线漏掉最新消息」的隐患；
- `GET /api/workspace/contact/{id}?before_ts=` 向更早翻页，前端「加载更早消息」按钮拼接，保持视口位置。

**事件文案可读化**：`_EVENT_LABELS` 把 `lead_captured`/`channel_identity_merged`/… 映射为中文（建档/
客户留资/身份已合并…），360 事件历史显示人类可读标签。

## 5f. 客户备注 / 标签 / 跟进提醒（Phase 6-3）

把 CRM 从「能看」升级到「能记、能管」，形成跟进闭环。

- **存储**：
  - 备注复用 `contact_attributes` 的 `note` 键；
  - 跟进时间用 `contacts.follow_up_at`（INTEGER 秒，0=无；ALTER 迁移 + 部分索引 `WHERE follow_up_at>0`），
    便于直接 SQL 排序/筛选/计数，比塞进 KV 文本再 CAST 更稳更快；
  - 标签独立成 `contact_tags(contact_id, tag)` 多对一表（PK + `idx_tags_tag`），
    支持**标签精确筛选**与**聚合自动补全**，优于把多标签塞一个 KV 值。
- **Store 方法**：`set_follow_up` / `set_contact_tags`(全量替换+去重) / `get_contact_tags` /
  `get_tags_for_contacts`(批量,避免 N+1) / `list_all_tags`(聚合计数) / `count_due_follow_ups`。
  `list_contacts_overview` 扩展 `tag` / `follow_up`(due|any) 筛选，并把**到期跟进排到列表最前**。
- **Gateway**：`update_contact_crm(note/tags/follow_up_at)`，None=不改该项；写 `crm_updated` 事件入 journey。
  `contact_overview` 增 `note/tags/follow_up_at`。
- **API**：
  - `POST /api/workspace/contact/{id}/crm`：保存备注/标签/跟进；
  - `GET /api/workspace/follow-ups?scope=due|any`：待跟进列表 + 到期计数；
  - `GET /api/workspace/tags`：标签聚合（自动补全/快筛）；
  - `GET /api/workspace/contacts/list` 增 `tag` / `follow_up` 参数 + `due_follow_ups` 计数。
- **前端**：
  - Contact 360 加「跟进管理」编辑区（标签 chips + datalist 自动补全、跟进时间 datetime-local、备注）；
  - CRM 列表加「全部跟进 / 待跟进(到期) / 有跟进」+「标签」快筛，卡片显示标签与到期高亮；
  - 工作台顶栏新增**到期待跟进徽标**（复用 merge-review 徽标轮询模式，60s 刷新），
    点击跳 `/workspace/contacts#due` 自动预置到期筛选。

## 5g. 跟进任务化 / 预设标签库 / CRM 导出 / 筛选入 URL（Phase 6-4）

把 6-3 的「单个可覆盖跟进时间」升级为可完成、可指派、有历史的任务，并补齐标签规范化与数据导出。

- **跟进任务化**：
  - 新表 `follow_up_tasks(task_id, contact_id, due_at, note, assignee, created_by, done_at, done_by)`；
  - `contacts.follow_up_at` 降级为**派生缓存**（= 最近未完成任务到期），由 `_recompute_follow_up` 维护，
    使 6-2 的 CRM 列表筛选/排序查询零改动仍可用；
  - `set_follow_up` 重写为**去重**语义（更新最近未完成任务，无则新建；清除=完成全部），
    兼容 6-3 的 `update_contact_crm(follow_up_at=)` 调用不产生重复任务；
  - `count_due_tasks(assignee)` 支持「我的待办 vs 全部」徽标（顶栏显示 `待跟进 我的/全部`）。
- **预设标签库**：新表 `tag_library(tag, color, sort_order)`；`list_all_tags` LEFT JOIN 库色，
  并把「库里未使用」的标签以 count=0 一并返回供补全；360/列表统一用库色渲染 chips，自由标签仍可临时加。
- **CRM 导出**：`GET /api/workspace/contacts/export.csv`，复用 `list_contacts_overview` 的全部筛选条件，
  带 UTF-8 BOM 兼容 Excel，时间戳格式化为可读字符串。
- **筛选状态入 URL**：客户列表读/写 `location.search`（`history.replaceState`），刷新/分享保留筛选与页码，
  并兼容旧 `#due` 锚点；导出按钮直接拼当前筛选。
- **API**：`POST /contact/{id}/follow-up`、`POST /follow-up/{task_id}/done`、
  `GET/POST /tag-library`、`DELETE /tag-library/{tag}`、`GET /contacts/export.csv`；
  `GET /follow-ups` 增 `due_tasks` / `due_tasks_mine`。

## 5h. 「我的待办」面板 + 任务指派协作 + 延期快捷 + SSE 实时（Phase 6-5）

把 6-4 的任务从「单客户挂着」升级为坐席的每日工作流与团队协作。

- **「我的待办」页** `GET /workspace/tasks`（顶栏新增「待办」导航）：
  - API `GET /api/workspace/my-tasks?scope=mine|all&due=today|overdue|all`；
  - 底层 `ContactStore.list_open_tasks(assignee, due_before)` 单次 JOIN 带出客户名 + 渠道（GROUP_CONCAT），
    `due=today` 取「今天 23:59:59 及之前」覆盖逾期+今日。
- **任务指派协作**：
  - `reassign_task` / `gateway.reassign_follow_up_task` + `POST /follow-up/{id}/assign`；
  - 指派下拉**复用 5-1 的 `presence` 在线坐席列表**；360 与待办页均可改派。
- **延期快捷**：`snooze_task(days|due_at)` + `POST /follow-up/{id}/snooze`，
  `days` 从 `max(now, 当前到期)` 顺延（逾期任务从今天起算），按钮 +1天/+3天/+1周；改派/延期均**仅作用未完成任务**。
- **SSE 实时**：任务 add/done/assign/snooze 经 `event_bus.publish("follow_up", …)` 推送；
  stream 白名单加 `follow_up`；`workspace_base.html` 订阅后实时刷新到期徽标（及打开中的待办页），
  替代纯 60s 轮询（轮询保留为兜底）。徽标点击改为直达 `/workspace/tasks`。
- 缓存列 `contacts.follow_up_at` 在 snooze/complete 后由 `_recompute_follow_up` 维护，列表/排序零改动。

## 5i. 会话↔CRM 双向打通 + 工作台仪表盘（Phase 6-6）

把割裂的「会话工作台」与「CRM/任务」缝合，并给团队一个概览。

- **会话内联跟进任务**：会话右侧档案区（`unified_inbox.html`）在已关联 contact 时内嵌跟进任务
  （看/建/完成/+1天），复用 6-4/6-5 端点；轻量只读端点 `GET /api/workspace/contact/{id}/tasks`。
- **会话列表逾期红点**：`/api/unified-inbox/chats` 批量给会话挂 `contact_id` + `follow_up_overdue`：
  `ContactStore.resolve_contacts_by_external(pairs)` 一条 `IN(...)` 查询解析 (channel, external_id)→contact，
  `overdue_contact_ids()` 单查缓存列，**零额外表、零 N+1**；前端列表项渲染红点。
- **工作台仪表盘** `GET /workspace/dash`（顶栏「概览」）+ `GET /api/workspace/dashboard`：
  今日新客户/留资/引流（`count_contacts_created_since` / `count_events_since`）、
  到期跟进（全部/我的）、坐席负载（`agent_task_load`：每人未完成 + 逾期）、漏斗分布、
  web 漏斗快照（复用 5-2 `web_funnel_snapshot`）。
- **踩坑**：新页面模板原名 `dashboard.html` 与既有**后台运营仪表盘**同名被覆盖（`test_web_alert` 抓到），
  改名 `workspace_dashboard.html`；提醒：新模板务必避开既有命名。

## 5j. SLA 未回复时长 + 7 日趋势 + 仪表盘聚合优化（Phase 6-7）

让"该先回谁"和"团队走势"一眼可见，全部复用既有事实源、不新增表。

- **SLA 当前未回复时长**：`InboxStore.last_message_dirs(cids=None)` 用
  `messages JOIN (SELECT conversation_id,MAX(ts) GROUP BY ...)` 一条查询取每会话**末条**消息的
  方向+时间；末条为 `in` ⇒ `unanswered_sec = now - ts`，超 `_SLA_WARN_SEC`（默认 1800s）标 `sla_breach`。
  - 会话列表 `/api/unified-inbox/chats` 给可见会话批量挂 `unanswered_sec` / `sla_breach`，
    前端 meta 区渲染 `⏱` 等待时长（超时红色加粗，`fmtWait` 秒/分/时/天）。
  - 仪表盘对最近 ≤500 会话聚合 `sla.waiting`（等待中）/ `sla.breaching`（超时），新增两张卡片。
  - **为何不存列**：方向已在 messages，按需 JOIN 即可；存"末条方向"列会引入写路径耦合与回填迁移，得不偿失。
  - **首响时长**：当前只做"当前未回复"（最可执行）；历史首响均值/分位属重计算，留待趋势看板二期。
- **7 日趋势**：`ContactStore.count_contacts_by_day` / `count_events_by_day` 用
  `strftime('%Y-%m-%d', ts, 'unixepoch', 'localtime')` 按本地日 `GROUP BY`；仪表盘补满 7 格
  渲染内联 SVG 折线（新客户 / 留资，带 `<title>` 悬浮值），零前端图表依赖。
- **仪表盘聚合优化**：今日 `lead_captured` + `handoff_sent` 两次计数合并为
  `count_events_since_multi(types, since)`（一条 `IN(...) GROUP BY event_type`），减一次 DB 往返。
  其余计数分散于不同表（contacts / follow_up_tasks / journeys），强行单查询收益小、可读性差，未合并。
- **前端组件抽取（crm-widgets.js）**：static 挂载（`/static`←`src/web/static`）确认可行，但跟进任务卡 +
  标签编辑横跨 360/待办/会话三模板、各有上下文差异；纯重构、无功能收益且无浏览器回归，
  本期**主动不做**（避免过早抽象引入回归面），列为下一期可选项。

## 5k. 首响时长二期 + SLA 可配置分级 + 趋势扩展（Phase 6-8）

把"反应快不快"量化到可考核，并让趋势可调时间窗。

- **首响时长（first response）**：`InboxStore.first_response_rows(since)` 用 `WITH firstin AS (MIN(ts)
  WHERE direction='in' GROUP BY conversation_id)` 取每会话首条入站，再相关子查询取**其后**首条出站
  `MIN(ts) WHERE direction='out' AND ts>=t_in`。返回 `{cid, t_in, t_out|None}` 纯数据，
  聚合（今日均值/达标率 + N 日达标率趋势）在路由内存完成，保持查询单一职责。
  - 仪表盘新增「今日平均首响」「今日首响达标率」卡 + 「首响达标率趋势」折线（0–100% 固定刻度 `sparkPct`）。
  - **达标口径**：首响 ≤ `sla_warn_sec` 记达标；未回复会话不计入分母（只统计已响应）。
  - **为何 t_out 要 ts>=t_in**：排除客户进线前坐席的主动消息（如群发/打招呼），避免负首响/虚高达标。
- **SLA 可配置 + 分级**：`config.inbox.sla_warn_sec`（默认 1800）/`sla_crit_sec`（默认 7200），
  `_sla_cfg(request)` 读取并兜底（crit<warn 时拉平）。会话列表 `sla_level` ∈ `''/warn/crit`，
  前端 ⏱ 灰→橙→红加粗；仪表盘 SLA 卡拆「待回复 / SLA 超时(≥warn) / 严重超时(≥crit)」。
- **趋势扩展**：折线加第三条「引流(转化, handoff_sent)」；仪表盘右上 7/30 天下拉，
  `GET /api/workspace/dashboard?days=30` 后端按 `span` 生成对齐日轴（缺日补 0），前端切换即重取。
- **复用既有**：转化/留资按天复用 6-7 的 `count_events_by_day`；首响/SLA 全部走 inbox 消息，零新增表。

## 5l. SLA 主动告警（顶栏徽标 + SSE 实时推送 + 下钻跳转）（Phase 6-9）

把 SLA 从"打开仪表盘才看到"升级为"坐席无论在哪个页面都被动收到提醒"。

- **告警源** `GET /api/workspace/sla-alerts` + `_sla_alert_snapshot()`：扫最近 ≤500 会话末条方向，
  返回 `waiting / breaching(≥warn) / critical(≥crit)` 计数 + 严重超时会话清单
  （`conversation_id/platform/chat_key/name/wait_sec`，按等待时长倒序，截 50）。
- **顶栏徽标**（`workspace_base.html`，所有工作台页继承）：轮询 30s，有严重→红「SLA 严重 N」、
  仅警告→橙「SLA 超时 N」，hover 列出 top8；点击把首个会话写 `sessionStorage.ws_focus_conv` 跳 `/workspace`。
- **SSE 边沿触发**：复用既有 `/api/workspace/stream` 生成器，**不新增 main.py 后台 loop**——
  在连接建立 + 每次 30s 心跳时跑 `_sla_pushes()`：对"新转入严重"的会话发 `sla_alert` 帧（每连接 `_sla_seen` 去重，
  会话恢复后移出 seen 可再次告警）。前端收到 → 右下角红色 toast（8s 自动消失）。
  - **为何不开全局后台任务**：SSE 生成器本就有 30s 心跳节拍，挂在上面天然按"在线坐席"作用域、
    零 lifespan 改动、零新依赖；代价是每连接各算一次（坐席数少，可接受）。
- **下钻跳转**：`unified_inbox.html` 的 `load()` 读 `sessionStorage.ws_focus_conv`（一次性）→
  在 `filtered` 里定位 index → `UI.selectChat()` 直接打开该会话。
- **复用既有**：SLA 阈值/分级沿用 6-8 `_sla_cfg`；快照与会话列表 `sla_level` 同源（`last_message_dirs`），口径一致。

## 5m. 坐席维度 SLA 归属（按活跃 claim）（Phase 6-10）

回答"谁手上压着待回复/超时会话"——团队管理与坐席自查都用得上。

- **数据源选择（关键决策）**：会话↔坐席归属用 **活跃 claim**（`list_conversation_claims`，lease 有效、
  过期自动 purge，**可靠**），而非 messages（无 agent 列）或历史 claim（已删）。
- **实现**：仪表盘 SLA 块在遍历 `last_message_dirs` 时，用 claim_map 把每个待回复会话归到
  `agent_id`（无 claim → `""`/「(未认领)」桶），统计每人 `waiting/breaching/critical`，
  按 `(-critical,-breaching,-waiting)` 排序输出 `sla_by_agent`。仪表盘新增「坐席 SLA 归属」分区
  （严重红 / 超时橙 / 待回复灰）。
- **为何不做"历史首响 by 坐席"**：首响出站消息在多平台经 RPA 旁路 ingest（`is_self`→out），
  **发送时不带 agent 身份**；仅 web 分支直接落库。要可靠归属需给所有平台出站打 agent_id（改发送路径 + 迁移），
  且 RPA ingest 与 send 路径 message_id 不一致——本期**主动不做**，避免在不可靠归属上建绩效指标。
  升级路径：发送路径统一打 `sent_by_agent` 列后，再做历史首响/解决时长的坐席绩效榜。

## 5n. SLA/首响明细下钻（仪表盘聚合数字 → 可操作清单）（Phase 6-11）

把仪表盘上的"数字"变成"点开就能处理的会话清单"，闭环到具体会话。

- **明细端点** `GET /api/workspace/sla-detail?scope=&agent=` + `_sla_detail()`：
  - `scope`: `waiting`（全部待回复）/ `breaching`（≥warn）/ `critical`（≥crit）/ `unresponded`（今日进线未回复）；非法值回落 `critical`。
  - `agent`: 传入按 claim 坐席过滤（`""`=未认领，`None`=不过滤）。
  - 返回 `{scope, count, items[]}`，item 含 `conversation_id/platform/chat_key/name/wait_sec/level/agent_id/agent_name`，按等待倒序截 200。
  - `unresponded` 复用 6-8 `first_response_rows(midnight)` 取 `t_out is None`；其余复用 `last_message_dirs` + 阈值；坐席归属复用 6-10 claim_map。
- **仪表盘下钻 UI**：SLA 三卡（待回复/超时/严重）与「今日首响达标率」卡、坐席 SLA 归属行**均可点**→
  弹层列清单，顶部 4 个 scope tab 可切换（保留当前 agent 过滤）；每条点 → `sessionStorage.ws_focus_conv` 跳 `/workspace`
  自动聚焦会话（复用 6-9 下钻通道）。点击坐席行 = `scope=waiting&agent=<id>`。
- **复用既有**：阈值/分级 `_sla_cfg`、跳转 `ws_focus_conv`、归属 claim_map 全部沿用，无新表、无新前端依赖。

## 5o. 出站坐席归属基建 + 历史首响坐席绩效（Phase 6-12）

补上 6-10 标注的数据缺口：让"历史首响 by 坐席"成为可靠指标。

- **核心设计：发送打点而非改消息行**。新表 `agent_sends(id, conversation_id, agent_id, agent_name, ts)`。
  `/api/unified-inbox/send` 五个平台分支成功后调 `record_agent_send(conv_id(...), agent)`（`_session_agent` 取身份）。
  - **为何不在 messages 加 sent_by_agent 列**：LINE/WhatsApp/Messenger 出站经 RPA **稍后**旁路 ingest，
    发送瞬间消息行还不存在；且 RPA ingest 与 send 路径 message_id 不一致，回填不可靠。
    发送即打点（与 ingest 解耦）天然覆盖全平台、不依赖出站何时落库。
  - **只记人工发送**：AI 自动回复不经此端点 → 无 marker，绩效只统计真人首响（正是所求）。
- **归属查询** `agent_first_responses(since)`：`firstin(MIN(ts) in)` + 关联子查询取**其后首条** agent_sends
  （`ts>=t_in ORDER BY ts ASC LIMIT 1`）的 ts/agent。resp_ts=None ⇒ 无人工首响（AI 或未回复）。
- **仪表盘「坐席首响绩效」**：按 agent 聚合首响数 / 平均首响 / 达标率（≤warn），达标率绿/橙/红。
  与 6-10「坐席 SLA 归属（当前 claim）」并列：一个看历史响应速度、一个看当前积压。
- **复用既有**：`conv_id`、`_session_agent`、`first_response_rows` 同构（把"首条出站"换成"首条 agent_sends"）；阈值沿用 `_sla_cfg`。

## 5p. 前端共享组件抽取 crm-widgets.js（Phase 6-13）

把散落多模板的纯工具函数收敛到一处，降维护成本（技术债收敛，纯重构）。

- **静态资产** `src/web/static/workspace/crm-widgets.js` → 挂 `window.CRMW`：
  `esc / fmtDur / fmtWait / fmtWaitMin / spark / sparkPct / toast`（零依赖、纯函数）。
- **挂载点**：`/static`←`src/web/static`（`admin.py` 已 `app.mount`，目录存在即挂）；
  `workspace_base.html` 在 head（CSRF 脚本后）`<script src="/static/workspace/crm-widgets.js">`，
  **所有工作台页继承 base** → 自动获得，子页 `{% block content %}` 内联脚本运行时 CRMW 已就绪。
- **零调用点改动**：各模板原同名函数改为**一行委托**（`function fmtWait(s){return CRMW.fmtWait(s);}`），
  调用处不动，回归面最小；删除 base 的 `wsToast`/`fmtWaitMin` 全身、dashboard 的 `spark/sparkPct/fmtDur/esc` 全身、
  inbox 的 `fmtWait` 全身，合计减约 60 行重复。
- **守卫测试**：`TestCrmWidgetsAsset` 断言 JS 暴露全部 API + base 模板确实引用，防误删/改名导致运行期 CRMW undefined。
- **取舍**：仅抽**纯函数**；下钻弹层/SSE 处理等含页面状态的逻辑留在各页（抽出反而要传一堆上下文，得不偿失）。

## 5q. 坐席首响绩效下钻 + 窗口切换（Phase 6-14）

把 6-12 的坐席绩效"数字"变成"可核查的会话清单"。

- **明细端点** `GET /api/workspace/agent-frt-detail?agent=&days=` + `_agent_frt_detail()`：
  复用 6-12 `agent_first_responses(since)`，按 `agent_id` 过滤、`resp_ts` 非空，
  join conv 取名/平台，算每会话 `frt_sec` + `attained`（≤warn），按首响时长倒序截 200。
  `days` 7/30 与仪表盘窗口一致（`since=midnight-(span-1)*86400`）。
- **仪表盘下钻**：「坐席首响绩效」每行可点 → 复用 6-11 弹层（无 scope tab）列该坐席窗口内首响会话，
  达标绿/超时红 ⚠，点条目 → `ws_focus_conv` 跳会话；弹层标题带当前 `days` 窗口。
- **窗口联动**：下钻读 `curDays()`（仪表盘右上 7/30 下拉），与绩效榜/趋势同窗口，口径一致。
- **复用既有**：`agent_first_responses`、`_sla_cfg`、弹层 DOM、`ws_focus_conv` 跳转全部沿用，无新表、无新前端依赖。

## 5r. 解决(引流)时长指标（Phase 6-15）

在"首响速度"之外补上"结案/引流时长"维度，构成「快 + 结」双维。

- **定义**：每 journey「首条 `msg_in` → 其后首个 `handoff_sent`（引流已发）」的时长。
  本仓库漏斗目标=引流，故以 `handoff_sent` 为解决里程碑（`resolve_event` 可配，默认即此）。
- **单库实现** `ContactStore.resolution_stats(since, resolve_event="handoff_sent")`：
  CTE `firstin` 取每 journey 首条 `msg_in`，子查询取 `ts>=t_in` 的首个解决事件 →
  返回 `[{journey_id, t_in, resolved_ts|None}]`。**早于进线的 handoff 不计**；
  `resolved_ts=None` ⇒ 尚未解决。聚合（均值/趋势）交路由层，保持 store 单一职责。
- **全部落在 journey_events 单表**，与首响（inbox.messages）解耦，无新表、无跨库 join。
- **仪表盘**：`api_workspace_dashboard` 按"解决日"分桶 → `resolution.today_resolved` /
  `today_avg_sec` 两张卡 + `res_trend`（窗口内每日 `avg_min`/`count`）折线（`spark` 自适应）。
  趋势随右上 7/30 窗口联动；无解决数据时提示"需漏斗推进到引流已发"。

## 5s. 坐席经营日报 / CSV 导出（Phase 6-16）

把 6-7~6-15 积累的指标从"只能看仪表盘"收口成"可交付物 + 历史回看"。

- **端点** `GET /api/workspace/daily-report.csv?days=7|30`：逐日一行，列含
  新客/留资/引流(转化) + 首响(条数/已响应/均值秒/达标率%) + 解决(条数/均值秒)，
  末尾一行「合计」（首响/解决均值按已响应/已解决数加权）。Excel UTF-8 BOM。
- **共用聚合** `_daily_report_rows(request, span)`：完全复用既有 store 方法
  （`count_contacts_by_day` / `count_events_by_day` / `resolution_stats` /
  `first_response_rows`），按本地日期分桶，contacts/inbox 任一缺失则该段补 0，
  不抛错。无新表、无新 store 方法。
- **入口**：仪表盘右上「⬇ 导出日报 CSV」按钮，随 7/30 窗口下拉联动导出。
- **历史回看**：CSV 天然含 N 天逐日明细；文件名带窗口与生成日期便于归档。

## 5t. SLA 告警 → 跟进任务闭环（Phase 6-17）

把 6-9 的「SLA 告警」与 6-4 的「跟进任务」打通：发现超时 → 一键建待办 → 指派闭环。

- **端点** `POST /api/workspace/sla/create-task`：body 接 `platform`+`chat_key`
  或 `conversation_id`（缺则从 `platform:account:chat_key` 解析）+ 可选
  `wait_sec`/`due_in_hours`(默认 2)/`assignee`(默认本人)/`note`。
  经 `resolve_contacts_by_external` 把会话解析为 contact，note 预填
  「SLA 超时未回复 N 分钟，请尽快跟进」+ 自定义补充，复用
  `gateway.add_follow_up_task`；成功发 `follow_up` SSE 事件刷新待办徽标。
  contact 未建档返回 `contact_not_found`（不抛错，前端 toast 提示）。
- **入口**：仪表盘 SLA/首响明细弹层每行加「生成跟进」按钮（`stopPropagation`
  不触发跳转）；按 index 取 `window.__slaItems` 避免内联 JSON 转义，
  建后按钮变「✓ 已建」+ toast。
- **复用既有**：会话→contact 解析、`add_follow_up_task`、`follow_up` 事件总线、
  `CRMW.toast`，无新表、无新 store 方法。

## 5u. 告警个性化：每坐席 SLA 阈值 + 免打扰 + 静音（Phase 6-18）

让 6-9 的一刀切告警可按坐席自调，避免无关告警淹没。

- **存储** `inbox.agent_prefs` 表（`warn_sec`/`crit_sec` 0=沿用全局、`muted`、
  `dnd_start`/`dnd_end` 本地分钟 0-1439 / -1=关）+ `get/set_agent_prefs`。
- **合并** `_agent_sla_cfg(request)`：全局 `_sla_cfg` 叠加当前坐席覆盖，
  返回 `{warn, crit, muted, dnd}`；`_dnd_active()` 支持跨午夜（start>end）。
- **静默口径**：`_sla_alert_snapshot` 按个人阈值算严重；静音或免打扰时段
  → `items=[]` + `quiet=true`（计数仍返回供仪表盘参考），徽标隐藏、
  SSE 自然无 toast（无 items 即无帧）。**仅影响告警，不改团队级列表/仪表盘配色**
  （团队统一 SLA 标准仍走全局 config）。
- **端点** `GET/POST /api/workspace/prefs`：回显（含全局默认 + effective）/保存。
- **入口**：顶栏 ⚙ 打开轻量设置面板（阈值分钟 + `<input type=time>` 免打扰 +
  静音勾选），保存即 `refreshSla()`。

## 5v. 坐席个人日报 CSV（Phase 6-19）

在 6-16 团队日报上加 `?agent=` 维度，把 6-12 的出站归属 + 6-5 的任务产出
收口成个人绩效可交付物。

- **复用同一端点** `GET /api/workspace/daily-report.csv?days=&agent=`：
  传 `agent` → 走 `_agent_daily_report_rows`，列改为
  首响数/均值/达标率 + 发送量 + 完成任务数，末行合计（首响均值/达标率加权）。
- **新增按日聚合**：`inbox.count_agent_sends_by_day(agent, since)`（发送量）+
  `contacts.count_tasks_done_by_day(done_by, since)`（任务产出），均 `strftime`
  本地日分桶，无相关子查询。
- **首响按"响应日(resp_ts)"归属**：即坐席当日实际动作日（区别于团队日报
  6-16 按进线日 t_in），`frt=resp_ts-t_in`。
- **入口**：仪表盘「坐席首响绩效」每行加 ⬇ 链接，`stopPropagation` 不触发下钻，
  随 7/30 窗口导出该坐席个人日报。无新路由（同 path 加 query）。

## 5w. 告警升级策略（团队安全网）（Phase 6-20）

补上 6-18「个人可静默」后的风险敞口：被放下的严重会话不能就此无人管。

- **快照** `_escalation_snapshot(request)`：**全局口径**（用全局 `_sla_cfg`，
  不叠加查看者个人覆盖、不受个人静默影响），列出"≥全局 crit 且无人有效处理"
  的会话 + 原因：
  - `unclaimed` 无人认领；
  - `holder_offline` 认领坐席不在线（presence 不在 `presence_stale_sec` 窗口内
    或状态非 online/busy）；
  - `holder_quiet` 认领坐席 muted 或处于免打扰。
- **端点** `GET /api/workspace/escalations`。
- **入口**：顶栏紫色「升级 N」徽标（独立 30s 轮询，**绕过个人静默**，点跳首条）+
  仪表盘「⚠ 升级」区段（原因标注 + 跳转 + 复用 6-17「生成跟进」一键建待办）。
- **为何不自动建任务/不开后台循环**：遵循 main.py 单体无新后台任务约定；
  升级以"高可见徽标 + 一键转任务"呈现，由在线坐席即时接管，避免误判自动派单。

## 6. 明确不做（后续阶段）

| 项 | 原因 |
|----|------|
| WebSocket 全量替换 SSE | 运维与收益不成比例 |
| Redis 强制依赖 | 单实例部署为主 |
| 独立坐席微服务 | 违背 main.py 单体骨架 |
| ingest 时全量自动翻译 | 成本不可控；改为打开会话时按需译 |

## 7. 配置项（config.example.yaml）

```yaml
workspace:
  claim_ttl_sec: 900        # 会话租约 TTL（秒）
  presence_stale_sec: 120   # 超过此秒数未心跳视为离线
  quick_templates: [...]    # Phase 4 已支持

web_chat:
  prechat:                  # Phase 5-4：聊天前留资
    enabled: false
    required: false         # true=必填才能开聊
    fields:                 # key 支持 name/phone/email/wechat/line_id/note
      - {key: name,  label: "称呼",   type: text}
      - {key: phone, label: "手机号", type: tel}
      - {key: email, label: "邮箱",   type: email}
```
