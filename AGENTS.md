# telegram-mtproto-ai — Codex 项目指令

> 本仓库是多平台 AI 客服的主骨架。Codex 在本 cwd 启动时自动加载本文件。
> **边界声明**见 [`docs/PROJECT_SCOPE.md`](docs/PROJECT_SCOPE.md)（权威文档）。

## 仓库一句话

`main.py` 启 FastAPI，内嵌：contacts/handoff 子系统 + Telegram/LINE/Messenger 三端 RPA runner + skill_manager / KB / 回复生成 / 语言守卫 + Web 后台 + observability。

## Codex 在本 repo 工作时的约定

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
>
> 防陈旧字节码 flaky（曾偶发 `test_*_event_alias`）：用 `scripts/regression.ps1`（Win）/
> `scripts/regression.sh`（posix）——跑前清 `src/tests` 的 `__pycache__` 并 `PYTHONDONTWRITEBYTECODE=1`，
> 含上述超时兜底；可透传 pytest 参数（如 `scripts\regression.ps1 tests\test_x.py`）。

**仅 contacts/handoff 主线**（快速回归 — P24-D 起默认开 `-n auto` 并行，~1.9x 加速）：
```bash
python -m pytest tests/test_contacts_*.py tests/test_gateway_*.py \
  tests/test_account_limiter.py tests/test_handoff_readiness.py \
  tests/test_intimacy_engine.py tests/test_reactivation_scheduler.py \
  tests/test_handoff_*.py tests/test_cap_alert.py \
  tests/test_rpa_contact_hooks_wireup.py tests/test_contacts_runner_bridge.py \
  tests/test_rpa_shared.py tests/test_rpa_shared_yaml.py \
  tests/test_intent_tags_rate_limit.py \
  tests/test_audit_throttle.py tests/test_intent_tags_watcher.py \
  -n auto -q --tb=line
```
（去掉 `-n auto` 改单线程更利于看错误 trace；CI 默认应保留并行）
预期：全绿（contacts/handoff 主线 + intent_tags admin 闭环，含 runner→真 hooks→store bridge，
以及 P14-P26 跨平台 intent_tags 字典编辑栈：
write/diff/restore/backups/rate-limit/metrics/audit-throttle/watchdog-autoreload）。

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

**前端「哑按钮」门禁**（内联 `on*="fn()"` 引用的函数必须①有定义 ②全局作用域可达；防 `setMode`/`saveConfig`
那类「定义了但在 IIFE 内没挂 window」→ 点了抛 ReferenceError 静默无反应）+ **重复 DOM id 门禁**
（同页两个 `id="x"` → getElementById 只命中第一个、第二个元素静默失效）+ **孤儿 DOM 引用门禁**
（`getElementById('x').prop` 直接解引用但 `id="x"` 全站不存在 → null.prop 必崩）+ **动态点属性拼接门禁**
（字符串里 `X.name'+var` 拼点属性名，var 含连字符时被当减法 → ReferenceError）：
```bash
python -m pytest tests/test_inbox_inline_handlers_exported.py tests/test_rpa_inline_handlers_exposed.py tests/test_template_unique_ids.py tests/test_template_orphan_refs.py tests/test_template_dynamic_dot_access.py -q --tb=line
```
预期：全绿。扫描/作用域分析共享核心在 `tests/_inline_handler_scan.py`——会**跳过字符串/模板字面量/注释/正则**
的掩码器算括号深度，可靠区分「IIFE 内定义（不可达除非挂 window）」vs「顶层全局定义（可达）」，任意架构零假阳性；
**已扩到 `src/web/templates/**.html` 全站模板**（glob 全扫；子模板 `{% extends %}`/`{% include %}` 的跨文件全局
经 `ambient_globals`＝base+`_*.html` partial 汇入防误报）。首轮全扫已修 `agent_perf`/`workspace_dashboard`/`draft_review`
共 24 个 IIFE 内漏挂 window 的哑按钮；`personas` 的「导出/导入 JSON」按钮引用**根本不存在**的
`exportProfiles`/`importProfiles`（遗留重复卡，真面板是 `#import-panel`）已直接删除。`_PENDING_ORPHANS`（记录
「引用了未定义函数」这类需产品决策的真 bug，CI 保绿+债务可见）当前为空；`test_pending_orphans_are_still_broken` 防其过期。运行时另有兜底守卫（`unified_inbox` 内 `_wireDeadClickGuard`
+ `_rpa_shared_scripts.html` 覆盖 4 个 RPA 页）捕获 ReferenceError 弹红条（附函数名便于上报），
补静态门禁扫不到的「运行时才由 innerHTML 拼出的 handler」盲区。`referenced()` 会剔除生成期字符串拼接段
`'+helper()+'`（如 `bodyId`/`esc` 只在拼 HTML 时求值、运行时 handler 不调用），只抓真正运行时执行的调用，避免逼着无谓暴露。
**该运行时守卫已可观测化**：捕获后除弹红条，还 `navigator.sendBeacon` 到 `POST /api/telemetry/frontend-error`
（任意登录用户可写，只送消毒后的 `{page, fn, type}`——绝不送原文/查询串/堆栈），后端 `src/web/frontend_error_stats.py`
（进程级单例，风格对齐 `outbound_translation_stats`，distinct key 有上限防刷量撑爆）按 page/fn/type 累计，
经 `dump()`→`/api/workspace/metrics.frontend_errors`、`dump_prom()`→Prometheus（`frontend_errors_total` +
`..._by_{page,fn,type}_total`）观测「哪页哪函数点崩、多频」，闭合「测不到→线上也能被发现」。
ops-overview 新增「🖱️ 前端哑按钮错误」卡（`ov2_s_fe`/`ov2_js_fe_*` 键，中英齐备）展示总数/按类型 + 按页/按函数
Top8（无错=显示健康空态）。门禁 `tests/test_frontend_error_stats.py`（计数/消毒/上限/端到端 beacon→metrics）。
重复 id 门禁（`tests/test_template_unique_ids.py`）只看静态字面 id（跳过 `<script>`/HTML+Jinja 注释/`{{}}`{% %}`/拼接 id）；
已修 `dashboard`(bm-quality 串味)/`knowledge`(批量翻译按钮)/`personas`(遗留导入卡去重)/`base`(setBadge 改
querySelectorAll 同时刷桌面+移动两套导航 badge)/`whatsapp`(「对话」pane 的 P7-A 内联检索与「运维」pane 的
P11-B 共享组件检索原共用 `wa-hist-q/results`、两 pane 同在 DOM 撞车 → P11-B 换独立 `wa-ops-hist-*`，两套各自工作)；
`_ACCEPTED_DUP_IDS`（响应式镜像/互斥 Jinja 分支=假阳性）附原因登记，`_PENDING_DUP_IDS`（真 bug 待决策）当前为空，
`test_dup_id_allowlist_not_stale` 防两表过期。
孤儿引用门禁（`tests/test_template_orphan_refs.py`）**只守高置信必崩的窄不变量**——`getElementById('x').prop`/`$('x').prop`
（结果立即解引用）而 `id="x"` 全站（含 `<script>` innerHTML 生成 / `el.id=` / `setAttribute` / base+partial 跨文件）
都没有；**刻意放过防御式引用**（`?.`/`|| fallback`/`if(!p)return`，那些容忍缺失不崩，宽口径会假阳）。已修
`line_rpa`（`lr-kpi-*-foot` 直接赋值 null → 被 try 吞成 ok%/avg/1h KPI 静默不更新；改 null 安全）；
personas 的 `previewTTS` 死代码（从不被调用、引用不存在的 `vp-*`，真编辑器用 `pe-vp-*`）已删除、其 5 个
仅此处用的 `psn_js_097..101` i18n 键一并回收，门禁强度恢复。`_PENDING_ORPHAN_REFS` 现仅剩 unified_inbox
未落地的声纹登记内联面板 `ve-*`（一整套 window 暴露 + `inbox.voice.*`，且已有出货副驾组件 `cp-voice.js` 走
同源 API；「落地内联面板 or 判定被取代后移除」属产品决策，如实追踪），`test_pending_orphan_refs_still_orphan` 防过期。
动态点属性拼接门禁（`tests/test_template_dynamic_dot_access.py`）抓「字符串里点访问+拼接扩展标识符」（`X.name'+var`
这种把代码当串拼、按变量拼点属性名的 code-gen 陷阱；普通手写 `a.b` 不会紧跟 `'+` 故不误伤）。已修
`_rpa_shared_scripts.html::initSearch`：原按 `inputId` 拼 `(window.__rpaPick_'+inputId+')(...)` inline onclick，
三个 RPA 调用方 inputId 全含连字符（`wa-ops-hist-q`/`lr-hist-q`/`mr-hist-q`）→ `window.__rpaPick_wa-ops-hist-q`
被解析成减法 → 点搜索结果开会话抽屉在 LINE/Messenger/WhatsApp 三页全坏（且触发 dead-click 红条兜底）；
改**事件委托 + `data-rpa-ck` 属性**（结果容器一次绑定处理所有动态行，彻底去掉「每输入框全局函数+dot 访问」脆弱模式）。
`_ALLOWLIST`（良性命中，如字面量以 `.ext` 结尾再拼变量的文件名串）当前为空，`test_allowlist_not_stale` 防过期。

**陪伴能力「分阶段开启」主线**（看→校→开→观测→纠偏 闭环；纯函数 core 在 `src/companion/`，
路由 `src/web/routes/companion_capability_routes.py` 挂 `/api/companion/capabilities*`，
看板卡片在 `rpa_overview.html`，配置体检接进 `ops-overview`）：
```bash
python -m pytest tests/test_companion_capability_status.py \
 tests/test_companion_delivery_calibration.py tests/test_companion_capability_toggle.py \
 tests/test_companion_capability_presets.py tests/test_companion_readiness_signals.py \
 tests/test_companion_capability_advisor.py tests/test_companion_proactive.py \
 tests/test_outbound_translate.py tests/test_autosend_worker_translate.py \
 tests/test_ops_overview.py tests/test_admin_route_inventory.py -q --tb=line
```
预期：全绿（能力就绪度看板 + 真发开闸校准 + 带护栏 toggle/overlay 写入 + 一键预设档/快照回滚 +
决策信号 + 档×信号联动建议/一致性体检 + 出站自动翻译闭环 + ops-overview 配置健康灯 + 路由契约）。
关键不变量：真发主开关 `inbox.l2_autosend.deliver` 双重 opt-in（worker on + auto_ai 会话），
所有开关写经 `config.local.yaml` overlay（保住主配置注释），单切/预设/回滚/一键修复均过同一护栏；
出站自动翻译（`inbox.l2_autosend.translate.enabled`）覆盖 **L2 autosend + 主动触达(care/reactivation
经 deferred 队列)**，投递前把消息译成会话客户语言；**自带源语言检测护栏**——文本已是客户语言
（陪伴回复/reactivation 本就按客户语言生成）即跳过不译，防 garble；任何异常/不可译/译文==原文
一律回落发原文，**绝不阻塞投递**。

**每人设「相册/媒体」主线**（图/视频备货 + 触发词自动发；DB 注册表 `src/companion/persona_media_store.py`
＝`config/persona_media.db`，纯函数匹配器 `persona_media.py`，探针 `media_probe.py`，路由
`persona_media_routes.py` 挂 `/api/personas/{pid}/media*`，UI 在 `personas.html` 相册面板，
迁移 CLI `scripts/import_persona_albums.py`。详见 `docs/PERSONA_MEDIA_ALBUMS.md`）：
```bash
python -m pytest tests/test_persona_media.py tests/test_persona_media_routes.py \
 tests/test_persona_media_import.py tests/test_media_probe.py \
 tests/test_selfie_wiring.py tests/test_image_autosend.py -q --tb=line
```
预期：全绿。关键不变量：命中相册**优先于**AI 现场出图（两条链 Stage 0：autosend `run_autosend_image` +
skill_manager `_handle_persona_media_request`）；关键词池独立于自拍/物体意图、通用池仅泛化要图时放开、
`min_bond_level` 关系闸门、加权轮播+会话内避重；护栏＝扩展名白名单/体积(图10M/视频50M)/视频时长(3min,
仅 ffprobe 可探时拦)/sha256 去重/路径消毒/viewer 只读/审计(`pmedia_*`)；探针（ffprobe 时长宽高、ffmpeg
封面、PIL 图宽高）全软失败不阻塞上传；多语配文 `caption_i18n` 随会话语种取文；观测经
`/api/workspace/metrics.persona_media` + Prometheus `ws_persona_media_*` + ops-overview「🖼️ 人设相册」卡。
总开关沿用 `companion.selfie.enabled`。

**外部 worker 会话健康 + Messenger 受控降级主线**（2026-07：网页链路不稳的止血与自愈闭环）：
```bash
python -m pytest tests/test_messenger_send_semantics.py tests/test_platform_session_health.py \
 tests/test_platform_session_selfheal.py tests/test_auto_draft_platform_modes.py \
 tests/test_alert_delivery_e2e.py tests/test_admin_route_inventory.py -q --tb=line
```
预期：全绿。链路：messenger-web / whatsapp-baileys(Node) 在登录/掉线/放弃自愈时 POST
`/api/internal/protocol/session-status` → `src/integrations/platform_session_health.py`
（进程级登记表）→ 转移告警（EventBus `platform_session_alert`，订阅别名 `platform_session`，
事件带 `rate_key=platform:acct` 防多账号挤限流窗）→ worker send/send_media 快速失败闸
（`_session_unhealthy`，仅拦自动路径）→ ops 卡「🔌 平台会话健康」+ 不健康 messenger 行
「重新登录」按钮（`POST /api/admin/platform-sessions/relogin` → Node `/accounts/:id/relogin`
同 profile 重启 + 30min 交互窗）。持续掉线由 `HealthWatchdog._check_platform_sessions`
升级式提醒（`health_watchdog.session_stale_remind`，默认 30min 首提/4h 重提，恢复自动清零）。
Node 侧不变量：composer 清空 + 回读气泡二次确认（失败标记→502 如实上报；回读不定态按已送达防重发刷屏）；
崩溃快自愈（退避×5）放弃后仍有 15min 慢重试兜底；WA 意外断线自动重连（修「假在线」）。
Messenger 自动化档位经 `inbox.auto_draft.platform_modes: {messenger: review}` 封顶
（`cap_automation_mode`，AI 只拟稿人审后发；恢复全自动删该行重启）。
messenger-web `start.ps1` 显式 `MSG_RESTORE_ON_BOOT=1`（headed 也开机恢复，解主进程启动顺序依赖）。

**质量评测门禁**（对外可信硬指标，缺资源优雅跳过，纯核心在 `src/eval/`）：
```bash
python -m pytest tests/test_faq_resolution_gate.py tests/test_translation_quality_gate.py \
 tests/test_memory_recall_eval.py tests/test_memory_extract_eval.py \
 tests/test_persona_consistency_eval.py tests/test_emotion_eval.py \
 tests/test_crisis_response_eval.py tests/test_translation_confidence.py \
 tests/test_proactive_guard_eval.py tests/test_crisis_resource_eval.py \
 tests/test_crisis_safety_overview.py tests/test_voice_language_eval.py -q --tb=line
```
- FAQ 自解决率：KB 备货(≥`AITR_FAQ_MIN_ENTRIES`)时强制 ≥`AITR_FAQ_RESOLVE_TARGET`；缺库/夹生库 skip。
- 翻译回译质量：src→tgt→src 回译相似度近似质量；可评引擎＝**确定性引擎(DeepL/Google)** 或
 **本地 MT(ollama_mt，评测器强制 temp=0 贪心=可复现)**，均缺 → skip。CLI `--xlate-engine
 auto|deterministic|ollama_mt|ai`（auto=DeepL/Google→ollama_mt 顺位；ai=DeepSeek 仅横比不进门禁）；
 evaluator 读 config 时会合并 `config.local.yaml` overlay 并对 Ollama 端点做 /api/show 探针（端点宕/
 模型缺→skip 而非全 0 假 FAIL）。本地 MT 实景门禁 opt-in：`AITR_XLATE_LOCAL_MT=1`（CI 默认不依赖
 局域网 GPU）。宽语种集 `config/eval/translation_samples_hymt.yaml`（30 样本×17 语）。
 阈值 `AITR_XLATE_SAMPLE_THRESHOLD`/`AITR_XLATE_PASS_TARGET`。
 CLI：`python -m scripts.run_eval --translation [--json]`。
 **语义轨**（P2）：有嵌入 provider（`embedding_providers.build_embed_fn`，本仓生产=140 bge-m3）时
 自动补嵌入余弦 `semantic`；字符轨不合格但语义 ≥ 阈（默认 0.8，`AITR_XLATE_SEM_THRESHOLD` /
 `--xlate-sem-threshold`）→ 按合格记 `rescued=True`——救「正确的意译」（「九折」→回译「10%的折扣」
 字符 0.39/语义 0.84）。阈值 0.8 依 bge-m3 实测校准：意译区 0.84-0.93 / 同域错义区 0.61-0.74 /
 跑题区 <0.42，落干净间隔中。嵌入失败软降级纯字符轨，绝不因端点抖动崩评测。`--xlate-semantic off` 关。
 **交叉回译**（P2）：`--xlate-back-engine same|deterministic|ollama_mt|ai`——同引擎自回译会给
 「复读自己措辞」的引擎虚高字符分；正/回向分属两引擎时偏置对称抵消，横比才公平。
 **周批趋势**：计划任务 `TranslationEvalWeekly`（周六 06:30，`scripts/translation_eval_weekly.ps1`）
 跑默认+宽集，`--out-jsonl` 摘要追加 `logs/eval/translation_trend.jsonl`。
 2026-07-11 实测基线（宽30样本×17语）：字符轨自回译 HY-MT 0.677 vs DeepSeek 0.750 看似落后，
 但**交叉回译+语义轨**下 HY-MT-fwd 0.933 vs DeepSeek-fwd 0.922——字符差距主要是复读偏置+意译压分
 假象；语义口径本地 MT 持平略胜（vi/es/ru +0.05~0.07，hi -0.075），两集 100% PASS（语义救回 3 例意译）。
- 记忆召回质量：真实 `EpisodicMemoryStore` 端到端跑 `get_bullets_for_prompt`，对比关键词 vs 向量融合召回率；
  机制自测用确定性本地嵌入(离线可复现)，真实语义增益需真实嵌入(不可用则 skip)。门禁 `top_k` 默认 3
  （须 < 每场景事实数才鉴别排序；实测 keyword 80% vs vector 100%/+20%）。
  CLI：`python -m scripts.run_eval --memory [--json]`。开向量召回走能力看板 `memory.vector.enabled` 治理化开启
  （非盲改默认；degrade-to-keyword 零阻断）。
- 记忆语义去重：跨真实 `merge_near_duplicates`(R5)，近义改写应并、异义事实不应过并；只在**真实语义嵌入**下有意义。
  CLI：`python -m scripts.run_eval --semantic-dedup [--dedup-threshold 0.7]`。门禁 `tests/test_memory_recall_eval.py`(缺嵌入 skip)。
- **真实嵌入 provider**（`src/eval/embedding_providers.py`，解锁上面两项从 skip→实跑）按序探测：
  ① OpenAI 兼容端点(env `AITR_EMBED_BASE_URL/MODEL/API_KEY` 或 config `ai.embedding_base_url/embedding_model`，
  LM Studio/Ollama/OpenAI)；② 本地 sentence-transformers(**opt-in** `AITR_EMBED_LOCAL=1`，默认多语
  `paraphrase-multilingual-MiniLM-L12-v2`，免 key、模型缓存后离线，避免默认 CI 背 torch 冷加载)；均无 → skip。
  生产开向量/去重：配 `ai.embedding_*` + `memory.vector.enabled` / `memory.consolidation.semantic_dedup`。
- 人设一致性（陪聊"真人感"最后防线）：`persona_guard` 是否抓全客服腔/AI 自曝（违规召回，漏一个=事故）
  且不误伤合规（含"我才不是AI啦"否定句）；纯函数常驻门禁。CLI：`python -m scripts.run_eval --persona`。
  阈值 `AITR_PERSONA_RECALL_TARGET`(默认 1.0)/`AITR_PERSONA_MAX_FP`(默认 0)。
- 情绪识别：① 情绪维度准确率(`analyze_emotion`，多分类，阈 `AITR_EMOTION_ACC_TARGET` 默认 0.8)；
  ② 危机识别(`detect_crisis` 安全红线，severe 召回须 1.0、惯用语零误报，`AITR_CRISIS_RECALL_TARGET`/`AITR_CRISIS_MAX_FALSE_ALARM`)。
  CLI：`python -m scripts.run_eval --emotion` / `--crisis`。
  **I 否定硬化**：`analyze_emotion` 情绪词命中加否定前瞻（不/没/别/not），「不难过/没那么累/别担心/not sad」
  不再误判负面/低能量；`tests/test_emotion_eval.py::test_negation_not_misclassified` 回归网量化。
- **危机响应闭环**（J，识别→处置端到端安全，安全侧最重门禁）：`src/eval/crisis_response_eval.py`
  复刻 `SkillManager._apply_crisis_safety_net`——severe/elevated 输入须注入安全指令(预防)；回复触自伤红线
  必被 `safe_fallback_reply` 整段覆盖、劝阻句(「别去死」)不可误覆盖；**终态输出 100% 不含鼓励自伤片段**(硬红线)。
  纯函数常驻门禁 `tests/test_crisis_response_eval.py`。CLI：`python -m scripts.run_eval --crisis-response`。
- **译文在线置信度 + 引擎智能切换**（K）：`src/ai/translation_confidence.py` 确定性评分(空/未翻译/错语种/长度异常)，
  `EngineRouter(min_confidence>0)` 在主引擎低置信时自动切换下一引擎择优(都不达标→最高分候选，不阻断)；
  生产开关 `translation.engines.confidence_switch.{enabled,min_confidence}`(默认关=旧行为)。scorer 门禁
  `tests/test_translation_confidence.py`(纯函数常驻)。CLI：`python -m scripts.run_eval --xlate-confidence`。
- **按语种引擎覆写 + 在线语义闸门**（K2）：`translation.engines.per_lang_order`（如 `{hi: [ai, ollama_mt]}`）
  把评测实锤的弱语对重排到强引擎优先——只重排 order 内引擎（未知名忽略），覆写外引擎按默认序补尾兜底，
  其余语种不受影响（hi 三样本 A/B：同 AI 回译口径 MT char=0.757 vs AI 0.884 → 已上线覆写）。
  `confidence_switch.semantic.{enabled,min_similarity}`（默认关）＝确定性信号的盲区补丁：确定性达标的译文
  再比对 源/译 跨语言 bge-m3 余弦（走 ai_client.embed，~50ms），低于阈值同低置信处理（切换/择优），
  嵌入失败/返空一律放行（fail-open 不阻塞）；源文 <4 有效字符（"OK"/"哈哈"类）直接跳过（嵌入噪声大且
  漂移风险≈0，省一次往返）。阈值 0.65 依宽语料 44 对离线校准：真实译文 min=0.712/p5=0.775，
  错配内容 max=0.741/p95=0.683（zh→fr/hi 正确译文天然低分 → 阈值再高会误切）。观测：
  `translation_engine_semantic_low_total`(Prom) / `metrics.translation_engines.semantic_low` / ops 卡「语义闸门拦截」
  （i18n 键 `ov2_js_sem_low`）。门禁 `tests/test_translation_engines.py`（覆写路由/兜底/describe + 语义切换/fail-open/短文本跳过/全低择优）。
  **嵌入双活**：`ai.embedding_base_urls`（列表，优先于单数键）= 140+176 双 bge-m3 端点，`ai_client.embed()`
  按序尝试、异常端点 60s 冷却降权（不剔除）、**全端点失败才计全局熔断 streak**（单点抖动零感知）；
  其他嵌入消费方（KB embed-all/eval provider/readiness）仍读单数键 `embedding_base_url`（保持 140）。
  门禁 `tests/test_ai_client_embed_failover.py` + readiness 认列表键（`tests/test_companion_embedding_readiness.py`）。
- **评测语料双向化**：`TransSample.source_lang` 显式标注源语（优先于探测——短句探测不可靠），反向进站样本
  （en/ja/ko/th/vi/id/es/ru→zh，12 条）已入宽集 `config/eval/translation_samples_hymt.yaml`（现 44 样本：
  zh→xx 32 + xx→zh 12；HY-MT 全绿 pass=44/44，语义均分 0.939，xx→zh 方向 char 均分 0.79 高于 zh→xx 0.68）。
- **主动护栏闭环**（L，情绪安全闸门）：`src/eval/proactive_guard_eval.py` 把所有主动路径共用的
  `proactive_emotion_gate` 当安全不变量回归——**severe 窗口内必 block**(漏判=最脆弱时还推剧情)、窗口外正确退化、
  负面末条→soft、正面/中性不过度沉默。门禁 `tests/test_proactive_guard_eval.py`。CLI：`--proactive-guard`。
- **翻译置信度上线观测**（M）：`TranslationEngineStats` 增 `low_confidence`/`confidence_switches` 计数，
  经 `dump()`→`/api/workspace/metrics`、`dump_prom()`→Prometheus(`translation_engine_low_confidence_total`/
  `..._confidence_switches_total`)，无需新路由；观测「切了多少、值不值」。
- **情绪强度分级**（N）：`analyze_emotion` 程度副词缩放 intensity(「有点累」0.39<「累」0.6<「累死了」0.78)，
  只改强度不改标签(→arousal/valence/记忆 salience，否定/维度判定不受影响)。门禁
  `tests/test_emotion_eval.py::test_intensity_grading_monotonic`。CLI：`--emotion-intensity`。
- **情绪强度落库 + 护栏分级**（O，打通 N→L）：ingest 用 `analyze_emotion` 量级补 `conversation_meta.last_emotion_intensity`
  (列默认 -1=未知；标签仍来自规则分类器，强度正交)；`proactive_emotion_gate(last_emotion_intensity,min_negative_intensity=0.5)`
  使「有点焦虑」(低强度)不抑制剧情邀约、「很焦虑」才 soft——**危机分级不受强度影响**、强度未知保守按旧行为。
  经 `build_proactive_opener`→`companion_proactive` 主动开场路径透传。门禁 `tests/test_proactive_guard_eval.py`。
- **翻译置信度看板**（P）：ops_overview 新增「🌐 翻译引擎」卡，读 `/api/workspace/metrics.translation_engines`
  展示翻译尝试/低置信率/智能切换次数/降级次数 + 每引擎成功率延迟（M 的计数可视化）。
- **危机资源保障**（Q，安全处置延伸）：`src/eval/crisis_resource_eval.py` 复刻 `_apply_crisis_safety_net` 资源分支——
  severe+开 `crisis_resource_assurance`+有热线+回复无资源→**补一次**(热线只现一次)、已含资源/非severe/无热线/关→不补、
  红线优先(有害先覆盖)。门禁 `tests/test_crisis_resource_eval.py`。CLI：`--crisis-resource`。
- **情绪强度全路径透传**（R，O 的覆盖补齐）：`last_emotion_intensity` 经 `daily_ritual`/`milestone_ritual` 透传进
  早晚安(`build_ritual_opener`)、纪念日/节日(`build_milestone_opener`)、槽位采集(`build_profile_ask_opener`)三条
  ritual 路径——与主动开场(O)同口径走 `proactive_emotion_gate` 强度分级（轻度负面不过度沉默）。
- **翻译置信度趋势化**（S，P 的时序延伸）：`src/ai/translation_trend_store.py`（仿 `tts_cost_store`，默认关）按日
  upsert {尝试/低置信/切换/语义闸门 sem_low}（旧库经幂等 ALTER 迁移补列），`/api/admin/translation-confidence-trend`
  读近 N 天，ops 看板出低置信率/切换率/语义闸门率 7 天 sparkline（语义线仅在有命中时显示）。
  开关 `translation.engines.confidence_switch.trend_log`；门禁 `tests/test_translation_trend_store.py`。
  **周批语对拆分**：`evaluate_translation_quality` 的 `summary.by_pair`（`{src->tgt: {n,passed,char_mean,sem_mean}}`）
  随 `--out-jsonl` 趋势行携带；`scripts/translation_eval_weekly.ps1` 宽语料一周三口径（默认集 + 宽集同引擎 +
  宽集交叉回译 `--xlate-back-engine ai`，行内 `back_engine` 区分）→ 弱语对该不该进 `per_lang_order` 直接读周数据。
- **危机安全总览**（T，整条安全链单一入口）：`src/eval/crisis_safety_overview.py` 聚合 L/O(主动抑制)+J(响应闭环)
  +Q(资源保障)为一张总览 + 合并 `passed`（全绿才绿），不引入新逻辑。门禁 `tests/test_crisis_safety_overview.py`，
  CLI：`python -m scripts.run_eval --crisis-overview [--json]`。
- 记忆**抽取**质量（源头质量，比召回更上游）：对消息跑真实抽取器，按 `expect`/`forbid` 子串算
  召回 + 误抽数。启发式抽取器(`extract_heuristic_facts`)是纯函数 → **常驻门禁**(召回≥`AITR_EXTRACT_RECALL_TARGET`
  且误抽≤`AITR_EXTRACT_MAX_FP`)；LLM 抽取(`ai_client.extract_memory_bullets`)缺 key → skip。
  CLI：`python -m scripts.run_eval --memory-extract [--extract-llm] [--json]`。
  （启发式自称/称呼正则已加动词/虚词护栏，防「我是说真的」类句子片段被误归名字污染长期记忆。）
- **语音合成语言一致性**（U，防「中文声纹念英文」）：`src/eval/voice_language_eval.py` 复刻发声路径共用的
  `voice_clone_client.effective_clone_language`——克隆合成送主机的 `language` 须随**待合成文本实际语种**
  （中文回复仍 zh=行为不变；英文/他语回复由默认 zh 纠正，防按中文音系发音 garble；无法判定/空→回落账号默认）。
  覆盖 autosend / 原生 voice_reply / 手动坐席三条链路同一瓶颈。纯函数常驻门禁 `tests/test_voice_language_eval.py`。
  CLI：`python -m scripts.run_eval --voice-language [--json]`。阈值 `AITR_VOICE_LANG_ACC_TARGET`(默认 1.0)。
- **语音情绪 GPU 化**（SER 远程主路）：176 音频服务（`scripts/asr176/`，与 GPU ASR 同进程同任务）加
  `POST /v1/audio/emotion`（emotion2vec_plus_large CUDA，warm ~44ms vs 117 CPU plus_base 秒级）；
  服务端只回 `{labels,scores}` 原始数组，**标签→系统语义映射仍在客户端** `speech_emotion.py` 单一出口。
  客户端 `speech_emotion.remote.{base_url,timeout_sec,cb_cooldown_sec}`（config.local）＝远程优先，
  失败进 120s 冷却回落本地 funasr CPU（远程可用时不受本地加载熔断牵连），语音链零阻断。
  观测：`SpeechEmotionStats.remote` → `speech_emotion_remote_total`(Prom) + ops「🎧 音频情绪」卡
  「远程 GPU 占比」（键 `ov2_js_se_remote`）。门禁 `tests/test_speech_emotion.py`（远程成功/失败回落/冷却/
  本地断路器不牵连/无 remote 旧行为）。模型获取教训见 `scripts/asr176/README.md`（176 hub 下载不可靠，
  117 下载→scp）。
- **176 音频服务自愈 + 预热**：`AITR_WARMUP`(默认 1) 启动即后台预载 ASR+SER（消重启后 ~15s/~6s 冷启，
  `/health` 出 `asr_loaded/ser_loaded`）；计划任务 `AITR_ASR_WATCHDOG`(每 5min) 跑 `watchdog_asr.ps1`
  ——health 8s 无响应经计划任务自动重启（ONSTART 只保开机，白天崩了会静默降级 CPU，看门狗闭环）。
- **视觉(VLM)双活**（`vision.base_urls`，2026-07）：176(5090,主)+140(4070,备)各备 `qwen2.5vl:7b`，
  `VisionClient` 多端点按序试、异常端点 60s 冷却降权（**模块级**状态——实例按调用即建即弃）；
  端点通但空答不切端点（省第二块 GPU），全端点异常仍走旧智谱云兜底。所有消费方
  （TG/LINE/Messenger/WA RPA + 图片翻译 OCR）经同一类自动获益。`_wants_openai_primary`/
  `has_any_vision_backend` 认 `base_urls`。门禁 `tests/test_vision_fallback.py`（解析/切换/冷却重排/
  全冷却硬试/空答不切）。140 冷载实测 130s（timeout 150 覆盖）、热态 ~5s。
- **166 旧主机引用清理**（网段迁移遗留，2026-07-11）：`messenger_rpa.audio_pipeline` → 176 GPU ASR
  （同 OpenAI 契约）；`whatsapp_rpa.voice_output` coqui_http→166 改 `minicpm_clone` 本机 IndexTTS2
  （与 TG voice_reply 同栈，失败回落 edge_tts）；`ai.embedding_base_url` 基线值 192.168.1.43(旧 Wi-Fi)
  → 192.168.0.140。`faceswap`(166:8000) 无替代主机，已知死配置待产品决策。140 双默认网关经核实
  metric 已分明（以太网 25 vs WLAN 326，Windows 自动降权），不动网络配置。
- **翻译趋势周报 CLI**：`python -m scripts.xlate_trend_report [--json]` 把周批 JSONL 按
  (dataset,engine,back_engine) 分组渲染趋势表 + 最新弱语对 Top-K（sem 升序，n<2 标注），
  周审读数即可决策 per_lang_order/阈值。门禁 `tests/test_xlate_trend_report.py`。

### i18n 施工约定（后台路由 CJK 收口 + 前端裸键）

后台 API 的 `detail`/`error` 文案前端 verbatim 直显 → 硬编码中文会漏给英文用户。收口靠请求级
`tr(request, key, default=None, /, **fmt)`（`src/web/web_i18n.py`），从 `request.state.ui_lang`
取语言出译文。**所有后台 routes 现已收口至 0 CJK**，靠 ratchet 门禁 `test_route_response_cjk_ledger_ratchet`
（`_ROUTE_CJK_CEILINGS` 为**非增天花板**，新增硬编码中文即红）守住。

**新路由族批量收口标准流程**（工具 `scripts/i18n_routeconv.py`）：
1. `python -m scripts.i18n_routeconv --coverage-all` 选靶（ratio=1.0 可一把过；<1.0 差集是硬骨头）。
2. `--suggest ROUTE_FILE` 出键匹配建议（reuse 现有键 / new 新键）；优先复用 `err.svc.*` / `err.rpa.*` /
   `err.ws.field_required` 等共享词汇，参数化（`{field}`/`{name}`/`{dep}`）而非造同义新键。
3. driver 里 **`convert_file` 的 `scope_check` 现为缺省 `True`**（P43e；勿轻易传 `False`）：施工**前**跑
   `scope_precheck` 剔除落在无 `request` 作用域的映射（不动源码、列 `scope_skipped`），从源头杜绝
   `tr(request,…)` 写进无 `request` 的 helper 而运行时 `NameError`。**任何 helper 若要调 `tr` 必须把
   `request` 收进形参**并改所有调用点。
4. 事后 `--verify-scope ROUTE_FILE` 复核（应空）；在 web_i18n.py 补齐 zh/en 两套键。

**两条硬护栏**（勿踩）：
- 占位符名**不得**叫 `request`/`key`/`default`（会与 `tr` 形参撞名；`tr` 已用位置限定 `/` 兜底，
  另有门禁 `test_i18n_placeholders_avoid_reserved_names` 从源头禁用）。历史坑：`err.rpa.config_missing`
  曾用 `{key}` → 改 `{name}`。
- 新键必须 zh+en 双语补全。前端 `window.T('key')` **不留中文兜底**（回落只显裸键名）；每个**静态**
  `window.T`/`Tf` 键必须在 web_i18n.py 存在，由全库门禁 `test_template_window_t_keys_resolve`
  （`templates/**/*.html` 递归 63 页，零缺失）守住。CLI 键覆盖自检见 `tests/test_i18n_coverage.py`。

### Feature flag 约定

- 新子系统默认 `enabled: false`（见 `config/config.yaml::contacts.enabled`）
- ALTER TABLE 集中到 `src/**/database.py` 的 migration 列表，不散落

### Git workflow

本 repo **2026-04-24 首次进 git**。现阶段：
- `main` 为主分支；baseline 见 `git log`（初次 import + AGENTS.md + gitignore 强化）
- 后续 feature 走 `feat-*` 分支 + PR（参考 `mobile-auto0423` 的 squash merge 流程）
- **多 agent 并发用 `git worktree` 隔离**：普通分支只隔离提交历史，多个 agent 仍共用
  同一工作目录 → 文件互相串改、`index.lock` 互撞（本仓曾反复踩）。各 agent 各开
  `git worktree add -b feat-xxx ../telegram-mtproto-ai-xxx <base>`（独立工作目录 +
  独立 index、共用 .git refs），冲突只在 merge 时显式解决；收尾 `git worktree remove`。

### 崩溃恢复提示

- 本项目不在 git 之前的工作记录在 `DEPLOYMENT_STATUS.md` / `TODO_NEXT.md` / `docs/` 下多份 `*_PLAN.md` 与早期分析（历史文档，可能已过期，**以代码为准**）
- 已知含**虚构 model ID** `Codex-4.6-oups-high` 的 deprecated docs（不要被这些占位误导）：`CURSOR_DEVELOPMENT_GUIDE.md`、`CURSOR_HANDOFF.md`、`docs/MONITORING_PLAN.md`、`docs/MONITORING_API_SPEC.md`、`docs/ORDER_REPLY_GENERATION_ANALYSIS.md`、`docs/LOG_ANALYSIS_OPTIMIZATIONS.md`——本 repo 实际 ai provider 见 `README.md` + `config/config.yaml::ai`
- `~/.Codex/projects/C--telegram-mtproto-ai/memory/` 里 `MEMORY.md` 按项目分组，本项目条目见 "Project: telegram-mtproto-ai" 段
- 关键教训：`project_tasklist_drift.md` — 文档落后于代码，重入时以 `grep` 验证代码实况再信任任务列表

## 不在本 repo 范围（见 PROJECT_SCOPE.md）

Facebook add_friend / greeting / auto_reply / VLM Level 4 fallback 栈 → `github.com/victor2025PH/mobile-auto0423`
