# 实现设计 — Phase C：意图分析 LLM 升级 + 翻译记忆持久化（可执行版）

更新日期：2026-05-31
对应蓝图：[`AI跨境电商客服平台_升级开发文档_v2_落地优化版.md`](AI跨境电商客服平台_升级开发文档_v2_落地优化版.md) §3 Phase C
依赖：[`实现设计_PhaseA_统一数据地基.md`](实现设计_PhaseA_统一数据地基.md)（`message_analysis` 表已在 A 建）
状态：**可直接落地的实现设计**，对照真实代码

---

## 0. 现状与目标

两个相对独立的子任务，共用「规则/缓存兜底 + LLM/持久化增强」的同一手法：

- **C1 意图分析**：`src/ai/chat_assistant_service.py::ChatAssistantService` 现在是**纯规则**（`_detect_intent`/`_detect_emotion`/`_detect_risk` 等，见 90–207 行），已预留 `ai_client` 入口但未用。返回 `ChatAnalysis`（shape 稳定）。
- **C2 翻译**：`src/ai/translation_service.py::TranslationService` 只有**进程内 TTL 缓存**（`self._cache` dict，`_cache_get/_cache_put`），重启即失、无术语库、无成本统计、单一 AI 引擎。

目标（**返回 shape / 接口签名不变**，纯增强）：
1. C1：规则版之上叠 LLM 评分通道，LLM 故障自动回落规则，结果落 `message_analysis`。
2. C2：持久化 `translation_memory` 替换进程缓存 + 术语库 + 多引擎可插拔 + 成本统计接 `llm_cost.py`。

> 关键不变量：`ChatAssistantService.analyze()` 仍返回 `ChatAnalysis`，`TranslationService.translate()` 仍返回 `TranslationResult`。调用方（`unified_inbox_routes.py:521-532` analyze、`505-519` translate）零改动。

---

## 1. 文件清单

### 新建
```
src/ai/
  intent_llm.py          # LLM 意图评分（结构化 JSON），analyze 的可选增强
  translation_memory.py  # TranslationMemoryStore（SQLite 持久层）
  translation_glossary.py# 术语库加载/应用（域包级 + 全局）
  translation_engines.py # 引擎抽象 + ai/google/deepl 适配（先只实现 ai）
tests/
  test_intent_llm.py
  test_translation_memory.py
  test_translation_glossary.py
  test_chat_assistant_llm_fallback.py
```

### 修改
| 文件 | 改动 |
|---|---|
| `src/ai/chat_assistant_service.py` | `analyze()` 增加 `use_llm` 路径：规则先算 baseline → LLM 增强 → 合并 → 失败回落；新增 `analysis_store` 可选注入落 `message_analysis` |
| `src/ai/translation_service.py` | `__init__` 接 `memory_store` + `glossary` + `engine`；`translate()` 缓存查/写改走持久层（保留内存层做 L1）；成本上报 |
| `src/web/routes/unified_inbox_routes.py` | `_get_chat_assistant_service` / `_get_translation_service` 注入新依赖（store/glossary/engine） |
| `main.py` | 构造 `TranslationMemoryStore` + glossary，注入两个 service |
| `config/config.example.yaml` | `intent_analysis`（use_llm/timeout）+ `translation`（engine/memory/glossary/cost）段 |

---

## 2. C1：意图分析 LLM 升级

### 2.1 合并策略（规则做兜底，LLM 做提升）

```python
async def analyze(self, *, text, messages=None, chat=None, use_llm=None) -> ChatAnalysis:
    base = self._rule_analyze(text, messages, chat)     # 现有逻辑原样保留
    if (use_llm if use_llm is not None else self._use_llm) and self.ai_client:
        try:
            llm = await self._llm_score(text, messages, chat, timeout=self._timeout)
            base = _merge_analysis(base, llm)            # LLM 覆盖 intent/emotion/summary/order_no
        except Exception:
            pass                                          # 静默回落规则版
    if self._analysis_store and chat:
        self._analysis_store.save_analysis(_to_message_analysis(base, chat))
    return base
```

合并规则 `_merge_analysis`（**风险只升不降**，安全优先）：
- `intent` / `emotion` / `summary` / `order_no` / `relationship_stage`：LLM 优先（若非空）。
- `risk_level`：取**两者更高**者（`max(rule, llm)` 按 low<medium<high）——LLM 说低但规则命中 money/privacy 仍判 high（规则 `_detect_risk` 的 `high_terms` 是硬底线）。
- `suggestions`：LLM 有则用 LLM 的，否则保留规则版三档。

### 2.2 LLM 评分（intent_llm.py）

调 `AIClient.chat(prompt, strategy_overrides)`（真实签名 `ai_client.py:2321`，返回 `Optional[str]`），prompt 要求**只输出 JSON**：

```python
SCHEMA = {"intent","emotion","risk_level","risk_reasons","summary","order_no","confidence"}
async def llm_score(ai_client, text, messages, chat, *, timeout=8.0) -> dict:
    prompt = _build_prompt(text, messages, chat)   # 含最近 N 条上下文 + 电商意图字典
    raw = await asyncio.wait_for(
        ai_client.chat(prompt, {"_skip_lang_guard": True, "_json": True}), timeout)
    return _parse_json_lenient(raw)                # 容错解析（去 ```json 包裹、取首个 {...}）
```

意图字典与电商域对齐（v1 §8.3：商品咨询/价格/库存/物流/退货退款/投诉/催单/复购/无效/人工），从域包 manifest 读，核心不硬编码（沿用 Phase 0A hook 范式）。

### 2.3 落库

`message_analysis`（Phase A 建表）字段已对齐 `ChatAnalysis.to_dict()`：`analyzer` 列记 `rule`/`llm`，供后续准确率对账与 SLA。

### 2.4 验收
- LLM 正常时意图识别样本集 ≥85%；LLM 超时/异常/返回非 JSON 时**自动回落规则版，不抛错**。
- 规则硬底线风险（money/privacy/self_harm/adult/stop_contact）LLM **不能调低**。
- 每次 analyze 落一行 `message_analysis`。

---

## 3. C2：翻译记忆持久化 + 术语库 + 多引擎

### 3.1 translation_memory 表（translation_memory.py）

复刻 `ContactStore` SQLite 范式（WAL + Lock + 幂等迁移）。可独立 DB（`data/translation_memory.db`）或并入 inbox.db：

```sql
CREATE TABLE IF NOT EXISTS translation_memory (
    cache_key      TEXT PRIMARY KEY,         -- sha256(source_lang|target_lang|style|glossary_ver|text[:2000])
    source_text    TEXT NOT NULL,
    translated_text TEXT NOT NULL,
    source_lang    TEXT NOT NULL,
    target_lang    TEXT NOT NULL,
    style          TEXT NOT NULL DEFAULT 'chat',
    engine         TEXT NOT NULL DEFAULT 'ai',
    glossary_ver   TEXT NOT NULL DEFAULT '',  -- 术语库版本变了 → key 变 → 不命中旧译
    hit_count      INTEGER NOT NULL DEFAULT 0,
    created_at     REAL NOT NULL,
    last_hit_at    REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tm_lang ON translation_memory(source_lang, target_lang);
CREATE INDEX IF NOT EXISTS idx_tm_hits ON translation_memory(hit_count DESC);
```

`cache_key` 沿用现有 `TranslationService._cache_key`（`translation_service.py:163`）算法**再加 `glossary_ver`**——术语库更新即自动失效旧译，无需手动清缓存。

### 3.2 TranslationService 改造（保持签名）

```python
def __init__(self, *, ai_client=None, memory_store=None, glossary=None,
             engine=None, default_target_lang="zh", cache_ttl_sec=86400, ...):
    ...  # 现有参数全保留，新增三个可选注入
```

`translate()` 查缓存顺序：**内存 L1（现有 dict）→ memory_store L2（持久）→ engine 实译**。实译前用 glossary 改写 prompt（注入术语对照），实译后写 L1+L2 并 `hit_count++`。`memory_store=None` 时退化为今天的纯内存行为（向后兼容，测试无需 DB）。

成本：实译走 AI 引擎时调 `src/ai/llm_cost.py` 记 token/费用，供后台展示（v1 技术原则 4「翻译要产品化」）。

### 3.3 术语库（translation_glossary.py）

- 全局术语 + 域包术语合并（payment 已有 `domains/payment/prompts/terminology.yaml`，电商专有词：尺码/颜色/物流/退款/材质/保修）。
- `glossary.version()` 返回内容 hash 当 `glossary_ver`。
- `apply(prompt, src, tgt)`：把命中术语作为 "must translate X as Y" 提示拼进现有 `_build_prompt`（`translation_service.py:149`），不改 prompt 主体结构。

### 3.4 多引擎（translation_engines.py）

```python
class TranslationEngine(Protocol):
    name: str
    async def translate(self, text, *, source_lang, target_lang, style) -> str: ...
```
先实现 `AiEngine`（包现有 `ai_client.chat` 路径），`google`/`deepl` 留接口骨架（v1 列为可插拔，非本期必做）。`config.translation.engine` 选默认，故障可回落 AI。

### 3.5 验收
- 重复句子跨**重启**仍命中（持久层生效）；命中 `hit_count` 递增。
- 术语库改版后旧译自动失效、按新术语重译。
- 翻译成本可在后台看到（接 llm_cost）。
- `memory_store=None` 时行为与今天一致（向后兼容，`test_translation_service.py` 不回归）。

---

## 4. main.py 接入点

Phase A 的 `inbox_store` 附近：

```python
from src.ai.translation_memory import TranslationMemoryStore
from src.ai.translation_glossary import build_glossary
from src.ai.translation_service import TranslationService
from src.ai.chat_assistant_service import ChatAssistantService

tm_store = TranslationMemoryStore(self._project_root / "data/translation_memory.db")
glossary = build_glossary(self.config, active_domain=self.domain_name)
web_app.state.translation_service = TranslationService(
    ai_client=self.ai_client, memory_store=tm_store, glossary=glossary)
web_app.state.chat_assistant_service = ChatAssistantService(
    ai_client=self.ai_client,
    use_llm=self.config.get("intent_analysis", {}).get("use_llm", False),
    analysis_store=getattr(self, "inbox_store", None))
```

> 注意：`unified_inbox_routes.py` 的 `_get_translation_service`/`_get_chat_assistant_service`（51–68 行）已有「state 有就用、没有就 new」逻辑——main.py 预置后路由直接复用预置实例（带 store/glossary），无 store 的测试路径仍 new 出裸实例。

---

## 5. 配置

```yaml
intent_analysis:
  use_llm: false          # 默认关，先用规则版；灰度开 LLM
  timeout_sec: 8
  context_messages: 6

translation:
  engine: "ai"            # ai / google / deepl（后两者留接口）
  memory:
    enabled: true
    db_path: "data/translation_memory.db"
  glossary:
    enabled: true
    extra_terms: {}       # 全局术语，域包术语自动合并
  cost_tracking: true
```

---

## 6. 测试计划

| 文件 | 覆盖 |
|---|---|
| `test_intent_llm.py` | JSON 容错解析（带 ```json 包裹/前后噪声）；超时/异常返回兜底；意图字典从域包读 |
| `test_chat_assistant_llm_fallback.py` | LLM 故障回落规则版不抛；风险只升不降（LLM 说 low 但规则命中 money → high）；落 `message_analysis` |
| `test_translation_memory.py` | 持久命中（new 一个 store 模拟重启仍命中）；hit_count 递增；glossary_ver 变更失效旧译 |
| `test_translation_glossary.py` | 术语合并（全局+域包）；version hash 稳定；apply 注入命中术语 |

向后兼容：`memory_store=None` / `use_llm=False` 时，现有 `test_translation_service.py` / `test_chat_assistant_service.py` **必须零回归**。回归：`python -m pytest tests/ -n auto -q` 全绿。

---

## 7. 风险与回滚

| 风险 | 缓解 |
|---|---|
| LLM 拖慢 analyze | `asyncio.wait_for(timeout)` + 默认 `use_llm=False`，灰度开；超时回落规则 |
| LLM 调低风险造成误自动发 | `_merge_analysis` 风险只升不降，规则 high_terms 是硬底线 |
| 持久缓存返回过期错译 | `glossary_ver` 入 key；可加 `cache_ttl` 二次校验；后台可清单条 |
| 多引擎接口未完成阻塞 | 只实现 `AiEngine` 即可上线，google/deepl 骨架不影响默认路径 |
| 改动影响现有 inbox 翻译/分析 | 两个 service 签名不变 + 新依赖全可选；不注入 store 即退化为今天行为 |

---

## 8. 与前后 Phase 的接口

- **依赖 Phase A**：`message_analysis` 表（C1 落库）。
- **供 Phase B**：`ChatAnalysis.risk_level`/`risk_reasons` 驱动 B 的 L0–L4 风险分层；`TranslationService` 把中文草稿译回客户语言填 `reply_drafts.translated_preview`。
- **供 Phase D**：`message_analysis.order_no`（C1 LLM 抽取）触发电商工具按订单号查询；`intent` 路由到对应工具。
- **供 Phase E**：`translation_memory.hit_count` + llm_cost 是套餐计量（字符/token）的数据源。

---

*本设计与 2026-05-31 代码版本对应。实现前 `grep` 复核 `ChatAssistantService.analyze` / `TranslationService.translate` / `AIClient.chat` 签名，以代码实况为准。*
