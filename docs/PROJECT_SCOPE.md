# Project Scope — telegram-mtproto-ai

> 本仓库的**唯一真实边界声明**。新增模块前先对照本文件判断归属；不在本 repo 范围的需求请去对应 repo 提。

## 仓库定位

`telegram-mtproto-ai` = **多平台 AI 客服**的主骨架：

- **多平台 RPA runner** — Telegram / LINE / Messenger 各自一个 runner
- **contacts / handoff 子系统** — 跨平台的 Contact / Journey / HandoffToken，含 Messenger→LINE 引流主线
- **知识库 + 回复逻辑** — skill_manager / KB / trigger / 回复生成 / 语言守卫
- **Web 后台** — admin / funnel / monitoring / handoff preview
- **observability** — metrics / audit / grafana 仪表盘

启动入口：`main.py`（FastAPI + 所有 runner + contacts 子系统）。

## 明确不在本 repo 范围

| 内容 | 实际归属 |
|---|---|
| Facebook 加好友 + 打招呼 bot (`add_friend` / `send_greeting` / `extract_members` / `browse_feed`) | `github.com/victor2025PH/mobile-auto0423` |
| Facebook Messenger 直发 bot (`_ai_reply_and_send` / `check_messenger_inbox` / `send_message` 在 FB App 内的路径) | `github.com/victor2025PH/mobile-auto0423` |
| `fb_contact_events` / `facebook_inbox_messages` / `facebook_friend_requests` 表 | `mobile-auto0423` |
| A/B 双 worker 契约 (`INTEGRATION_CONTRACT.md` 的 A/B 分区) | `mobile-auto0423/docs/` |
| VLM Level 4 fallback / Gemini→Ollama swap 栈 | `mobile-auto0423` |

**注意**：本 repo 的 `src/integrations/messenger_rpa/` 是 **Android Messenger RPA runner**（用 adb + UI Automator 驱动手机里的 Messenger App），与 `mobile-auto0423` 的 FB/Messenger 自动化是**两套独立实现**，不共享代码。

## 与 mobile-auto0423 的关系

- **没有代码共享**，只有概念衔接：contacts 子系统的"Messenger 引流"是一条逻辑链路，各家实现走自己的 stack。
- **没有运行时依赖**：两仓库各自部署、各跑各的。
- **Claude Code 协同**：同一个 Claude 账号轮流在两个 repo 里工作，用 `~/.claude/projects/` 的两个 project dir 区分记忆。

## 本 repo 的工作流约定

1. **分支**：`feat-*` / `fix-*` / `chore-*`，squash merge 回 main。
2. **回归**：contacts/handoff 相关改动用定向 glob 跑（见 `memory/reference_broken_tests_scope.md`），预期 266+ 全绿。
3. **feature flag**：新子系统默认 `enabled: false`（参考 `config/config.yaml` 的 `contacts.enabled`）。
4. **数据库 schema**：所有 ALTER 归到 `src/...database.py` 的 migration 列表，不散落在各文件。
