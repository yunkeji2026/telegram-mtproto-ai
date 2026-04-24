# telegram-mtproto-ai — Claude Code 项目指令

> 本仓库是多平台 AI 客服的主骨架。Claude Code 在本 cwd 启动时自动加载本文件。
> **边界声明**见 [`docs/PROJECT_SCOPE.md`](docs/PROJECT_SCOPE.md)（权威文档）。

## 仓库一句话

`main.py` 启 FastAPI，内嵌：contacts/handoff 子系统 + Telegram/LINE/Messenger 三端 RPA runner + skill_manager / KB / 回复生成 / 语言守卫 + Web 后台 + observability。

## Claude 在本 repo 工作时的约定

### 回归命令（contacts/handoff 改动必跑）

```bash
python -m pytest tests/test_contacts_*.py tests/test_gateway_*.py \
  tests/test_account_limiter.py tests/test_handoff_readiness.py \
  tests/test_intimacy_engine.py tests/test_reactivation_scheduler.py \
  tests/test_handoff_*.py tests/test_cap_alert.py \
  tests/test_rpa_contact_hooks_wireup.py -q --tb=line -p no:warnings
```

预期 **266+ 全绿**。全量 `pytest tests/` 有一批预存失败/collection errors（aiohttp 未装等），不要被淹没；用定向 glob。

### Feature flag 约定

- 新子系统默认 `enabled: false`（见 `config/config.yaml::contacts.enabled`）
- ALTER TABLE 集中到 `src/**/database.py` 的 migration 列表，不散落

### Git workflow

本 repo **2026-04-24 首次进 git**。现阶段：
- `main` 为主分支；baseline 见 `git log`（初次 import + CLAUDE.md + gitignore 强化）
- 后续 feature 走 `feat-*` 分支 + PR（参考 `mobile-auto0423` 的 squash merge 流程）

### 崩溃恢复提示

- 本项目不在 git 之前的工作记录在 `DEPLOYMENT_STATUS.md` / `TODO_NEXT.md` / `docs/` 下多份 `*_PLAN.md`（历史文档，可能已过期，以代码为准）
- `~/.claude/projects/C--telegram-mtproto-ai/memory/` 里 `MEMORY.md` 按项目分组，本项目条目见 "Project: telegram-mtproto-ai" 段
- 关键教训：`project_tasklist_drift.md` — 文档落后于代码，重入时以 `grep` 验证代码实况再信任任务列表

## 不在本 repo 范围（见 PROJECT_SCOPE.md）

Facebook add_friend / greeting / auto_reply / VLM Level 4 fallback 栈 → `github.com/victor2025PH/mobile-auto0423`
