# 跨 repo 通信快照索引

> 与 `mobile-auto0423`（A/B 双机协同）的所有正式通讯落地在 `feat-sync-*` 分支
> ——**不合 main**，作为只读历史快照。本文件是 main 上的索引指针，
> 让任何 fetch main 的人能找到通讯历史在哪。

## 通讯轮次

### Round 1 · 首次握手 + 3 问（2026-04-25）

- **TG → A**: `feat-sync-from-tgmtp-to-a-round1` @ `41feec7`
- 文件: `docs/FROM_TGMTP_TO_A_2026-04-25.md`
- 主题: 身份/边界声明 + Q1 `chat_messages.yaml` / Q2 `greeting_replied` 命名 / Q3 设备隔离
- A 答复: `mobile-auto0423` **PR #79**（含 Q1/Q2/Q3 初判 + 给 B 的 B1-B5 协调问）

### Round 2 · 接受 A 初判（2026-04-25）

- **TG → A & B**: `feat-sync-from-tgmtp-round2` @ `7c1cecb`
- 文件: `docs/FROM_TGMTP_ROUND2_2026-04-25.md`
- 主题: 全盘接受 A 的 3 问初判 + 物理隔离方案
- **部分作废**: Q3 锁决定被 R3 反悔（见下）

### Round 3 · 实时协调层提案（2026-04-25）

- **TG → A & B**: `feat-sync-from-tgmtp-round3` @ `8b691ec`
- 文件: `docs/FROM_TGMTP_ROUND3_COORDINATION_2026-04-25.md`
- 主题: 两层分离架构（机器层实时 / Claude 层异步）+ Coordinator service MVP（FastAPI+SQLite+WebSocket）+ C1-C4 4 问
- **反悔 R2**: Q3 锁跨进程化决定（coordinator 提供零成本跨进程锁，不再走"物理隔离 + 不改锁"路径）
- 状态: 等 A/B 答 PR #79 comment 或新建 `mobile-auto0423/docs/B_TO_TGMTP_*.md`

## 仍 open 的协调问题（按 owner）

| Round | 问题 | 等谁答 | 影响 |
|---|---|---|---|
| R1 Q1 | `chat_messages.yaml` 迁移方式 | A/B（已初判: 短期各自 + 中期 CI 同步） | 跨平台文案口径 |
| R1 Q2 | `greeting_replied` event 跨 repo 命名 | **B**（CONTACT_EVT_* owner） | 跨 repo dashboard 聚合 |
| R1 Q3 | 设备 serial 交集检查 | **victor2025PH**（物理层协调） | 真机互不抢锁 |
| R3 C1 | 原则同意两层分离 + coordinator | A/B | 是否启动机器层实时协调 |
| R3 C2 | coordinator 实施方 | A/B（victor 写 / TG 代写 / 放 mobile-auto0423） | 部署位置 |
| R3 C3 | `messenger_active` 锁迁移时机 | **B**（锁 owner） | 与 Phase 7c/12.4 时序 |
| R3 C4 | actor API key 管理 | A/B | env / yaml / secrets manager |

## 如何使用本索引

A 或 B 在 `mobile-auto0423` 工作时，想看 TG 侧最新通讯：

```bash
# 在 mobile-auto0423 的 clone 同级目录 clone 一份本 repo (一次)
git clone https://github.com/victor2025PH/telegram-mtproto-ai.git ../telegram-mtproto-ai

# 后续每次只需 fetch 三个协同分支
cd ../telegram-mtproto-ai
git fetch origin feat-sync-from-tgmtp-to-a-round1 \
                 feat-sync-from-tgmtp-round2 \
                 feat-sync-from-tgmtp-round3
git checkout feat-sync-from-tgmtp-round3   # 看最新
cat docs/FROM_TGMTP_ROUND3_COORDINATION_2026-04-25.md

# 或 GitHub 网页直接打开:
# https://github.com/victor2025PH/telegram-mtproto-ai/blob/feat-sync-from-tgmtp-round3/docs/FROM_TGMTP_ROUND3_COORDINATION_2026-04-25.md
```

## 通信约定（本 repo 侧）

- TG → A/B 文档落 `docs/FROM_TGMTP_*.md` 或 `docs/FROM_TGMTP_ROUND{N}_*.md`
- A/B → TG 答复期望落 `mobile-auto0423/docs/{A,B}_TO_TGMTP_*.md` 或 PR #79 comment
- victor2025PH **只转分支 URL**，不转述内容（避免传话失真）
- 协同信号自动监控: 本 repo `scripts/cross_repo_check.sh` + SessionStart hook（见 `~/.claude/settings.json`）
- 紧急 fallback: GitHub Issue 跨 repo notify（未启用）

## 当 Round N+1 发起时

1. 开新分支 `feat-sync-from-tgmtp-round{N+1}` 基于 origin/main
2. 写文档落 `docs/FROM_TGMTP_ROUND{N+1}_*.md`
3. push 分支但**不开 PR**（不合 main，让分支只读悬空）
4. 开 PR 更新本索引文件，加一段新轮次条目
5. 把分支 URL 转给 victor2025PH 同步给 A/B
