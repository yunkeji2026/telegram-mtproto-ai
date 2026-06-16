# 命名约定与产品词汇表（Naming & Product Glossary）

> 目的：本仓库已经长出**多个用户可见的产品面**（营销站 / 小程序 / 两个不同的「后台」）+ 一批后端服务。
> 口头沟通里「后台」「小程序」「看板」常指代不清，导致开发意图错位。
> 本文档给每个产品、功能、服务钉死**唯一规范名 + 英文 slug**，作为今后沟通和提 PR/issue 的统一词汇。
>
> **铁律**：讨论需求时，凡涉及"面"或"模块"，一律用下表的 **slug** 或 **规范中文名**，不要再用「那个后台」「小程序里」这类模糊说法。

---

## 0. 一句话消歧（最常踩的坑）

| 你可能会说 | 到底指谁 | 请改用 |
|-----------|---------|--------|
| 「后台」 | 既可能是 Next 的 `/admin`，也可能是 FastAPI 运营台 | **`growth-admin`**（增长看板）或 **`ops-console`**（运营后台），二选一说清 |
| 「小程序」 | Telegram Mini App | **`tg-miniapp`** |
| 「网站 / 官网」 | 营销落地站 | **`web-site`** |
| 「翻译聊天」 | 待定的新方向 | **`live-translate`**（见 §6，形态未定） |

---

## 1. 顶层产品面（Surface）

> 「面」= 一个独立的、用户/运营能直接打开看到的界面入口。

| slug | 规范中文名 | 技术栈 | 入口（开发态） | 职责 |
|------|-----------|--------|---------------|------|
| `web-site` | 营销官网 | Next.js（`website/`） | `http://localhost:3000/`、`/en`、`/privacy`、`/terms` | 品牌营销、SEO、落地转化、留资入口 |
| `tg-miniapp` | Telegram 小程序 | Next.js（`website/app/app/`） | `http://localhost:3000/app` | Telegram 内 Mini App，多视图 SPA，承载漏斗埋点/CTA/解锁领码 |
| `growth-admin` | 增长看板 | Next.js（`website/app/admin/`） | `http://localhost:3000/admin` | **营销侧**数据后台：小程序漏斗、留资 CRM、内容发布、兑换码 |
| `ops-console` | 运营后台 / 坐席工作台 | FastAPI（`src/web/`，`main.py` 内嵌） | `http://127.0.0.1:18787`（令牌 `admin`） | **客服运营侧**主控台：统一收件箱、知识库、翻译、RPA、联系人触达 |

> 说明：`web-site` / `tg-miniapp` / `growth-admin` 是**同一个 Next.js 应用**的三个路由面，同跑在 `:3000`（dev）。
> `ops-console` 是**完全独立**的 Python/FastAPI 应用，跑在 `:18787`，由 `python main.py` 拉起。

---

## 2. `ops-console` 内的功能模块（Module）

> 引用格式：`ops-console/<module>`，例如「在 `ops-console/inbox` 加一个翻译开关」。

| slug | 规范中文名 | 路由 | 关键文件 | 说明 |
|------|-----------|------|---------|------|
| `inbox` | 统一收件箱 / 坐席工作台 | `/workspace` | `src/web/routes/unified_inbox_routes.py`、`templates/unified_inbox.html` | 多渠道会话聚合、坐席接管、**双向实时翻译**、媒体译 |
| `kb` | 知识库 | `/knowledge` | `src/web/routes/kb_routes.py`、`kb_import_routes.py` | BM25 知识库、多语言条目翻译、导入 |
| `drafts` | 草稿审核 | `/draft_review` | `src/web/routes/drafts_routes.py` | AI 草稿人工审核、译后发送 |
| `agent-perf` | 坐席绩效 / 翻译引擎 | `/agent_perf` | `drafts_routes.py::register_agent_perf_routes` | 坐席绩效、翻译引擎健康、术语库控制台 |
| `contacts` | 联系人 / 触达 | `/ops/contacts` | `src/web/routes/contacts_routes.py`、`src/contacts/` | 联系人库、亲密度、再激活、handoff |
| `rpa` | RPA 总览 | RPA 各页 | `rpa_overview_routes.py`、`line_rpa_routes.py`、`messenger_rpa_routes.py`、`whatsapp_rpa_routes.py` | 设备/账号运行态、审批、巡检 |
| `persona` | 人设 | `persona_routes.py` | `src/web/routes/persona_routes.py` | 人格/语气配置 |
| `copilot` | 坐席助手 | `copilot_routes.py` | `src/web/routes/copilot_routes.py` | 回复建议/润色 |
| `learner` | 学习/评测 | `learner_routes.py` | `src/web/routes/learner_routes.py` | 样本学习、草稿评测 |
| `report` | 报表 | `report_routes.py` | `src/web/routes/report_routes.py` | 运营报表 |
| `auth` | 登录/用户 | `/login` | `auth_user_routes.py` | 后台登录与用户 |

---

## 3. `growth-admin` 内的功能（Next.js `/admin`）

> 引用格式：`growth-admin/<feature>`。这些是 Tab/区块，不是独立路由。

| slug | 规范中文名 | 说明 |
|------|-----------|------|
| `overview` | 增长概览 | 总量、趋势、WoW 环比 |
| `miniapp-funnel` | 小程序漏斗 | 会话级漏斗、维度下钻（落地视图/来源）、卡点诊断、时间窗 |
| `crm` | 留资 CRM | 留资明细、状态流转、来源/意向分布 |
| `content` | 内容发布 | 排期、广播、目录发布、AI 选题 |
| `codes` | 兑换码 | 解锁码签发/核销 |

---

## 4. 后端服务与引擎（Service）

> 引用格式：直接用 slug，例如「`translation-svc` 加一个 DeepL 兜底」。

| slug | 规范中文名 | 关键文件 |
|------|-----------|---------|
| `ai-client` | AI 客户端 | `src/ai/ai_client.py`（当前 provider：DeepSeek，OpenAI 兼容） |
| `skill-manager` | 技能管理器 | `src/skills/skill_manager.py`（三端共用回复产线入口 `process_message`） |
| `translation-svc` | 翻译服务 | `src/ai/translation_service.py` + `translation_engines.py`（多引擎 failover：ai/deepl/google） |
| `translation-memory` | 翻译记忆 | `src/ai/translation_memory.py`（SQLite `config/translation_memory.db`） |
| `glossary` | 术语库 | `src/ai/translation_glossary.py` |
| `voice-translate` / `image-translate` | 语音/图片翻译 | `src/ai/voice_translate.py`、`image_translate.py` |
| `lang-guard` | 语言守卫 | `ai_client.py::_guard_reply_language` + `detect_language` |
| `tg-runner` | Telegram Runner | `src/client/telegram_client.py`（Pyrogram，私聊/群） |
| `line-runner` | LINE Runner | `src/integrations/line_rpa/`（ADB RPA）+ `line_webhook.py`（API） |
| `messenger-runner` | Messenger Runner | `src/integrations/messenger_rpa/runner.py`（ADB RPA） |
| `wa-runner` | WhatsApp Runner | `src/integrations/.../wa_rpa`（ADB RPA） |
| `contacts-gateway` | 联系人网关 | `src/contacts/gateway.py`、`rpa_hooks.py` |
| `monitoring` | 监控 | `src/monitoring/server.py`（metrics 端口 `19190`） |

---

## 5. 端口与进程对照

| 端口 | 归属 | 进程 | 说明 |
|------|------|------|------|
| `3000` | `web-site` + `tg-miniapp` + `growth-admin` | `next dev`（`website/`） | 同一 Next 应用三个面（dev 态） |
| `18787` | `ops-console` | `python main.py`（uvicorn 子线程） | 运营后台，host `0.0.0.0`，令牌 `admin` |
| `19190` | `monitoring` | `python main.py`（监控线程） | Prometheus metrics |
| `18080` | 外部 `mobile-api` | `mobile-auto0423` 仓库 | Mobile Bridge 对接（不在本仓库，见 `PROJECT_SCOPE.md`） |

> 启动方式：`ops-console` 全套用 `python main.py`（会同时拉起 `tg-runner` 并连**真 Telegram 账号** + RPA，注意环境）。
> `web-site`/`tg-miniapp`/`growth-admin` 用 `cd website && npm run dev`。

---

## 6. 新方向占位：`live-translate`（实时翻译 AI 聊天）

> 形态**尚未最终确定**，先占名以便沟通。落地时在本节细化，并补进 §1/§2。

候选形态（待对齐，三选一或组合）：
- `live-translate.auto` — 全自动无人：外语消息进 → 译 → AI 生成回复 → 译回对方语言发出
- `live-translate.relay` — 双人互译中继：两个不同语言的人经 bot 实时对话
- `live-translate.assist` — 人机协同（即 `ops-console/inbox` 现有「双向翻译收件箱」的增强）

> 现状：可复用 `translation-svc` / `lang-guard` / `skill-manager` / `ops-console/inbox` 已有的双向翻译 UX；
> 尚无独立「翻译机器人模式」开关。

---

## 7. 命名规则（写代码 / 提 PR / 起 issue 时遵守）

1. **slug 用 kebab-case 英文**（`growth-admin`、`translation-svc`），出现在分支名、PR 标题 scope、目录/模块讨论里。
2. **沟通用规范中文名**，首次出现时带 slug：「增长看板（`growth-admin`）」。
3. **引用具体页面/模块**用 `surface/module`：`ops-console/inbox`、`growth-admin/miniapp-funnel`。
4. **commit scope** 沿用现有习惯并向 slug 靠拢，例如：
   - `feat(miniapp): ...` → 对应 `tg-miniapp`
   - `feat(admin): ...` → 对应 `growth-admin`
   - `feat(ops-inbox): ...` → 对应 `ops-console/inbox`
   - `feat(translate): ...` → 对应 `translation-svc` / `live-translate`
5. **禁止裸用**「后台」「小程序」「看板」「网站」而不带 slug 限定（除非上下文已明确）。

---

## 8. 维护

- 新增产品面/模块时，**先在本文档登记 slug**，再开发。
- 本文档是命名的**唯一事实源**；`CLAUDE.md` / `AGENTS.md` / `README.md` 如需引用产品名，以此为准。
- 形态/端口变更后同步更新 §1、§5。
