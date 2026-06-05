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
