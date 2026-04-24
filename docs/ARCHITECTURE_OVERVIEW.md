# 架构总览（简）

## 进程与入口

- **main.py**：加载 `ConfigManager` → `AIClient` → `SkillManager` → `TelegramClient`；可选启动 Web（FastAPI）与监控线程。
- **单进程假设**：KB 单例、指标、情景记忆补全预算等以进程内状态为主；多实例部署前需重新设计共享层。

## 消息路径（概念）

1. Telegram 收到消息 → `TelegramClient` 组装 `context`（含可选 `request_id`）。
2. `SkillManager.process_message` → 同 chat-user 串行锁 → `_handle_message_guarded`。
3. 冷却、情景记忆注入、合并上下文、意图识别、（可选）知识库混合检索、策略与 Skill 执行、回复。

## 数据存储（config 目录常见文件）

- **knowledge_base.db**：知识库条目、BM25/向量索引持久化等。
- **bot.db**：用户上下文、情景记忆表等（依配置）。
- **audit.db**：管理端操作审计（若启用）。
- **web_users.db**：后台账号与角色。

## Web 管理端

- **create_app**：集中注册路由；域包 `domains/<domain>/web/routes.py` 在核心路由之后注册；情景记忆相关路由在域路由之后注册以避免覆盖。

## 扩展点

- **域包**：`manifest.yaml`、hooks、人设与 KB 分类。
- **Hooks**：如 KB 预检索跳过（通道指标等场景）。
