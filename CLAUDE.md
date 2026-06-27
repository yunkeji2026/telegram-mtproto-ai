# telegram-mtproto-ai — Claude Code 项目指令

> 本仓库是多平台 AI 客服的主骨架。Claude Code 在本 cwd 启动时自动加载本文件。
> **边界声明**见 [`docs/PROJECT_SCOPE.md`](docs/PROJECT_SCOPE.md)（权威文档）。

## 仓库一句话

`main.py` 启 FastAPI，内嵌：contacts/handoff 子系统 + Telegram/LINE/Messenger 三端 RPA runner + skill_manager / KB / 回复生成 / 语言守卫 + Web 后台 + observability。

## Claude 在本 repo 工作时的约定

### 回归命令

**全量**（pytest.ini `asyncio_mode=auto` + pytest-asyncio plugin，0 ignore）：
```bash
python -m pytest tests/ -n auto -q
```
预期：全绿，0 fail，CI ~50 秒（baseline 266 → 4x+ 当前规模；不存具体数字，每次合 PR 会增加，按 `git log` 看实际）。

> ⚠️ 本机若有常驻服务（app/RPA runner）在跑，`-n auto` 会与之争 CPU 把全量拖到数分钟，
> 且**无超时时任一 worker 卡住会无限等**（曾出现「跑 50 分钟不结束」）。本机跑全量建议固定带超时兜底
> （挂起会被点名而非无限等，已装 `pytest-timeout`）：
> ```bash
> python -m pytest tests/ -n auto -q --timeout=90 --timeout-method=thread
> ```

**仅 contacts/handoff 主线**（快速回归）：
```bash
python -m pytest tests/test_contacts_*.py tests/test_gateway_*.py \
  tests/test_account_limiter.py tests/test_handoff_readiness.py \
  tests/test_intimacy_engine.py tests/test_reactivation_scheduler.py \
  tests/test_handoff_*.py tests/test_cap_alert.py \
  tests/test_rpa_contact_hooks_wireup.py tests/test_contacts_runner_bridge.py \
  -q --tb=line
```
预期：全绿（contacts/handoff 主线子集，含 runner→真 hooks→store bridge 测试）。

**桌面客服「受控出站 / 人审介入」主线**（P0–P7 闭环：桌面启动档 + 注入健康看板 + 选择器热修 +
受控出站 hold/拦截/改写/放行 + AI 重写 + 纠正样本三元组/导出 + SLA 提醒 + 失误聚类）：
```bash
python -m pytest tests/test_desktop_*.py -q --tb=line
```
预期：全绿（出站队列状态机 + 人审介入 + 纠正样本/JSONL 导出 + SLA + 拦截聚类，
含 boot-gate / selectors / inject-health / 路由契约）。
桌面壳前端纯函数（Node 直跑，无框架）：
```bash
cd desktop && npm test
```
预期：全绿（health-panel 看板模型 / 出站行 / 待审 FIFO / 拦截 chips / SLA / fingerprint / launcher 等）。

### Feature flag 约定

- 新子系统默认 `enabled: false`（见 `config/config.yaml::contacts.enabled`）
- ALTER TABLE 集中到 `src/**/database.py` 的 migration 列表，不散落

### Git workflow

本 repo **2026-04-24 首次进 git**。现阶段：
- `main` 为主分支；baseline 见 `git log`（初次 import + CLAUDE.md + gitignore 强化）
- 后续 feature 走 `feat-*` 分支 + PR（参考 `mobile-auto0423` 的 squash merge 流程）

### 崩溃恢复提示

- 本项目不在 git 之前的工作记录在 `DEPLOYMENT_STATUS.md` / `TODO_NEXT.md` / `docs/` 下多份 `*_PLAN.md` 与早期分析（历史文档，可能已过期，**以代码为准**）
- 已知含**虚构 model ID** `claude-4.6-oups-high` 的 deprecated docs（不要被这些占位误导）：`CURSOR_DEVELOPMENT_GUIDE.md`、`CURSOR_HANDOFF.md`、`docs/MONITORING_PLAN.md`、`docs/MONITORING_API_SPEC.md`、`docs/ORDER_REPLY_GENERATION_ANALYSIS.md`、`docs/LOG_ANALYSIS_OPTIMIZATIONS.md`——本 repo 实际 ai provider 见 `README.md` + `config/config.yaml::ai`
- `~/.claude/projects/C--telegram-mtproto-ai/memory/` 里 `MEMORY.md` 按项目分组，本项目条目见 "Project: telegram-mtproto-ai" 段
- 关键教训：`project_tasklist_drift.md` — 文档落后于代码，重入时以 `grep` 验证代码实况再信任任务列表

## 不在本 repo 范围（见 PROJECT_SCOPE.md）

Facebook add_friend / greeting / auto_reply / VLM Level 4 fallback 栈 → `github.com/victor2025PH/mobile-auto0423`
