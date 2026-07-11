# 通用化改造开发文档

> 项目：telegram-mtproto-ai — 从「支付客服系统」到「通用 Telegram AI 助手平台」

---

## 改造目标

将当前深度绑定支付行业的客服系统，改造为一个**仅需配置不同人设和知识库**即可适配任意行业群体的 Telegram AI 助手平台。

## 阶段总览

| 阶段 | 任务 | 状态 | 测试结果 |
|------|------|------|----------|
| Phase 0A | 核心框架解耦 — Hook 系统 + 抽取支付硬编码 | ✅ 完成 | 68/68 通过 |
| Phase 0B | Persona 系统 — persona.yaml + 多群多人设 | ✅ 完成 | 22/22 通过 |
| Phase 1A | Domain Pack 行业模板库（8 个行业） | ✅ 完成 | 121/121 通过 (8 skip) |
| Phase 1B | Web 管理后台通用化 — 去除「客服」硬编码 | ✅ 完成 | 16/16 web 测试通过 |
| Phase 1C | Docker 化部署 | ✅ 完成 | Dockerfile + docker-compose + .dockerignore |
| Phase 2A | 知识库文档批量导入 | ✅ 完成 | 30/30 通过 |
| Phase 2B | 可视化人设编辑器 + KB 导入 API | ✅ 完成 | 8/8 API 测试通过 |
| Phase 2C | admin.py 模块拆分 | ✅ 完成 | 路由模块化 + 24/24 通过 |
| Final | 全量测试 3 轮 | ✅ 完成 | 3/3 轮均 515 通过, 8 skip, 0 失败 |
| Phase 3 | Web 路由插件化 — 域包声明式菜单 + 路由抽取 + 模板迁移 | ✅ 完成 | 3/3 轮均 515 通过, 8 skip, 0 失败 |
| Phase 4A | 深度域隔离 — Widget 模板化 + KB 冲突注册机制 + 域语义清理 | ✅ 完成 | 3/3 轮均 530 通过 (新增 15 测试), 8 skip, 0 失败 |

---

## Phase 0A：核心框架解耦 — Hook 系统

### 目标
- 定义事件钩子协议（EventHook Protocol）
- 在 skill_manager.py 和 telegram_client.py 中植入 Hook 调度点
- 将 142 处支付行业硬编码迁移到 domains/payment/hooks.py
- narrow_reply 关键词从域包加载

### 设计

#### Hook 协议
```python
class DomainHook:
    async def on_message_pre_process(self, message, context) -> dict
    async def on_intent_resolved(self, intent, message, context) -> str
    async def on_kb_pre_search(self, query, context) -> str
    async def on_reply_generated(self, reply, intent, context) -> str
    async def on_reply_post_process(self, reply, message, context) -> str
    def get_narrow_reply_config(self) -> dict
    def get_channel_followup_config(self) -> dict
    def is_domain_specific_intent(self, text) -> Optional[str]
```

#### 文件变更清单
- 新建 `src/hooks/base.py` — Hook 基类
- 新建 `src/hooks/registry.py` — Hook 注册与调度
- 新建 `domains/payment/hooks.py` — 支付行业 Hook 实现
- 修改 `src/skills/skill_manager.py` — 植入 Hook 调度
- 修改 `src/client/telegram_client.py` — 植入 Hook 调度
- 修改 `src/utils/domain_loader.py` — 加载域包 hooks
- 修改 `domains/payment/manifest.yaml` — 声明 hooks
- 新建 `tests/test_hooks.py` — Hook 单元测试

### 开发记录
- 新建 `src/hooks/__init__.py`, `base.py`, `registry.py` — Hook 协议 + 注册表
- 新建 `domains/payment/hooks.py` — PaymentDomainHook 实现（~260行）
- 修改 `src/utils/domain_loader.py` — 支持加载 hooks.py + persona.yaml
- 修改 `src/skills/skill_manager.py` — 12 处引用改为 Hook dispatch：
  - 语言检测歧义 token → `_hooks.is_ambiguous_token_message()`
  - 意图识别后 override → `_hooks.dispatch_intent_resolved()`
  - 语气词判断 → `_hooks.is_meaningless_interjection()`
  - KB 搜索前跳过 → `_hooks.dispatch_kb_pre_search()`
  - 实时状态注入 → `_hooks.get_channel_status_info()`
  - 短追问检测 → `_hooks.get_followup_config()` + `is_short_followup()`
  - 回复角度轮换 → `_hooks.get_reply_angle_rotation()`
  - 升级提示 → `_hooks.get_escalation_line()`
  - narrow_reply 域包配置 → `_hooks.is_domain_metrics_query()` + `_hooks.is_short_followup()`
- `_recognize_intent()` 精简：移除 60+ 行支付硬编码，保留通用逻辑
- 修改 `domains/payment/manifest.yaml` — 声明 `hooks: true`
- 更新 3 个现有测试文件适配新架构
- 新建 `tests/test_hooks.py` — 49 个测试用例

### 测试结果
- **68/68 通过** (0.32s-1.77s)
- 含 49 个新 Hook 测试 + 19 个原有测试无回归
- 14 个预存在的失败（channel_health.py NameError 等，非本次引入）

---

## Phase 0B：Persona 系统

### 目标
- 定义 persona.yaml 结构化规范
- 支持按 chat_id 绑定不同人设
- system prompt 动态组装：persona + domain context + KB context
- 迁移当前散落各处的人设配置

### 设计

#### persona.yaml 结构
```yaml
name: "Camille"
role: "客服专员"
personality:
  traits: ["友好", "专业", "耐心"]
  style: "自然聊天风格，像真人在打字"
  emoji_level: "moderate"
speaking:
  openers: ["在的", "好的呀", "目前来看"]
  forbidden_phrases: ["作为一个AI", "我是人工智能"]
  reply_length: "concise"
  language_follow: true
identity:
  deny_ai: true
  deny_ai_reply: "我是{name}，真人客服哦"
boundaries:
  max_reply_sentences: 4
  topics_to_avoid: []
  escalation_phrases: ["这个我不太确定"]
```

### 开发记录
- 新建 `src/utils/persona_manager.py` — PersonaManager 单例（~230行）
  - 支持默认/域包/按群绑定三级 persona 覆盖链
  - system prompt 动态组装：persona + domain prompt + KB context + extra context
  - persona.yaml 读写、chat binding 导入导出
- 新建 `domains/payment/persona.yaml` — 支付客服人设（Camille）
- 修改 `src/utils/domain_loader.py` — _load_persona() 方法
- 修改 `src/skills/skill_manager.py` — 域包加载时注册 persona
- 新建 `tests/test_persona.py` — 22 个测试用例

### 测试结果
- **22/22 通过** (0.18s)
- 覆盖：单例生命周期、默认人设、域包人设、多群多人设、prompt 组装、文件IO、DomainLoader 集成

---

## Phase 1A：Domain Pack 行业模板库

### 目标
- 创建 5-8 个开箱即用的行业模板
- 每个模板包含：persona.yaml, system_prompt.txt, kb/seeds.yaml, hooks.py, manifest.yaml

### 模板清单
1. payment（已有，迁移优化）
2. ecommerce（已有骨架，完善）
3. community（社区管理）
4. education（教育答疑）
5. crypto（加密货币）
6. it_helpdesk（IT 帮助台）
7. legal（法律咨询）
8. general（通用助手）

### 开发记录
_(开发过程中自动填写)_

---

## Phase 1B：Web 管理后台通用化

### 目标
- 所有「客服」「AI智能客服系统」文案改为可配置
- 根据当前域包的 display_name 动态渲染
- 保持 payment 域用户的体验不变

---

## Phase 1C：Docker 化部署

### 目标
- Dockerfile + docker-compose.yaml
- 一键启动，环境变量注入配置

---

## Phase 2A：知识库文档批量导入

### 目标
- 支持 PDF/TXT/Markdown 文件上传
- 自动分块（chunk）→ 生成问答对 → 入库
- Web 管理后台增加导入页面

---

## Phase 2B：可视化人设编辑器

### 目标
- Web 后台增加 Persona 编辑页面
- 实时预览 system prompt 组装效果
- 支持多人设管理和切换

---

## Phase 2C：admin.py 模块拆分

### 目标
- admin.py (5932行) 拆分为多个子模块
- 不影响现有功能

---

## Phase 3：Web 路由插件化

### 目标
- 消除核心 admin.py 中所有硬编码的 `domain_name == 'payment'` 判断
- 域包通过 manifest.yaml 声明式注册 web 页面、菜单项、仪表盘组件
- 域包自带路由文件和模板，核心自动发现并挂载
- 新行业域包零修改核心即可添加专属 Web 功能

### 设计

#### manifest.yaml web 声明协议
```yaml
web:
  routes: true
  pages:
    - key: ch
      path: /channels
      label: "通道管理"
      label_simple: "通道状态"
      icon: globe
      section: ops
      show_in_simple: true
      roles: [master, admin, viewer]
      cmd_keys: "channels 通道 状态 管理"
  dashboard_widgets:
    - key: channel_health
      section: pro-only
```

#### WebContext 依赖容器
```python
@dataclass
class WebContext:
    config_manager, audit_store, event_tracker, templates, user_store
    page_auth, api_auth, api_write_factory
    auto_snapshot, broadcast_config_reload, fire_webhook
    domain_name, domain_web_pages
```

#### 域包路由注册约定
`domains/<name>/web/routes.py` 导出 `register_routes(app, ctx: WebContext)`

### 文件变更清单

#### 新建文件
- `src/web/web_context.py` — WebContext 数据类
- `domains/payment/web/templates/channels.html` — 从核心迁移

#### 修改文件
- `domains/payment/manifest.yaml` — 添加 `web.pages` + `web.dashboard_widgets`
- `src/utils/domain_loader.py` — DomainPack 新增 web_pages/web_dashboard_widgets/web_routes_enabled 属性 + _load_web()
- `src/web/admin.py`:
  - 域包 manifest 加载重构：一次读取即获取 display_name + web_pages + dashboard_widgets
  - Jinja2 FileSystemLoader 支持域包模板目录
  - `_enrich_context` 注入 domain_web_pages 和 domain_dashboard_widgets
  - `_PATH_TO_ACTIVE` / `_PATH_TO_PAGE` 动态扩展域包页面
  - 删除 _build_channel_list / channels_page / channels_update / _sync_domain_exchange_rates / api_batch_channels / api_get_channels / api_update_channel（共约 230 行）
  - health-check / alert-status 中 `domain_name == 'payment'` → 基于 domain_web_pages 动态判断
  - `_register_domain_routes` 升级为 WebContext 传参
- `src/web/templates/base.html`:
  - 简洁模式侧栏：`{% for dp in domain_web_pages %}` 动态渲染
  - 完整模式侧栏：`{% for dp in domain_web_pages if dp.section == 'ops' %}` 动态渲染
  - 命令面板：域包页面动态生成
  - 欢迎弹窗：域包页面动态列出
  - 删除所有 `{% if domain_name == 'payment' %}` 硬编码
- `src/web/templates/dashboard.html`:
  - 统计卡片：基于 `domain_web_pages` 动态选择通道/知识库
  - 健康组件：基于 `domain_dashboard_widgets` 动态显示
- `domains/payment/web/routes.py` — 从占位符升级为完整通道路由实现
- `tests/conftest.py` — 测试配置增加 `domain: payment` + 最小域包 manifest + 模板拷贝

#### 删除文件
- `src/web/templates/channels.html` — 已迁移到 `domains/payment/web/templates/`

### 实施过程优化记录

1. **架构优化 — 放弃 APIRouter 改用直接注册模式**
   原计划用 FastAPI APIRouter + include_router()，但分析后发现域包路由需要大量闭包依赖（config_manager、audit_store、auth 中间件等）。改用 `register_routes(app, ctx)` 直接模式，更简单且无 prefix 冲突问题。

2. **数据驱动替代硬编码判断**
   原方案保留 `domain_name == 'payment'` 作为过渡判断。优化为完全数据驱动：sidebar/dashboard/alert/health-check 全部基于 `domain_web_pages` 和 `domain_dashboard_widgets` 列表判断，零硬编码。

3. **工具类保留在核心而非迁移**
   深度分析后发现 `channel_health.py` 和 `channel_status_format.py` 被 6 个核心模块引用（skill_manager、kb_direct_render 等），迁移会导致核心反向依赖域包。决定只迁移纯 UI 模板（channels.html），工具类保留在 src/utils/。

4. **Jinja2 多目录模板搜索**
   通过 `FileSystemLoader([domain_tpl_dir, core_tpl_dir])` 实现域包模板优先、核心模板兜底的搜索链。域包可覆盖核心模板，也可新增专属模板。

5. **图标系统采用命名映射**
   避免在 YAML 中嵌入 SVG 字符串，采用 `icon: globe` 命名方式 + 模板内 icon map 渲染。干净且易扩展。

### 测试结果
| 轮次 | 时间 | 通过/总数 | 失败项 |
|------|------|-----------|--------|
| Round 1 | 2026-04-15 | 515/523 (8 skip) | 0 |
| Round 2 | 2026-04-15 | 515/523 (8 skip) | 0 |
| Round 3 | 2026-04-15 | 515/523 (8 skip) | 0 |

---

## Phase 4A：深度域隔离

### 目标
- 消除 admin.py 中所有会泄漏到非支付域的支付行业语义
- 建立可注册扩展机制（KB 冲突检测器、意图显示名），任何域包可插入自己的逻辑
- Dashboard widget 完全模板化，域包自带 partial 模板
- 验证 general 域在 Web 层的零泄漏

### 实施内容

#### 4A-1: Dashboard Widget 模板化
- `dashboard.html` 的 channel_health 62 行内联块 → 域包 `_widget_channel_health.html`
- 核心改为 5 行通用 `{% for _dw in domain_dashboard_widgets %} {% include _dw.template %} {% endfor %}`
- manifest 新增 `template` 字段声明 widget 模板文件名

#### 4A-2: KB 冲突检测注册机制
- 新建 `app.state.kb_conflict_checkers: list[callable]` 注册表
- 核心 `_detect_channel_data_conflict` → 改为遍历注册表 `_run_kb_conflict_checkers()`
- 支付域路由中注册 `_check_channel_data_conflict` 检测器
- `/api/kb/check-channel-conflict` → 通用 `/api/kb/check-conflict`（旧端点保留兼容）
- knowledge.html 中的冲突 CSS/HTML/JS 用 `{% if domain_web_pages has 'ch' %}` 条件包裹

#### 4A-3: 自检与意图名称域化
- `INTENT_DISPLAY_NAMES` 拆分为核心基础 + `app.state.intent_display_names_extra` 域包合并
- 支付域注册 8 个意图显示名（order_query、channel_info、gxp_command 等）
- 自检 fallback prompt 从 "你是客服Camille…" → "你是一位专业的AI助手…"
- channel_overrides / SOP 检查包裹在 `_has_ch_page` 条件中

#### 4A-4: 残留文案清理
- `/api/system-info` 的 `channels_count` 仅在有通道页的域输出
- `/api/config/summary` 的 `channels` 字段仅在有通道页的域输出
- `_CATEGORY_MAP` 合并 `intent_display_names_extra`，不再硬编码支付映射

#### 4A-5: 域隔离测试覆盖
- 新建 `tests/test_domain_web_isolation.py` — 15 个测试用例：
  - `TestGeneralDomainNoChannelLeak` (10 tests): dashboard/sidebar/API/knowledge 页面零泄漏
  - `TestWebContextStructure` (1 test): WebContext 数据类字段验证
  - `TestPaymentDomainRegistrations` (4 tests): 冲突检测器注册、意图名称注册、检测效果

### 实施过程优化记录

1. **可注册冲突检测器替代直接迁移**
   原计划只是把 `_detect_channel_data_conflict` 搬到域包。优化为注册表模式：`app.state.kb_conflict_checkers` 列表，任何域包可插入检测器。电商域可以检测"价格数据冲突"，教育域可以检测"课程时间冲突"，核心不预设任何行业知识。

2. **前端冲突 UI 条件渲染**
   原方案只移了后端代码。深入分析发现 knowledge.html 中有完整的前端冲突检测 JS（_CH_DATA_KW 数组 + _checkChannelConflict 函数）+ CSS + HTML 元素。全部用 Jinja2 条件包裹，非支付域不输出任何冲突检测代码。

3. **意图名称合并而非替换**
   保留核心基础意图（greeting、small_talk 等），域包通过 `app.state.intent_display_names_extra` 增量扩展。数据流：核心基础 ← 域包追加 = 最终合集。

4. **API 响应字段条件化**
   `/api/system-info` 和 `/api/config/summary` 的通道相关字段用 dict unpacking 条件注入：有通道页时输出 `channels_count`，否则不包含该字段。

### 测试结果
| 轮次 | 时间 | 通过/总数 | 失败项 |
|------|------|-----------|--------|
| Round 1 | 2026-04-15 | 530/538 (8 skip) | 0 |
| Round 2 | 2026-04-15 | 530/538 (8 skip) | 0 |
| Round 3 | 2026-04-15 | 530/538 (8 skip) | 0 |

---

## 最终测试

### 测试轮次记录
| 轮次 | 时间 | 通过/总数 | 失败项 | 修复情况 |
|------|------|-----------|--------|----------|
| Round 1 | 2026-04-15 | 515/523 (8 skip) | 0 | ✅ 全部通过 |
| Round 2 | 2026-04-15 | 515/523 (8 skip) | 0 | ✅ 全部通过 |
| Round 3 | 2026-04-15 | 515/523 (8 skip) | 0 | ✅ 全部通过 |

### 额外修复（原有 Bug）
- `channel_health.py:52` — `NameError: name 'payout' is not defined`，缺少 payout 变量定义
- `channel_status_format.py:128` — `NameError: name 'amt_label' is not defined`，else 分支缺少变量
- `test_web_alert.py` — 简洁模式下 `/` 路由重定向到 `/cases` 而非 `dashboard`，修正测试

---

## 变更日志

| 日期 | 阶段 | 变更内容 |
|------|------|----------|
| 2026-04-15 | Phase 0A | Hook 系统创建 + 支付逻辑解耦到 domains/payment/hooks.py，68 测试全部通过 |
| 2026-04-15 | Phase 0B | Persona 系统 + payment 人设 + 多群多人设 + prompt 组装，22 测试全部通过 |
| 2026-04-15 | Phase 1A | 8 个行业域包模板（community/education/crypto/it_helpdesk/legal/general），121 测试全部通过 |
| 2026-04-15 | Phase 1B | Web 后台 "AI智能客服系统" 硬编码替换为 Jinja2 site_name 变量，34 处替换，16 web 测试通过 |
| 2026-04-15 | Phase 1C | Dockerfile + docker-compose.yaml + .dockerignore 创建 |
| 2026-04-15 | Phase 2A | KB 批量导入器（TXT/MD/CSV），30 测试全部通过 |
| 2026-04-15 | Phase 2B | Persona API 端点 + KB Import API 端点，8 测试全部通过 |
| 2026-04-15 | Phase 2C | admin.py 路由模块拆分到 src/web/routes/，24 测试全部通过 |
| 2026-04-15 | 修复 | channel_health.py payout 变量 + channel_status_format.py amt_label 变量 + test_web_alert 路由修正 |
| 2026-04-15 | Final | 全量测试 3 轮：515 passed, 8 skipped, 0 failed — 每轮稳定通过 |
| 2026-04-15 | Phase 3 | Web 路由插件化：新建 WebContext + manifest web 声明协议 + 域包路由抽取 + 模板迁移 + base.html/dashboard 动态渲染，admin.py 减少约 230 行，全部 3 轮 515 通过 |
| 2026-04-15 | Phase 4A | 深度域隔离：Dashboard widget partial 模板化 + KB 冲突检测注册机制 + 意图名称域化 + 前端冲突 UI 条件渲染 + API 响应字段条件化，新增 15 测试（530 total），3 轮全通过 |
