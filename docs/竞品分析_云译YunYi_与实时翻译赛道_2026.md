# 竞品分析：云译 YunYi GPT 与「跨境社交实时翻译」赛道（2026-06）

> 触发：体验 YunYi GPT（[yunyiai.app](https://yunyiai.app/)）客户端后，评估其与本平台的竞争力，并据此规划「模仿并超越」的开发项。
> 与 [`竞品对比与市场定位_2026.md`](竞品对比与市场定位_2026.md) 互补：那篇打 Intercom/Gorgias/Respond.io（SaaS 客服赛道），本篇打**实时翻译工具赛道**（云译/易翻译/悟空翻译）。
> 结论为工程/产品判断，引用价格与功能随竞品官网更新，使用时复核。

---

## 1. 竞品画像：云译 YunYi GPT

**定位**：出海跨境社交**实时翻译专家**，Windows 桌面客户端，底层驱动注入主流通讯软件聊天窗口，做毫秒级双向翻译。同类还有**易翻译**（yifanyiscrm.com）、**悟空翻译**（wk.app）。

### 1.1 功能清单（从官网 + 客户端截图提取）

| 模块 | 能力 |
|---|---|
| **实时双向翻译** | 收/发消息自动识别语种并翻译，200–400 语种，按用户单独配置语种 |
| **多翻译引擎** | 6 大通道：有道 / 百度 / 谷歌 / DeepL / ChatGPT / Gemini，可切换 |
| **多模态翻译** | 文本 / 语音 / 视频流 / 图片识别翻译 |
| **AI 智能回复** | 基于上下文语境实时生成「地道文案」 |
| **话术库 / 自定义快捷回复** | 预设话术一键插入 |
| **打粉计数器** | 统计加粉 / 触达量（营销 KPI） |
| **多平台聚合** | WhatsApp / Telegram / Line / Facebook / Messenger / Instagram / Signal / Twitter / Skype / TikTok / Discord / Snapchat / Zalo / Tinder / Teams / 自定义网站 等 18+ |
| **多开 / 多账号 / 子账号** | 沙盒多开，账号矩阵切换，子账号管理 |
| **TG 群发** | Telegram 批量营销 |
| **计费** | 按字符量（基础/高级/专业/包月）或端口数（易翻译 $48–1300/月） |

### 1.2 它的本质

**「翻译 + 轻营销」的个人效率工具**，而非客服系统。卖点是「让不会外语的人也能跨语种聊天」。AI、话术库、计数器都是**翻译之外的轻量增值**，没有：服务端持久化、多坐席协作、SLA、知识库检索、客户旅程/漏斗、风险分层、关系阶段、质检。

---

## 2. 正面对比：本平台 vs 云译

| 维度 | 云译 YunYi | 本平台 | 谁强 |
|---|---|---|---|
| 实时双向翻译 UX | ★★★★★ 旗舰、毫秒级、内嵌原生窗口 | ★★☆ 有 translate API + 单条「译」按钮，无自动双向 | **云译** |
| 翻译引擎数 | 6 通道可切 | 1 条（ai provider）+ translation_memory | **云译** |
| 多模态翻译 | 文本/语音/图片/视频 | 文本为主 | **云译** |
| 多平台覆盖 | 18+ App（含 Tinder/Snapchat/Discord） | TG/LINE/Messenger/WhatsApp/Web（RPA 个人号） | **云译**（广度）/ 本平台（深度+个人号API触达不到的能力） |
| AI 回复 | 单轮「地道文案」 | KB bm25 + 四层 trigger + 关系阶段 + copilot + 流失预测 | **本平台** |
| 客服平台能力 | 无 | 统一收件箱 / 多坐席租约锁 / SLA / 标签 / 升级 / 跟进任务 / 日报 | **本平台** |
| 客户资产 | 打粉计数（数字） | contacts/journey/handoff 漏斗 + 360 视图 + 关系演进时间线 | **本平台** |
| 数据主权 | SaaS 客户端 | 私有部署 + SQLite 事实源 | **本平台** |
| 协作 | 单人单机 | 多坐席 SSE 实时协作 | **本平台** |
| 上手成本 / 即时性 | 极低，下载即用 | 需部署 | **云译** |

**一句话**：云译是「单兵翻译外挂」，本平台是「团队 AI 客服中台」。两者**翻译 UX 上有真实差距**，但本平台在 AI 深度、协作、客户资产沉淀上是降维优势。

---

## 3. 战略判断

1. **不放弃翻译 UX 这一格**：跨境私域客服天然是跨语种场景，翻译体验差 = 坐席嫌难用。云译证明了「实时双向翻译」是高频刚需，我们必须把工作台的翻译体验做到不输它。
2. **用「平台」吃掉「工具」**：云译的翻译是孤立的；我们把翻译嵌进**有 KB / 关系阶段 / 漏斗 / 多坐席**的中台里——同样一句翻译，我们能复用翻译记忆、绑定客户语言偏好、喂给 copilot、计入质检。这是工具型竞品结构上给不了的。
3. **「打粉计数器」升维成「转化漏斗计数器」**：云译只数加了多少粉；我们已有 contacts/journey，可直接做「今日新增/触达/进入各漏斗阶段/成交」的实时计数条，信息量碾压。

---

## 4. 模仿并超越：开发路线图

> 原则：**先补齐云译的旗舰翻译 UX（追平），再用平台能力做它做不到的（超越）**。

### P55（本期）— 实时双向翻译 + 工作台美化【追平 + 局部超越】

**追平云译：**
- **入站自动翻译**：开启后，对方消息气泡下自动显示译文（目标=坐席语言），无需逐条点「译」。
- **发送前翻译**：坐席用母语撰写 → 发送时自动译成**对方语言**再投递（带「译文预览 + 确认」防误发）。
- **每会话语言记忆**：自动记住每个会话的「对方语言/我的语言」，切回会话即恢复（localStorage + 后续落库到 contact）。

**超越云译：**
- **翻译记忆复用**：命中 `translation_memory.db` 直接回填，省 token、保证术语一致（云译每次都重新翻）。
- **与 copilot/KB 联动**：译文与 AI 副驾、知识库推荐共用同一输入框，翻译只是管线一环。

**工作台美化：**
- 翻译条重做为「双向翻译」开关组（收到→母语 / 发送→对方语言 + 引擎标识）。
- 输入区、信息侧栏视觉层级优化；顶部「今日数据」计数条（见下）。

### P55 附带 — 转化漏斗计数器【超越打粉计数器】

会话列表顶部加一条「今日」实时数据条：**今日新增联系人 / 今日活跃会话 / 待回复 / 已成交**，数据从现有 contacts/inbox 派生，零新增重表。

### P56（已完成 2026-06-07）— 多引擎翻译 + 术语库【追平多引擎 + 超越】

**已交付：**
- `src/ai/translation_engines.py`：`TranslationEngine` 接口 + `AIEngine`/`DeepLEngine`/`GoogleEngine` + `EngineRouter`（按 `config.translation.engines.order` 故障转移，首个「可用且非空」获胜，AI 兜底）。DeepL/Google 缺 key 或缺 aiohttp 自动 `available=False`，路由跳过——本地零外部依赖。
- `src/ai/translation_glossary.py`：`build_glossary()` 合并全局 + 域包 `terminology.yaml`，产出 `terms`（偏好译法，注入 prompt）+ `protect`（不译保护词）+ `version`（内容 hash，变更即失效旧译记忆）。
- `TranslationService` 改造：translate 走 `EngineRouter`；**品牌词保护**（mask→翻译→restore，所有引擎统一生效）；结果带获胜引擎名（落 `translation_memory.engine` 列 + 前端徽标）。全程**向后兼容**（仅 ai_client 时退化为原行为）。
- `main.py` 预置：按 config 构造引擎链 + 术语库；启动日志打印 `引擎=deepl→ai, 术语=N, 保护词=M`。
- 前端：双向翻译条引擎徽标按响应 `provider` 实时显示（AI引擎/DeepL/Google）。

**超越云译之处：**
- **品牌词不译保护**：云译只翻不保护，本平台 `glossary.protect` 让品牌/产品名逐字保留（mask/restore，引擎无关）。
- **故障转移 + 引擎归因**：主引擎挂自动降级到兜底，并记录每条译文出自哪个引擎（云译切引擎是手动、无降级、无归因）。
- **术语版本化记忆**：术语库改版 → cache_key 变 → 自动失效旧译，无需手清缓存。

### P57（已完成 2026-06-07）— 非 AI 引擎术语强制 + 引擎用量可观测【收口超越】

**已交付：**
- **术语对所有引擎强制**（`apply_glossary_mask`）：放弃 P56 原计划的 DeepL glossary API（需预建词表资源、按语对受限、无 key 不可测），改用**统一占位符方案**——`terms`（源词→偏好译法）在发往引擎前 mask 成 `〔N〕`，引擎翻译完再 restore 成目标译法；`protect` 同机制 restore 成原词。**结果对 AI/DeepL/Google 一视同仁、确定性强制、零外部依赖、可测**。比 prompt 软提示（LLM 可能忽略）更可靠。
- **引擎用量观测**（`src/ai/translation_engine_stats.py`，单例，对齐 `llm_cost` 风格）：每引擎累计 调用/成功/失败/平均延迟 + 全局降级次数；进 `/api/workspace/metrics` JSON 与 `/api/messenger-rpa/metrics` Prometheus。降级语义**精确化**——「不可用引擎被跳过」不计降级，仅「已尝试的可用引擎失败后转兜底」才 +1，避免主引擎缺 key 时计数虚高。
- 后台「🌐 翻译引擎健康」面板（坐席绩效看板，主管可见）：实时显示各引擎调用量/成功率/延迟与累计降级数。

**超越云译之处（续）：**
- **引擎无关的术语强制**：偏好译法不再依赖 LLM「听话」，DeepL/Google 也被强制——这是云译切引擎做不到的一致性。
- **引擎健康可观测**：主管可见每引擎成功率/延迟/降级，及时发现某引擎劣化并调整 order。

### P58-1（已完成 2026-06-07）— 图片 OCR→翻译【追平多模态·第一步】

**已交付：**
- `src/ai/provider_stats.py`：把 P57「引擎 stats + 降级」**抽象为通用 `ProviderStats`**（按 namespace 注册单例），OCR/ASR 等任何「多后端+故障转移」provider 复用同一套 调用/成功/失败/延迟/降级 计数 + Prometheus。
- `src/ai/image_translate.py`：`ImageTranslateService` 复用现有 `VisionClient`（Ollama→智谱故障转移）做**逐字 OCR**（OCR_PROMPT 强约束只提字不翻译/不描述），再过 `TranslationService`（自动带术语强制 + 品牌词保护 + 引擎归因）。`ocr_fn` 可注入 → 单测无需真 VLM；`decode_image_to_temp` 校验 mime/大小（8MB）落临时文件用完即删。
- 路由 `POST /api/unified-inbox/translate-image`：vision 未启用 / 无后端 / 图片非法均返回**明确 reason+message**，临时文件 `finally` 清理。
- 前端：双向翻译条「🖼 图片翻译」按钮 → 选图 → 弹「原文(OCR)+译文」面板，一键「填入输入框/复制译文」。
- OCR 用量进 `/api/workspace/metrics`（`providers.ocr`）+ `/api/messenger-rpa/metrics` Prometheus。

**超越云译之处（续）：**
- **OCR 译文同享术语强制 + 品牌保护**：图片里的产品名/品牌也按词库统一译法、逐字保留——云译图片翻译无术语库联动。
- **多后端故障转移 + 用量可观测**：OCR 走 Ollama→智谱兜底且记录降级，主管可见 OCR 成功率/延迟。

### P58-2（已完成 2026-06-07）— 语音翻译 + 多模态缓存与健康面板【多模态收口】

**已交付：**
- `src/ai/voice_translate.py`：`VoiceTranslateService` 复用既有 `AudioPipeline`（faster-whisper/在线 ASR，自带 circuit breaker + 在线兜底）转写，再过 `TranslationService`。`transcribe_fn` 可注入 → 单测无需真模型；`decode_audio_to_temp` 校验 11 种音频 mime + 25MB 上限。**ASR 直接复用 P58-1 抽象的 `provider_stats("asr")`**——印证了上阶段「通用 provider 观测」抽象的价值（零新观测代码即得用量/降级）。
- 路由 `POST /api/unified-inbox/translate-voice`：未启用/无后端/转写失败/无语音都返回明确 reason+message，临时文件 `finally` 必删。
- **`src/ai/media_text_cache.py`（兑现承诺的「再次优化」）**：OCR/ASR 结果按**媒体内容 sha1** 缓存（有界 LRU + TTL），同图/同语音重复识别直接命中、跳过昂贵的 VLM/Whisper 调用。OCR 与 ASR 服务均已接入（`ocr_cached`/`asr_cached` 标志回前端）。
- 前端：双向翻译条新增「🎙 语音翻译」，转写+译文面板，一键填入/复制。
- **多模态健康面板**：坐席绩效看板「翻译引擎健康」下方新增 OCR/ASR 后端用量表（调用/成功率/延迟），与翻译引擎面板共用一次 `/api/workspace/metrics` 拉取。

**超越云译之处（续）：**
- **图/文/音三模态译文同享术语强制 + 品牌保护 + 引擎归因**：云译多模态翻译与术语库不联动。
- **多模态识别用量可观测 + 内容级缓存**：OCR/ASR 成功率、延迟、降级主管可见，重复媒体零成本复用。

### P59（已完成 2026-06-07）— 术语库管理控制台【激活差异化资产】

**为何改做这个（方案再思考）**：原计划「会话内媒体免上传翻译」，开工勘察发现 `message_obj` 不携带 `media_type/media_ref`、纯媒体消息（无文本）被过滤、各平台落盘路径不统一、且部分媒体栈在 `PROJECT_SCOPE` 之外——做「通用版」既脆弱又跨仓库高风险。于是**主动改做更高杠杆、全仓内、可测**的术语库控制台：P56-P58 已把「术语强制」做到跨引擎+跨模态，但**术语只能改配置文件**，非工程无法维护——控制台正是激活这一差异化资产的关键。

**已交付：**
- `src/ai/glossary_store.py`：可编辑覆盖层（`config/glossary_overrides.yaml`，原子写 + `.bak` 备份），优先级最高。
- `build_glossary` 增 `overrides` 层（域包 < 全局 < 覆盖层）；`TranslationService.update_glossary()` **运行时热替换**——version 变 → cache_key 变 → 旧译自动失效，主动清 L1。
- API `GET/POST /api/workspace/glossary`（主管专属）：增删改术语/保护词，落盘→重建→热更新到 live 服务，返回合并视图（标注「控制台/基线」来源 + 可编辑性）。
- `main.py` 启动加载覆盖层 + 存重建上下文到 `app.state`，供热更新复用。
- UI：坐席绩效看板「🔤 术语库管理」面板——增删术语（原词→译法）+ 品牌保护词，即时生效；基线条目（配置/域包）只读、控制台条目可删。

**超越云译之处（续）：**
- **改一次，文/图/音 + AI/DeepL/Google 全部即时统一**：云译术语库不联动多引擎/多模态，且无「不译保护」概念。
- **来源可追溯 + 安全编辑**：区分配置基线与控制台覆盖，备份可回滚，热更新零重启。

### P60（已完成 2026-06-07）— 术语命中统计 + CSV 导入/导出【术语库运营化】

**已交付：**
- `src/ai/glossary_hits.py`：进程级命中计数器，`translate()` 在术语/保护词**实际被触发**时计数（仅命中才记，热路径零额外开销）。量化「哪些术语真在用、哪些是僵尸条目」。
- 控制台 GET 视图每条术语/保护词带 `hits` + 汇总命中数；UI 新增「命中」列（>0 高亮）。
- CSV 导入/导出**复用现有 2 个路由**（GET `?format=csv` 导出，POST `op=import_csv` 导入），零路由清单膨胀。导出合并视图（基线+控制台，可 Excel 编辑后回灌），导入写覆盖层并一次性重建热更新。
- UI：术语库面板加「导出 CSV / 导入 CSV」按钮。

**超越云译之处（续）：**
- **术语价值可度量**：命中统计让主管知道术语库 ROI，定期清理僵尸术语——云译术语库是"黑盒只增不减"。
- **批量运营**：CSV 让百级术语可离线维护、版本化、团队协作。

**遗留（转 P61）**：会话内媒体免上传翻译（需先补 `media_ref` 贯通）。

### P61-1（已完成 2026-06-07）— 媒体字段端到端贯通 + 契约安全网【零行为变化】

把"会话内媒体免上传翻译"拆成两步，先交付**零行为变化的基础设施 + 安全网**（按"先补字段贯通再动管线"的风险控制）：

**已交付：**
- `src/inbox/normalizer.py::extract_media()`：跨平台 source（`image_path/voice_path/media_ref/media_url/...` 字段名各异）统一映射为 `(media_type, media_ref)`，best-effort、不抛异常；`message_obj()` 新增 `media_type/media_ref` **加法字段**（显式参数优先，否则从 source 抽取）——纯文本消息显示与既有行为完全不变。
- `src/inbox/ingest.py::_msg_from_obj()`：把媒体字段贯通到 `InboxMessage`→store（store 早有 `media_type/media_ref` 列，此前被丢弃）。
- `src/inbox/media_resolver.py`（新）：`resolve_media_path()` / `media_kind()` / `resolve_for_translate()` 统一解析层——把消息 `media_ref`（绝对路径 / `file://` / 相对路径+base_dirs / 远程 URL）解析为**本进程可直接打开的本地文件路径**，纯函数、不下载远程、解析不到返回 `None` + 明确 reason（`no_ref/remote_unsupported/not_found/unsupported_kind`），供 4 个 runner 共用、避免各写一遍。
- `tests/test_media_passthrough.py`（新，16 例）：锁定 source→obj→ingest→store→读回全程不丢，且纯文本零回归。
- 全量回归 4095 passed / 31 skipped，**零行为变化已验证**。

**为何先做这一步**：P59 评估发现媒体路径深、各平台落盘不一、UI 无媒体气泡——直接改高风险。本步只加字段+解析层+测试，不改任何渲染/过滤逻辑，把高风险大改拆成可独立审查的小步，为 P61-2 铺安全网。

### P61-2（已完成 2026-06-07）— 会话内媒体一键翻译（可解析则免上传）【超越上传式翻译】

在 P61-1 安全网之上接通用户可见能力：

**已交付：**
- `/api/unified-inbox/translate-message-media`（新）：按 `conversation_id+message_id` 从 store 取**受信** `media_ref`（回落 body），经 `resolve_for_translate(base_dirs=config.media.base_dirs)` 解析；可解析 → 直接复用 `ImageTranslateService`/`VoiceTranslateService`（两服务本就吃 path，**免上传、免 base64 往返**）；不可解析 → 回 `reason`+`fallback:upload`+中文提示，前端回落到 P58 上传组件。
- **路径穿越防护**：`config.media.base_dirs` 白名单 + `_within_base_dirs()` realpath 容纳检查；受信优先（store ref 覆盖前端伪造 ref）。
- 前端会话气泡：图片/语音入站消息加「识别翻译/转写翻译」按钮，结果原文+译文就地展示，命中缓存标「缓存」徽标，不可解析时黄字引导上传。
- 复用既有 vision/audio 配置门禁（`vision.enabled`/`audio_pipeline.enabled` + `has_any_vision_backend`），自动继承术语强制 + 品牌保护 + `MediaTextCache` 命中复用。
- 测试：`tests/test_media_translate_route.py`（8 例，覆盖 no_ref/remote/not_found/outside_base_dirs/vision_disabled/asr_disabled/store 受信优先）+ 路由清单。全量回归 4103 passed / 31 skipped。

**超越云译之处：**
- **会话内一键、免离开界面**：云译多模态翻译需上传文件；本方案对已落盘媒体直接识别翻译。
- **安全可控**：白名单目录 + 受信 ref + 解析失败语义化回退，既防穿越又给坐席明确下一步。

### P61-3（已完成 2026-06-07）— 合规分组批量触达 dry-run 规划层【再激活 vs 无脑群发】

按「先安全网后执行」纪律，先交付**纯 dry-run 规划层 + 预览**（只读不发），把"今天实际能触达谁"算清楚再谈发送：

**已交付：**
- `src/inbox/outreach_planner.py`（新）：`OutreachPlanner` 纯规划层——按 `OutreachFilters`（平台/标签任一命中/关系阶段/沉默天数区间/排除归档）从统一收件箱圈选会话，叠加 **账号级日配额（只读 `AccountLimiter.remaining_for` + 内存模拟扣减）+ cooldown** 产出 `OutreachPlan`：命中数、可触达名单、跳过原因（cooldown/account_cap）、每账号分布、预计耗时。**只读不发、不计配额、不写日志**。
- `src/inbox/store.py`：新增 `outreach_log` 表 + `record_outreach/last_outreach_ts/last_outreach_ts_bulk/outreach_batch_stats`（cooldown 判定 + 后续回执统计基座；migration 集中在 store 列表）。
- `/api/unified-inbox/outreach/preview`（新）：配置驱动（`config.outreach.cooldown_days/per_send_seconds/default_account_cap`）的 dry-run 预览端点。
- 主管面板（`agent_perf.html`）新增「分组批量触达预览」：筛选器 + 命中/可触达/跳过 chips + 每账号配额占用 + 预计分钟数 + 名单表。
- 测试：`tests/test_outreach_planner.py`（15 例，覆盖各筛选维度 + cooldown + 配额分布 + 默认上限 + outreach_log 存储）+ 路由清单。全量回归 4114 passed / 31 skipped。

**超越云译之处：**
- **合规优先**：定位「再激活」非「群发」——账号日配额 + cooldown + 沉默天数上限（排除已流失），从机制上避免云译式无脑群发封号。
- **发送前透明**：dry-run 预览让主管先看命中人数/配额占用/预计耗时/跳过原因，再决定是否执行——而非黑盒一键群发。

### P61-4（已完成 2026-06-08）— 触达执行 + 回执闭环【合规真发送】

在 dry-run 之上接通真实发送，多重安全闸：

**已交付：**
- `src/inbox/outreach_executor.py`（新）：`OutreachExecutor` 消费 `OutreachPlan.eligible` → `render_template`（占位符 `{name}/{silent_days}/{platform}`，空昵称回落「朋友」）→ `AccountLimiter.check_and_reserve`**真实扣减** → 注入式 `send_fn` 投递 → `record_outreach` 落回执。**只对真实发送尝试（sent/failed）写 log**（配额拒绝/空模板不写，保持 cooldown 语义干净）；配额拒绝不退还（防风控聚合）；逐条 try 不让单条异常中断整批；pacing 可配。
- `/api/unified-inbox/outreach/execute`（新）：**feature-flag `config.outreach.enabled`（默认 false）+ `confirm=true` 二次确认 + 服务端按 filters 重建 plan（不信任客户端名单）+ `max_batch` 硬上限**四重闸；`send_fn` 包 `send_via_adapters` 复用统一发送链路。
- `/api/unified-inbox/outreach/batch`（新）：批次回执统计（成功/失败计数）。
- 主管面板：预览面板加消息模板 + 本批上限 + 「执行发送」按钮（浏览器二次确认弹窗）+ 回执 chips。
- 测试：`tests/test_outreach_executor.py`（15 例：render_template / happy / 失败记 failed / 配额拒绝不写 log / max_send / flag 关 / confirm 缺 / 空模板 / 真发送 + batch 统计 / 硬上限）+ 路由清单。全量回归 4125 passed / 31 skipped。

**超越云译之处：**
- **四重安全闸 + 透明回执**：feature-flag/二次确认/服务端重建/硬上限，配合 dry-run 预览，从机制上区别于云译"一键无脑群发"；批次成功/失败/跳过实时可见。
- **cooldown 语义干净**：仅真实触达写 log，预览/配额拒绝零副作用。

### P61-5（已完成 2026-06-08）— 触达效果回流（回复率统计）【ROI 闭环】

把"发送→回执"延伸到"效果"，让再激活有可度量 ROI：

**已交付：**
- `src/inbox/store.py::outreach_response_stats()`：对某批次每条 `status='sent'` 触达，用相关子查询（走 `idx_msg_conv_ts`）找其会话在触达 ts **之后**、且在 `response_window_days` 窗口内的首个**入站**消息 → 算 sent/responded/response_rate/avg_response_minutes。纯查询无副作用，回复异步累积故可随时回看。
- `/api/unified-inbox/outreach/batch` 扩展：附带 `response`（回复率 + 平均回复时延），窗口可配（`config.outreach.response_window_days`，默认 7）/可传参。
- 主管面板：批次回执查询行（批次 ID + 回复窗口 → 成功/失败/回复率/均回复分钟）；执行后自动回填批次 ID 便于追踪。
- 测试：`tests/test_outreach_response.py`（6 例：触达后回复计入 / 触达前不算 / 超窗排除 / 不限窗计入 / 失败不计分母 / 出站不算 / 端点集成）。全量回归 4131 passed / 31 skipped。

**超越云译之处：**
- **效果可度量**：再激活批次的真实回复率 + 回复时延——云译群发是"发完即结束"的黑盒；本方案让主管知道哪种话术/分群真正有效。

### P61-6（规划）— 个性化开场 + 远程媒体补口 + 自动调参

- 模板 AI 个性化开场（基于最近互动/画像生成差异化首句），可选开关。
- media_ref 远程拉取（带平台凭证）→ 补齐 P61-2 的 `remote_unsupported` 缺口。
- 用 P61-5 回复率自动反哺 cooldown / 配额策略（低回复分群自动延长 cooldown）。

---

## 5. 验收（P55）

- [ ] 入站消息开启自动翻译后，气泡下出现译文，命中翻译记忆不重复调用。
- [ ] 发送前翻译开启时，弹译文预览，确认后投递的是译文。
- [ ] 切换会话语言设置被记住，刷新/重进会话保留。
- [ ] 顶部今日计数条显示且随 loadChats 刷新。
- [ ] 全量回归 `pytest tests/ -n auto -q` 全绿。

---

*对应 2026-06-07 代码版本与竞品快照。云译功能/价格以 [yunyiai.app](https://yunyiai.app/) 为准。*
