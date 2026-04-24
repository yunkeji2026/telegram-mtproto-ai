# ADR-0001：异地多活 / 灾备架构（Messenger RPA & AI Assistant）

- **Status**: Draft（P6-6）
- **Date**: 2026-04-21
- **Context**: P1–P5 已完成单机高可用骨架；P6-1 引入多账号真并发；准备规划异地/多机房容灾
- **Authors**: Platform Team

---

## 1. 背景与目标

当前部署为 **单机单进程**（Windows Desktop + 本机 ADB daemon）：
- 单点风险：Windows 机器重启、ADB 崩溃、网络断联 → 全站不可用
- SQLite 本地存储：state_store / context_store 都在本机，无复制
- 价值：商家凌晨仍需回复；多账号联动；合规要求 24/7 可溯源

**目标**：
1. **RTO ≤ 10 min**：单机房故障 10 分钟内主备切换完成
2. **RPO ≤ 5 min**：审批/发送记录丢失窗口 ≤ 5 分钟
3. **运维心智负担 ≤ 1 人/周**：绝对避免引入 Kubernetes / Kafka 这类重运维栈
4. **对 RPA 设备本地性兼容**：ADB 必须在设备物理附近（网络延迟 < 30ms）

---

## 2. 现状盘点

| 组件 | 存储 | 恢复点 | 单点度 |
|------|------|--------|--------|
| MessengerRpaStateStore (SQLite) | 本机文件 | last ack | 🔴 硬单点 |
| ContextStore (SQLite / 内存) | 本机+WAL | 内存快照 | 🔴 硬单点 |
| SkillManager / AIClient | 无状态 | N/A | 🟢 |
| AccountPool / Runner | 进程内 | 无 | 🔴（进程死 → 全账号停机） |
| ADB + Android 设备 | 设备本地 | N/A | 🟠（物理硬件） |
| Web / API | FastAPI 进程 | 无 | 🔴 |
| Prometheus | Grafana 外部 | 30 天 | 🟢（外部） |

**硬约束**：ADB ≈ 设备必须物理同机房；AI 调用可跨地域。
→ 数据层可异地复制；**"执行层"（runner）只能机房本地。**

---

## 3. 候选方案对比

### 方案 A：主备冷切换（人工）

```
┌───────────────────┐       ┌───────────────────┐
│ 主机 (生产)        │       │ 备机 (冷)          │
│ FastAPI+Runner    │ nightly rsync ───►       │
│ SQLite + ADB      │                           │
└───────────────────┘       └───────────────────┘
```

- **优点**：零运维成本；符合"心智负担 ≤1 人/周"
- **缺点**：RTO ≈ 30 min（需人工启动 + 重新连设备）；RPO ≈ 24h
- **风险**：回滚窗口内所有 state 丢失

**不满足 RTO/RPO 目标**，否决。

### 方案 B：SQLite WAL + Litestream → S3（主备热切换）

```
┌───────────────────┐
│ 主机 (生产)        │
│ SQLite+WAL        │──► Litestream ──► S3 / MinIO
│ FastAPI+Runner    │
└───────┬───────────┘        ▲
        │ heartbeat          │
        ▼                    │
┌───────────────────┐        │
│ 备机 (standby)     │ ◄─── Litestream restore
│ 冻结 runner        │  (每 10s pull wal frames)
└───────────────────┘
```

- **优点**：
  - Litestream 零侵入（透明备份 SQLite WAL → S3）
  - RPO ≈ Litestream 同步间隔（10s ~ 1min 可调）
  - RTO ≈ 2 min（主死 → 备机 restore + 启 runner）
  - 对 AccountPool 无侵入（仍单机运行，只是 state 异地）
- **缺点**：
  - ADB 仍绑定物理设备，切备机需"设备搬移"或"备机也接同型号设备并 stand by"
  - Split-brain 风险（主备都活着） → 需要外部 leader election（etcd / Redis SETNX）
- **运维**：
  - 新增依赖：Litestream（一个单独二进制）、S3/MinIO bucket
  - 监控：Prometheus 加 `litestream_replication_lag_seconds`

**推荐作为阶段性目标**：P7 实施。

### 方案 C：多活 + Shard by Account（未来）

```
   Account A ──► 华东机房 Runner
   Account B ──► 华北机房 Runner
   Account C ──► 新加坡机房 Runner
        ↑                 ↑
        └─── shared state (Postgres + replication)
```

- **优点**：
  - 水平扩展；单机房故障只影响该 shard
  - 每个 shard 的 runner 只管 1~N 账号，局部失效
- **缺点**：
  - 需要把 SQLite → PostgreSQL 迁移（state_store / context_store 全改）
  - SkillManager 的 `_conversation_history` 需要分布式（Redis Streams）
  - 调度层（哪个账号跑在哪个 shard）需要 ZooKeeper / etcd
  - **≥3 名工程师全职 1 季度才能落地**，违反"心智负担" 约束

**不推荐**，除非单 shard 账号数 ≥ 30 或 Messenger 并发超出单机 ADB 能力。

---

## 4. 决策

**分阶段采纳 B**：

1. **P7（4 周）**：SQLite WAL → Litestream → MinIO/S3，主备热切换
2. **P8（观察季度）**：引入 leader election（使用 Redis SET NX EX），完全自动 failover
3. **P9+（按需）**：若账号数超 30，才考虑 PostgreSQL + shard 的方案 C

---

## 5. P7 实施大纲

### 5.1 依赖引入

- `litestream` 二进制（Go 写的，无 Python 依赖）
- MinIO 单节点 docker（`docker run -p 9000:9000 minio/minio server /data`）

### 5.2 改动清单

| 文件 | 改动 |
|------|------|
| `scripts/litestream.yml` | 新增，声明 3 个 SQLite → MinIO |
| `docs/ha_runbook.md` | 手动 failover 步骤 + 自动化脚本 |
| `src/integrations/messenger_rpa/state_store.py` | 启动时检测 WAL 模式（已启用）；增加 `/health` 端点校验最近 WAL frame |
| Web `/api/health/replication` | 暴露 litestream lag 给 Prometheus |
| `scripts/failover_to_standby.ps1` | 备机启动脚本：restore → 启 FastAPI |

### 5.3 风险与缓解

| 风险 | 缓解 |
|------|------|
| Split-brain：主备同时写入 | 用 MinIO 的 if-none-match 或 redis SETNX 做 leader lock |
| ADB 设备无法跨机房 | 备机必须提前接好同型号设备并预装 Messenger；或"同机房双机+同 ADB" |
| Litestream replay 慢 | 预跑 benchmark；目前 state_store 每日 DELETE 旧 runs，DB ≤100MB，预计 restore < 30s |
| SkillManager 的上下文不复制 | 接受 RPO：切换后会话语境可能需 1 轮"找回"；episodic summary (P3-6) 已缓解 |

### 5.4 验收

- Chaos 测试：`kill -9 messenger_rpa` 主进程 → 1 分钟内备机接管 + /status 返回 ok
- Durability：断电前 10s 内的 approval 在切换后可见（RPO check）
- 成本：MinIO + Litestream 每日存储 ≤ 500MB，成本可忽略

---

## 6. 未采纳的方案

- **PostgreSQL 全量替换 SQLite**：迁移成本 2 人 × 6 周，且对单机部署用户不友好
- **主动/主动双写**：SQLite 不支持（会破坏 WAL）；PostgreSQL 的多主也复杂
- **Cloud SQL / RDS**：托管数据库成本每月 $100+，对小规模商家不经济

---

## 7. Open Questions

- Q1：备机是否需要 ADB 物理设备？若需，每机房至少 2 台同型号手机 → 成本 ×2
- Q2：跨机房网络中断时，AI 调用是否走就近 LLM 端点？（需要 LLM 端点的区域路由）
- Q3：Litestream 对 WAL checkpoint 的影响（会不会偶发性卡顿主写入）？需跑 48h 压测

---

## 8. 参考

- Litestream: https://litestream.io
- Jepsen report on SQLite WAL: https://jepsen.io/analyses/sqlite-3.39.0
- 我们的单机 HA 设计：`docs/DISTRIBUTED_NOTES.md`（前期）
- 监控基准：`docs/OBSERVABILITY.md`（P3-4/P4-4）
