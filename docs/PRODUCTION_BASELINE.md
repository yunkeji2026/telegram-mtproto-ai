# 生产基线（备份、安全、配置提示）

## 备份

- **建议**：升级或大批量改配置前执行备份。
- **命令**（项目根目录）：`python scripts/backup_sqlite_dbs.py`
- **输出**：`config/backups/YYYYMMDD_HHMMSS/` 下复制 `config/*.db`。
- **RPO**：取决于备份频率；重要变更前务必手动执行一次。
- **定时**：可用 **cron**（Linux/macOS）或 **任务计划程序**（Windows）按日/周调用上述脚本；注意工作目录为项目根，Python 解释器与依赖环境与线上一致。
- **cron 示例**（Linux/macOS，每日 3:15，请替换 `PROJECT_ROOT` 与 `python` 路径）：
  ```cron
  15 3 * * * cd /path/to/PROJECT_ROOT && /path/to/venv/bin/python scripts/backup_sqlite_dbs.py >> logs/backup_cron.log 2>&1
  ```
- **Windows 任务计划**（在项目根执行一次 `schtasks` 创建每日任务，按需改路径与用户）：
  ```bat
  schtasks /Create /TN "TG-AI-SQLite-Backup" /SC DAILY /ST 03:15 /RL HIGHEST /F /TR "cmd /c cd /d D:\telegram-mtproto-ai && D:\telegram-mtproto-ai\.venv\Scripts\python.exe scripts\backup_sqlite_dbs.py >> D:\telegram-mtproto-ai\logs\backup_task.log 2>&1"
  ```
- **WAL 模式**：审计等库默认 `journal_mode=WAL`，运行中会存在 `-wal`/`-shm` 旁路文件。`backup_sqlite_dbs.py` 默认用 **`sqlite3` 在线备份 API** 生成一致快照；若某文件备份失败会回退为 `copy2` 并在 stderr 打出 `WARN`。若仍手动复制 `.db` 文件，请在低写入窗口或先停服务，避免拷贝到不完整 WAL 状态。

## 安全

- **Web 管理端**：默认 `127.0.0.1` 风险较低；若 `web_admin.host` 为 `0.0.0.0` 或对公网开放，必须配合 HTTPS、强密码、防火墙或 VPN，并轮换 `auth_token`。
- **凭证**：`config.yaml` 与 Telegram session 文件权限仅限运维账号；勿提交仓库。
- **监控 API**：默认本机端口（见 `monitoring.metrics_port`），勿对公网裸暴露。

## 配置互斥与提示

- **情景记忆**：若同时开启 `memory.vector.backfill_on_startup` 与 `backfill_periodic`，启动补全会自动跳过，避免重复嵌入（见启动日志）。
- **日预算**：`memory.vector.daily_embed_budget` 仅约束「补全」路径的嵌入计数（UTC 日）。

## 关联 ID

- 每条用户消息处理若未传入 `request_id`，将自动生成 `r-<12位hex>`，并出现在相关日志前缀中，便于同一条链路检索。

## 相关文档

- 架构总览：`docs/ARCHITECTURE_OVERVIEW.md`
- 多实例与 SQLite：`docs/DISTRIBUTED_NOTES.md`
