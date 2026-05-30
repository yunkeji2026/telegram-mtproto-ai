# GitHub 上传清单

本仓库是多平台 AI 客服与转化承接主仓。上传 GitHub 时只提交可复现的工程资产，不提交模型、会话、截图、语音、数据库和真实凭证。

## 应提交

- `src/`：AI 回复、contacts/handoff、RPA runner、Web 后台、工具模块。
- `tests/`：单元测试、回归测试、接口测试。
- `docs/`：架构、部署、运维、功能说明、已脱敏方案。
- `domains/`：领域模板、知识库种子、persona 模板。
- `scripts/`、`tools/`：长期可复用的运维、诊断、同步工具。
- `.github/`：CI 工作流。
- `config/config.example.yaml` 和不含真实凭证的配置模板。
- `requirements*.txt`、`Dockerfile`、`docker-compose.yaml`、`README.md`。

## 不应提交

- `.env`、`config/config.yaml`、API key、bot token、Telegram session。
- `sessions/`、`data/`、`logs/`。
- `*.db`、`*.sqlite*`、`*.db-wal`、`*.db-shm`。
- `models/`、Whisper/LLM 权重、缓存模型。
- `tmp_*` 截图、UI dump、临时语音、一次性探针脚本。
- 用户语音样本、聊天截图、真实联系人、客户资料。
- `.docx/.pdf/.xlsx` 等内部商务材料，除非确认可公开且脱敏。

## 提交前检查

```bash
git status --short
git diff --stat
git diff --cached --stat
```

如果看到 `sessions/`、`models/`、`tmp_*`、`logs/`、`.env`、`*.db`、真实账号或客户内容，先不要提交。
