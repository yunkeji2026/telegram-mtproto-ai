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

**质量评测门禁**（对外可信硬指标，缺资源优雅跳过，纯核心在 `src/eval/`）：
```bash
python -m pytest tests/test_faq_resolution_gate.py tests/test_translation_quality_gate.py \
 tests/test_memory_recall_eval.py tests/test_memory_extract_eval.py \
 tests/test_persona_consistency_eval.py tests/test_emotion_eval.py \
 tests/test_crisis_response_eval.py tests/test_translation_confidence.py \
 tests/test_proactive_guard_eval.py tests/test_crisis_resource_eval.py \
 tests/test_crisis_safety_overview.py -q --tb=line
```
- FAQ 自解决率：KB 备货(≥`AITR_FAQ_MIN_ENTRIES`)时强制 ≥`AITR_FAQ_RESOLVE_TARGET`；缺库/夹生库 skip。
- 翻译回译质量：src→tgt→src 回译相似度近似质量；仅用**确定性引擎(DeepL/Google)**评（可复现、零 LLM 成本），
  缺 key/未列入 `translation.engines.order` → skip。阈值 `AITR_XLATE_SAMPLE_THRESHOLD`/`AITR_XLATE_PASS_TARGET`。
  CLI：`python -m scripts.run_eval --translation [--json]`。
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
  upsert {尝试/低置信/切换}，`/api/admin/translation-confidence-trend` 读近 N 天，ops 看板出低置信率/切换率
  7 天 sparkline。开关 `translation.engines.confidence_switch.trend_log`；门禁 `tests/test_translation_trend_store.py`。
- **危机安全总览**（T，整条安全链单一入口）：`src/eval/crisis_safety_overview.py` 聚合 L/O(主动抑制)+J(响应闭环)
  +Q(资源保障)为一张总览 + 合并 `passed`（全绿才绿），不引入新逻辑。门禁 `tests/test_crisis_safety_overview.py`，
  CLI：`python -m scripts.run_eval --crisis-overview [--json]`。
- 记忆**抽取**质量（源头质量，比召回更上游）：对消息跑真实抽取器，按 `expect`/`forbid` 子串算
  召回 + 误抽数。启发式抽取器(`extract_heuristic_facts`)是纯函数 → **常驻门禁**(召回≥`AITR_EXTRACT_RECALL_TARGET`
  且误抽≤`AITR_EXTRACT_MAX_FP`)；LLM 抽取(`ai_client.extract_memory_bullets`)缺 key → skip。
  CLI：`python -m scripts.run_eval --memory-extract [--extract-llm] [--json]`。
  （启发式自称/称呼正则已加动词/虚词护栏，防「我是说真的」类句子片段被误归名字污染长期记忆。）

### Feature flag 约定

- 新子系统默认 `enabled: false`（见 `config/config.yaml::contacts.enabled`）
- ALTER TABLE 集中到 `src/**/database.py` 的 migration 列表，不散落

### Git workflow

本 repo **2026-04-24 首次进 git**。现阶段：
- `main` 为主分支；baseline 见 `git log`（初次 import + AGENTS.md + gitignore 强化）
- 后续 feature 走 `feat-*` 分支 + PR（参考 `mobile-auto0423` 的 squash merge 流程）

### 崩溃恢复提示

- 本项目不在 git 之前的工作记录在 `DEPLOYMENT_STATUS.md` / `TODO_NEXT.md` / `docs/` 下多份 `*_PLAN.md` 与早期分析（历史文档，可能已过期，**以代码为准**）
- 已知含**虚构 model ID** `Codex-4.6-oups-high` 的 deprecated docs（不要被这些占位误导）：`CURSOR_DEVELOPMENT_GUIDE.md`、`CURSOR_HANDOFF.md`、`docs/MONITORING_PLAN.md`、`docs/MONITORING_API_SPEC.md`、`docs/ORDER_REPLY_GENERATION_ANALYSIS.md`、`docs/LOG_ANALYSIS_OPTIMIZATIONS.md`——本 repo 实际 ai provider 见 `README.md` + `config/config.yaml::ai`
- `~/.Codex/projects/C--telegram-mtproto-ai/memory/` 里 `MEMORY.md` 按项目分组，本项目条目见 "Project: telegram-mtproto-ai" 段
- 关键教训：`project_tasklist_drift.md` — 文档落后于代码，重入时以 `grep` 验证代码实况再信任任务列表

## 不在本 repo 范围（见 PROJECT_SCOPE.md）

Facebook add_friend / greeting / auto_reply / VLM Level 4 fallback 栈 → `github.com/victor2025PH/mobile-auto0423`
