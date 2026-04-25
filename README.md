# telegram-mtproto-ai

[![tests](https://github.com/victor2025PH/telegram-mtproto-ai/actions/workflows/tests.yml/badge.svg)](https://github.com/victor2025PH/telegram-mtproto-ai/actions/workflows/tests.yml)

多平台 AI 客服主骨架：contacts/handoff 跨平台客户通讯 + Telegram/LINE/Messenger RPA runner + 知识库回复 + Web 后台 + observability。

## 核心能力

- **contacts/handoff 子系统** — 跨平台 Contact / Journey / HandoffToken，含 Messenger→LINE 引流主线
- **多平台 RPA runner** — Telegram (MTProto)、LINE、Android Messenger（adb + UIAutomator）各自一个
- **AI 回复栈** — skill_manager / KB bm25 / 四层 trigger / 回复生成 / 语言守卫
- **Web 后台** — admin / funnel / handoff preview / monitoring
- **observability** — metrics / audit / Grafana 仪表盘

启动：`python main.py`（FastAPI + 所有 runner + contacts 子系统，config flag 控制子系统开关）

## 相关仓库

- **[victor2025PH/mobile-auto0423](https://github.com/victor2025PH/mobile-auto0423)** — 配套的 Facebook/Messenger 移动端 bot（add_friend / greeting / auto-reply）。两 repo **代码独立**，只通过 contacts 子系统的 Messenger→LINE 引流主线业务衔接。详见 [`docs/PROJECT_SCOPE.md`](docs/PROJECT_SCOPE.md)。

## 开发

```bash
pip install -r requirements.txt
cp config/config.example.yaml config/config.yaml  # 按需填 api_key / Telegram 凭证
python main.py
```

测试：
```bash
python -m pytest tests/ -n auto -q         # 全量回归，0 ignore（baseline 266 → 4x+ 当前规模，~50s）
```

CI：push/PR 到 main 自动跑全量回归（见 `.github/workflows/tests.yml`）。

## 文档

- [`docs/PROJECT_SCOPE.md`](docs/PROJECT_SCOPE.md) — 仓库范围与边界（硬约定）
- [`CLAUDE.md`](CLAUDE.md) — Claude Code 项目级指令
- [`docs/ARCHITECTURE_OVERVIEW.md`](docs/ARCHITECTURE_OVERVIEW.md) — 架构一览
- [`docs/CONTACTS_RPA_INTEGRATION.md`](docs/CONTACTS_RPA_INTEGRATION.md) — contacts/handoff 与 RPA runner 集成契约
- [`docs/CROSS_REPO_LOG.md`](docs/CROSS_REPO_LOG.md) — 与 mobile-auto0423 的跨 repo 通讯快照索引
- [`docs/OBSERVABILITY.md`](docs/OBSERVABILITY.md) — 指标、日志、告警
- [`scripts/README.md`](scripts/README.md) — scripts/ 目录索引（运维 + 调试 + 联调）

## License

Private repository.
