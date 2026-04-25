# scripts/ — 运维 + 调试 + 联调脚本

> 这些脚本**不在 pytest 路径**（`pytest.ini::testpaths = tests`）。
> 都是手动跑的工具：诊断、smoke、联调、一次性 seed、打包。
> 历史 P 阶段（P1-P7）ad-hoc 验证脚本已于 2026-04-25 PR #13 删除——
> 真正的回归测试在 `tests/` 下走 pytest，约 1100+ case，CI 自动跑。

## 命名约定

- `<verb>_<noun>.py` 通用风格（`backup_sqlite_dbs`、`probe_view_tree`、`diag_messenger_context`）
- `<channel>_<verb>_<scope>.py` 跨渠道脚本（`line_rpa_*`、`msgr_*`）
- `cross_repo_*.sh` 跨 repo 协调（与 `mobile-auto0423` 互通）
- 不用下划线开头（保留给 `__pycache__` 等 Python 内部）

## 索引（按用途分类）

### 跨 repo 协同

| 脚本 | 用途 |
|---|---|
| `cross_repo_check.sh` | 一键摘要 TG-MTProto ↔ mobile-auto0423 协同信号（近 24h docs/* 变动 + PR #79 状态 + 本 repo open PR）。被 SessionStart hook 自动调用 |

### 诊断 (production-safe，只读)

| 脚本 | 用途 |
|---|---|
| `diag_messenger_context.py` | 查 `bot.db::user_context` 表里 messenger chat 的 `_conversation_history` 持久化情况——排查"AI 没记住前文"问题 |
| `probe_view_tree.py` | uiautomator dump 探测 Messenger 各页面的 text/content-desc，定位 selector |
| `msgr_probe_accounts.py` | 扫所有配置账号当前 Messenger Chats 页，人眼确认 peer name |
| `replay_inspect.py` | 回放 RPA 决策日志做 post-hoc 检查 |

### Messenger RPA 联调

| 脚本 | 用途 |
|---|---|
| `msgr_mutual_chat_test.py` | 两台 Messenger 后台账号**互发一条固定文案**（不经 LLM）——验证发送链路 |
| `msgr_single_send.py` | 给指定 chat 发单条消息——最小化复现 |
| `seed_messenger_skipped_chats.py` | **一次性 seed**：把已知"不该自动回复"的 chat 写入 `messenger_rpa_skipped_chats`。重复跑幂等 |

### LINE RPA 联调

| 脚本 | 用途 |
|---|---|
| `line_rpa_list_scan.py` | LINE 聊天列表扫描 + 滑动探测（vision 驱动） |
| `line_rpa_live_test.py` | LINE 实机联调最小闭环（绕开 uiautomator） |
| `line_rpa_vision_check.py` | LINE vision 多模态识别验证 |

### 维护 / 运维

| 脚本 | 用途 |
|---|---|
| `backup_sqlite_dbs.py` | 备份 `config/` 下所有 SQLite db |
| `package_handoff_zip.py` | 打包 handoff 交付 zip |

### 演示 / 一次性配置

| 脚本 | 用途 |
|---|---|
| `contacts_demo.py` | contacts/handoff 子系统 9+1 步 demo —— 看跨平台合并怎么走 |
| `create_pyrogram_session.py` | 首次创建 Telegram Pyrogram session（要登录交互） |

## 运行约定

- **CWD = repo 根目录**：所有脚本假定从 `C:\telegram-mtproto-ai\` 启动，不在 `scripts/` 内
- **无 sys.path hack 必要**：脚本通过 `sys.path.insert(0, parent)` 自插入（`contacts_demo` / `_p2_*` 等历史脚本的模式，已 deprecated）；新写的 script 直接用 `python -m scripts.foo` 形式跑可避免 path 黑魔法
- **不在 CI**：CI 只跑 `tests/`，scripts/ 改动不触发回归

## 添加新脚本时

1. 命名按上述约定
2. 第一行加 docstring（`"""<一句话用途>"""`）—— 自动出现在本 README 的更新中
3. 如果是手动跑的 ad-hoc 验证（不是长期工具），不要进 scripts/——写到 tests/ 下用 pytest 跑
4. 合并 PR 时如果加新 script，顺手更新本 README 索引
