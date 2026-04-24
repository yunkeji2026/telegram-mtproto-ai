# Telegram MTProto AI — 接手说明（给 Cursor / 协作者）

本文档概述**当前已实现能力**与**推荐延展方向**，便于在新环境用 Cursor 继续开发。解压后把本文件与 `AGENTS.md`（若上层工作区有）、`config/config.yaml` 一并阅读。

---

## 1. 项目是什么

- **名称**：`telegram-mtproto-ai`（Python 3，异步）
- **形态**：基于 **Pyrogram** 的 **Telegram 用户账号（MTProto）** 客户端 + **大模型（OpenAI 兼容 / Gemini 等）** + **Skill 工作流** + **SQLite 知识库** + **FastAPI Web 管理端**
- **入口**：`main.py` → `AIChatAssistant` 组装 `ConfigManager`、`AIClient`、`SkillManager`、`TelegramClient`

---

## 2. 已实现的核心功能

| 模块 | 路径 / 说明 |
|------|----------------|
| Telegram 客户端 | `src/client/telegram_client.py` — 私聊/群消息、限流、多 Bot 路由、GXP 机器人回复处理等 |
| 触发决策 | `src/client/trigger.py` + `src/trigger/four_layer_trigger.py` — 回复链、@本账号、关键词、四层 L1–L4 |
| 技能与意图 | `src/skills/skill_manager.py` — 意图识别、KB 检索注入、`narrow_reply`、冷却、对话历史 |
| 领域包 | `domains/payment/`、`domains/ecommerce/` — `manifest.yaml` + `DomainLoader` 动态注册技能 |
| AI 调用 | `src/ai/ai_client.py` — 生成回复、多语言检测 `_detect_message_language`、`reply_lang` 注入提示词 |
| 知识库 | `src/utils/kb_store.py` — BM25 + 向量混合、`kb_translations`、miss 日志、翻译审核 API |
| Web 管理 | `src/web/admin.py` + `templates/` — 知识库、通道、案例、审计、批量翻译、向量化等 |
| 工具类 | `src/utils/` — 配置、限流、人工升级、通道状态格式化、插件加载等 |
| 脚本 | `scripts/create_pyrogram_session.py` — 本地生成 `.session`（验证码 `code.txt`） |

**配置**：`config/config.yaml`（含 `telegram`、`ai`、`narrow_reply`、`trigger`、`web_admin` 等）。

**文档**：`docs/功能清单与测试手册.md`、`docs/测试功能与流程说明.md`、`docs/部署到新服务器指南.md`。

---

## 3. 可延展的功能（按优先级建议）

1. **多语言检索**：BM25 仅索引条目主表中文字段；可在 `_rebuild_index` 合并翻译字段，或强化「英文 triggers + 向量化」流程（`knowledge.html` 向量化按钮）。
2. **新领域**：在 `domains/<name>/` 新增 `manifest.yaml` + Skill 类，参考 `payment`；`config.yaml` 的 `domain` 切换。
3. **新 Skill**：继承 `src/skills/base.py`，在 `SkillManager` 或域 manifest 中注册。
4. **插件**：`plugins/` + `config.plugins`（见 `plugin_loader`）。
5. **触发规则**：`config/trigger_rules.yaml` + 四层调参。
6. **监控与运营**：`scheduled_tasks`、`channel_alerts`、分析面板 API 等可按配置打开。
7. **测试**：`tests/` 下 pytest；改核心逻辑时跑相关用例。

---

## 4. 新环境最小步骤

```bash
cd telegram-mtproto-ai
python -m venv .venv
.venv\Scripts\activate   # Windows
pip install -r requirements.txt
# 复制 config/config.yaml，填写 telegram / ai / web_admin
# sessions 下放 *.session（勿提交仓库）；或运行 scripts/create_pyrogram_session.py
python main.py
```

- Web 默认 `127.0.0.1:8080`；**完整模式**才显示知识库「翻译审核」等：`/set_ui_mode?mode=full`
- 生产常用 **systemd** 托管单进程，避免多实例抢 session。

---

## 5. 安全与打包说明

- **不要**把真实 `*.session`、`auth_token`、`api_hash`、生产数据库随压缩包外传；本仓库 handoff 包已尽量排除 session 与常见密钥文件，**仍请检查** `config.yaml` 再转发。
- 解压后若需联生产，请自行替换为脱敏配置或环境变量方案。

---

## 6. 给 Cursor 的简短 Prompt 示例

```
你是接手开发者。先读 CURSOR_HANDOFF.md、config/config.yaml 注释、docs/功能清单与测试手册.md。
当前任务：<在此填写>
约束：小步提交、改动的文件写清原因；不要删除 sessions 与生产密钥相关说明。
```

---

*生成于工作区打包流程，可按项目演进更新本文件。*
