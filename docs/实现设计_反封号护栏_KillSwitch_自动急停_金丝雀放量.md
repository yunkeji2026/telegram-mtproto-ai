# 实现设计：反封号护栏三件套（Kill-Switch / 自动急停 / 金丝雀放量）

> **状态**：草案 v1，待团队评审
> **作者**：（待签）　**日期**：2026-06-18
> **关联**：`docs/N_LINE_REAL_ACCOUNT_CHECKLIST.md`、`docs/M_LINE_COMMERCIALIZATION.md`、`docs/REACTIVATION_GRADUAL_ROLLOUT.md`
> **slug**：`ops-console/send-guard`（沿用 `companion_send_gate` 家族）

---

## 0. 背景与目标

产品方向已锁定为 **「AI 情感陪伴数字员工 · 全球化 · 7×24 无人值守」**，且决策为**激进上量**（尽快放开全自动抢市场）。
激进全自动的最大风险是**烧号**：协议号被封 = 关系链断 = 陪伴产品信任崩塌。因此「边跑边补」必须建立在**不会一夜烧光账号池**的安全护栏之上。

本设计补齐 Sprint 0 缺失的三件护栏，把「激进 GTM」与「账号池存活」解耦：

| 编号 | 护栏 | 一句话职责 |
|---|---|---|
| **G1** | 全局 Kill-Switch | 一键（毫秒级）冻结**所有**自动发送（A 线 / B 线 / RPA），重启不丢 |
| **G2** | 封号信号自动急停 | 发送侧捕获 Telegram 风控错误 → **自动暂停**该号 + 告警，不再硬怼 |
| **G3** | 金丝雀放量 | 自动化只先在小批账号 cohort 跑，绿灯稳定才逐步扩面 |

非目标（本设计**不**涉及）：代理池管理、号源采买、内容质量调优、RPA 真机适配。

---

## 1. 现状盘点（以代码为准）

| 能力 | 状态 | 代码锚点 / 缺口 |
|---|---|---|
| 单账号反封号闸门（预热爬坡 + 红黄绿灯 + 配额拦截） | ✅ 完成 | `src/skills/companion_send_gate.py::evaluate/gate_decision`；信号 `src/skills/account_signals.py`；M7 `account_health` |
| 闸门已接入真实发送路径 | ✅ 完成 | A 线 `src/client/sender.py:233`（`_send_reply`）；B 线 `src/integrations/protocol_autoreply.py:392`（`_send`） |
| 被动急停（banned/红灯/熔断 → 拒发） | ✅ 完成 | `gate_decision` 对 `signals.banned` / 红灯 / `_circuit_open` 返回 `allowed=False` |
| **主动封号探测**（风控错误 → 翻 banned/暂停） | ❌ 缺 | 无 Telegram 错误分类器；熔断只看「连续发送失败」泛化信号 |
| **全局 Kill-Switch** | ❌ 缺 | 仅 Messenger 单 chat 紧急停发 `/api/messenger-rpa/.../emergency_stop`，非跨平台总闸 |
| **金丝雀放量** | ❌ 缺 | 有策略 A/B 灰度、`reactivation` 灰度 SOP，但无账号级 cohort 放量 |

**关键设计杠杆**：`companion_send_gate.evaluate()` 已是 A/B 两线**唯一发送前汇聚点**。G1 与 G3 都应在这里注入判定，**零散落**、一处生效两线全覆盖。

---

## 2. 设计原则

1. **单一汇聚点**：所有「能不能发」的判定收口到 `evaluate()`，新增维度按优先级短路。
2. **默认零破坏**：三件护栏各有独立开关，缺省**不改变现有行为**（kill-switch 默认未触发；canary 默认全量；auto-pause 默认关）。
3. **失败开放 vs 失败关闭**：Kill-Switch 与 auto-pause 走**失败关闭**（判定异常时偏向「停」更安全）？——**待评审，见 §7 开放问题 Q1**。当前 `evaluate` 异常时是「放行」（失败开放），与本原则冲突，需明确。
4. **重启不丢**：Kill-Switch 与账号 pause 状态必须落盘（参考 Messenger emergency_stop「重启不丢」）。
5. **可观测**：每次拦截/急停/扩面都出 metric + 告警 + 审计，能复盘。

---

## 3. G1 · 全局 Kill-Switch

### 3.1 目标
运营发现异常（封号潮 / 内容事故 / 风控升级）时，**一键冻结所有自动发送**，毫秒生效、重启不丢、可分级、可一键恢复。

### 3.2 分级（tiered）
| 级别 | 范围 | 用途 |
|---|---|---|
| `global` | 所有平台所有账号的自动发送 | 核弹按钮，舆情灭火 |
| `platform:<telegram\|line\|...>` | 单平台 | 某平台风控升级 |
| `account:<platform>:<id>` | 单账号 | 单号异常（与 G2 复用同一暂停态） |

### 3.3 数据与持久化
新增轻量运行时状态存储 `src/ops/kill_switch.py`：
- 落盘：`config/runtime_flags.db`（SQLite，单表 `kill_switch(scope TEXT PK, on INTEGER, reason TEXT, actor TEXT, ts REAL)`）或复用现有 `bot.db` 加表（**待评审 Q2**：新库 vs 复用）。
- 内存缓存 + mtime/版本号，避免每次发送都查库（热路径）。
- 提供纯函数 `is_blocked(scope_chain) -> (blocked: bool, reason: str)`，`scope_chain = [global, platform:x, account:x:y]` 任一命中即 block。

### 3.4 接线点
在 `companion_send_gate.evaluate()` **最前**插入（优先级高于 banned/红灯）：
```python
ks = kill_switch_check(platform, account_id)   # 读内存缓存
if ks.blocked:
    return {"allowed": False, "reason": f"kill_switch:{ks.scope}", "light": "red", ...}
```
RPA 线（Messenger/LINE/WhatsApp runner）发送前同样调用 `kill_switch_check`（它们目前不一定过 `evaluate`，需各自接一行；**待评审 Q3**：RPA 是否统一改走 `evaluate`）。

### 3.5 API（ops-console）
新增 `src/web/routes/ops_killswitch_routes.py`（计入路由清单 `test_admin_route_inventory.py`）：
- `GET  /api/ops/kill-switch` — 当前所有生效 scope + 历史
- `POST /api/ops/kill-switch` — body `{scope, reason}` 置位
- `DELETE /api/ops/kill-switch` — body `{scope}` 解除
- 权限：master/admin（复用 `web_user_store.PAGE_PERMISSIONS`）
- UI：`ops-console` dashboard 顶部红色「🛑 全局停发」按钮（默认折叠确认弹窗，参考 messenger_rpa.html 应急面板）

### 3.6 配置
```yaml
ops:
  kill_switch:
    enabled: true          # 总功能开关（关 = 永不拦截）
    fail_closed: false     # 见 Q1：判定异常时是否偏向「停」
```

### 3.7 测试点
- 置 `global` → A 线 + B 线 `evaluate` 均返回 `kill_switch:global`。
- 置 `account:telegram:X` → 仅 X 被拦，其它号放行。
- 重启进程后状态仍在（读库恢复）。
- 解除后恢复发送。

---

## 4. G2 · 封号信号自动急停

### 4.1 目标
发送时若命中 Telegram 风控错误，**按错误类型分级处置**：临时退避 / 暂停账号 / 标记封禁，并告警，避免「硬怼到死」。

### 4.2 错误分类表（pyrogram 异常 → 处置）
| 错误类 | 含义 | 处置 | 落地 |
|---|---|---|---|
| `FloodWait(x)` | 限速，需等 x 秒 | **临时退避**（非封号）：记 `circuit` 冷却 x 秒，不翻 banned | 限速器既有冷却 |
| `PeerFlood` | 被判垃圾邀约 | **暂停账号** `pause`（可恢复）+ 红色告警 | `account:pause` |
| `UserDeactivatedBan` / `UserBannedInChannel` | 账号被封 | **标记 banned**（不可自动恢复，待人工）| registry `meta.banned=true` |
| `AuthKeyUnregistered` / `Unauthorized` / `SessionRevoked` | session 失效/被踢 | **标记 offline + banned 候选** + 告警重登 | registry `status`/`meta` |
| `SlowmodeWait` / 其它 transient | 群慢速等 | 退避，不动账号态 | 冷却 |

> 分类器 `src/ops/ban_signal.py::classify(exc) -> Action(kind, cooldown_sec, reason)`，**纯函数**、可注入假异常单测，不依赖真号。

### 4.3 接线点
两条发送路径的**异常捕获处**调用分类器：
- B 线：`protocol_autoreply._send` 的发送 try/except（现仅泛化失败→熔断）→ 改为先 `classify`，按 kind 调 `pause_account` / `mark_banned` / 退避。
- A 线：`client/sender.py::_send_reply` 的 except 分支同样接入。
- 处置后写 `account_registry`（`meta.banned` / `status` / `meta.paused_until`），下次 `build_account_signals` 自然读到 → `evaluate` 拒发（**闭环复用既有被动急停**，无需新拦截逻辑）。

### 4.4 暂停态（pause）与 banned 的区别
- `pause`：可自动恢复（如 `paused_until` 到期 / 人工解除）。新增 `account_signals` 识别 `meta.paused_until > now` → 视为受限（`STAGE_RESTRICTED`）。
- `banned`：不可自动恢复，必须人工核查后清除。

### 4.5 告警
复用 `protocol_autoreply.publish_alert` / `health_watchdog` webhook：`category=account_paused` / `account_banned`，带 platform/account_id/reason。

### 4.6 配置
```yaml
ops:
  auto_pause:
    enabled: false         # 默认关（遵循新子系统默认关；陪伴预设里开）
    pause_minutes: 60       # PeerFlood 等可恢复类的暂停时长
    peerflood_to_pause: true
```

### 4.7 测试点
- 喂 `FloodWait(30)` → 退避，账号**不**翻 banned。
- 喂 `PeerFlood` → `meta.paused_until` 被写，`evaluate` 下次拒发，发 `account_paused` 告警。
- 喂 `UserDeactivatedBan` → `meta.banned=true`，fleet-health 显示该号 banned。
- `paused_until` 到期后自动恢复发送。

---

## 5. G3 · 金丝雀放量

### 5.1 目标
自动化开关打开后，**不**立刻全量；先只让一小批 cohort 真发，观察绿灯稳定（无急停、无红灯）达 N 小时，再按比例扩面。把「新策略上线」的爆炸半径限制在少数账号。

### 5.2 模型
- cohort 状态：`canary(放量中) → expanding → full`；或运营手动 pin 一批账号。
- 选池策略（**待评审 Q4**）：
  - (a) **手动 pin**：运营指定账号 id 列表先跑（最可控，推荐首版）。
  - (b) **自动按健康**：优先选老号/绿灯号进 cohort。
- 扩面触发：cohort 内**无** account_paused/banned 且红灯率 < 阈值，持续 `hold_hours` → 放大 `step`（如 5→15→全量）。
- 由 `health_watchdog` 周期 tick 驱动评估（已有周期巡检骨架）。

### 5.3 接线点
`evaluate()` 注入「是否在放量名单」判定（优先级低于 kill-switch / banned，高于 warmup cap）：
```python
if canary_enabled(cfg) and not in_active_cohort(account_id, cfg):
    return {"allowed": False, "reason": "canary_hold", "light": "yellow", ...}
```
B 线命中 `canary_hold` 应**转人工/收件箱**而非报错丢弃（与 `HANDOFF_REASONS` 一致，**需把 `canary_hold` 加入 handoff 原因集**）。

### 5.4 API + 配置
- `GET/POST /api/ops/canary`：看 cohort 现状、手动扩面/回滚。
```yaml
ops:
  canary:
    enabled: false
    mode: manual            # manual | auto_health
    pinned_accounts: []      # mode=manual 时先跑这些
    step: 5                  # auto 扩面每轮新增账号数
    hold_hours: 24           # 绿灯稳定多少小时才扩面
    red_rate_threshold: 0.05
```

### 5.5 测试点
- `enabled + manual + pinned=[A]` → 仅 A 放行，B/C 返回 `canary_hold`。
- cohort 出现 paused → 扩面评估**不**推进（停在原档）。
- 绿灯满 `hold_hours`（注入假 now）→ cohort 扩大 `step`。

---

## 6. 统一接线总览（evaluate 判定优先级）

`companion_send_gate.evaluate()` 内判定顺序（短路，先命中先返回）：

```
1. kill_switch_check        → reason=kill_switch:<scope>   (G1, 最高)
2. signals.banned           → reason=banned                (既有 + G2 喂数据)
3. paused_until > now        → reason=account_paused         (G2 新增)
4. block_on_red 且红灯       → reason=health_red             (既有)
5. canary 且不在 cohort      → reason=canary_hold            (G3)
6. sends_today >= cap        → reason=warmup_cap             (既有)
7. 否则                      → allowed=True                  (ok)
```

> 这样三件护栏全部收口在**一个纯函数**里，A/B 两线天然共享；RPA 线只需在发送前加一行 `kill_switch_check`（或统一改走 evaluate，见 Q3）。
> handoff 原因集 `HANDOFF_REASONS` 需新增：`kill_switch`、`account_paused`、`canary_hold`（B 线命中转人工而非丢弃）。

---

## 7. 开放问题（评审需拍板）

- **Q1 失败开放 vs 失败关闭**：`evaluate` 异常时现行为是「放行」。Kill-Switch 触发态下若读盘异常，是否应「失败关闭」（偏向停）？建议：kill-switch 单独走失败关闭，其余维持失败开放。
- **Q2 状态存储**：新建 `config/runtime_flags.db` 还是复用 `bot.db` 加表？建议新库（隔离、易清）。
- **Q3 RPA 是否统一走 evaluate**：现 RPA 线发送未必过 `companion_send_gate`。统一改走收益大但改动面广；首版可只在 RPA 发送前加 `kill_switch_check` 一行，G2/G3 暂限协议号 A/B 线。
- **Q4 金丝雀选池**：首版用 `manual` pin 还是直接做 `auto_health`？建议首版 manual（可控），auto 作二期。
- **Q5 与现有 `companion_send_gate.enabled` 关系**：`ops.kill_switch` / `ops.auto_pause` / `ops.canary` 是独立开关，还是收进 `companion_send_gate` 命名空间？建议独立 `ops.*`，因 kill-switch 需对 RPA 也生效，语义大于「陪伴闸门」。

---

## 8. 分期与验收

| 阶段 | 交付 | 验收（全部不依赖真号，可单测） |
|---|---|---|
| **S0-a** | G1 Kill-Switch（store + evaluate 接线 + API + UI 按钮） | 置/解全局与单号、重启不丢、两线生效 |
| **S0-b** | G2 自动急停（classify 分类器 + 两线异常接线 + pause/banned 落地） | 各错误类处置正确、闭环拒发、告警触发 |
| **S0-c** | G3 金丝雀（cohort store + evaluate 接线 + watchdog 扩面 + API） | manual pin 只放行名单、出 paused 停推进、绿灯到期扩面 |
| **回归** | 三件套单测 + `test_companion_send_gate.py` / `test_account_signals.py` 增量 + 路由清单更新 | `python -m pytest tests/ -n auto -q` 全绿 |

预计：S0-a ≈ 1–1.5 天，S0-b ≈ 1.5–2 天，S0-c ≈ 2 天（含 watchdog 扩面逻辑），共 ~1 周。

---

## 9. 回退策略

- 每件护栏独立 `ops.*.enabled` 开关，置 false 即回到当前行为（零破坏）。
- Kill-Switch 误触：`DELETE /api/ops/kill-switch` 解除即恢复。
- auto_pause 误判：人工清 `meta.banned`/`meta.paused_until` 即恢复（提供 API/脚本）。
- canary 卡住放量：手动 `POST /api/ops/canary` 设 `full` 或关 `enabled`。

---

## 10. 评审清单（请 reviewer 逐条确认）

- [ ] 判定优先级（§6）是否合理，是否有遗漏维度
- [ ] Q1–Q5 五个开放问题的取舍
- [ ] `HANDOFF_REASONS` 新增三项后 B 线转人工链路是否仍正确
- [ ] RPA 线接入范围（Q3）对工期的影响是否可接受
- [ ] 配置 schema（`ops.*`）命名与 `config.example.yaml` 现有风格是否一致
- [ ] 是否需要把这三件套也写进 `config/presets/companion.yaml`（陪伴模式默认开 kill_switch 功能位 + auto_pause + canary）
