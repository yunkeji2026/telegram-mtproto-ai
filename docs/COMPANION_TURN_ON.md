# 陪护「真·开起来」Runbook（记忆深度 + 分阶段开启）

> 目标：把已建但默认 flag-off 的陪护栈**实际开启并验证**，重点先打通「长期记忆深度」
> （向量召回），再按风险逐档点亮全自动真发 / 主动触达。
>
> 配套：`config/companion_on.overlay.example.yaml`（拷进 `config/config.local.yaml`）。

## 0. 先理解一个关键坑（最隐蔽）

`memory.vector.enabled=true` **只是打开「查询时去嵌入用户消息」的闸门**。真正的向量来自
`ai_client.embed()`，其后端由 `ai.embedding_base_url` / `ai.embedding_model` 决定。

主对话 LLM（DeepSeek 等）**没有 embedding 端点** → `embed()` 返回空 → 召回**静默退化为
纯关键词、不报错**。于是「以为开了记忆深度，其实没开」。

所以第一步永远是：**先配一个真实可达的嵌入源。**

## 1. 配嵌入源（三选一）

| 方式 | base_url | model | 成本 |
|---|---|---|---|
| 本地 Ollama（推荐） | `http://127.0.0.1:11434` | `nomic-embed-text` | 免费/离线 |
| 本地 LM Studio | `http://127.0.0.1:1234` | `text-embedding-nomic-embed-text-v1.5` | 免费/离线 |
| OpenAI 云 | `https://api.openai.com` | `text-embedding-3-small` | 按量 |

Ollama：`ollama pull nomic-embed-text` 后填 `ai.embedding_*`（见 overlay）。

## 2. 开记忆深度（Tier-1，低风险）

合并 overlay 的 `memory.vector` + `memory.consolidation` + `ai.embedding_*` 段，重启。
启动 backfill 会给历史事实补向量（一次性）。

## 3. 验证（缺这步等于没开）

1. **离线量化收益**：`python -m scripts.run_eval --memory`
   —— 对比「向量融合 vs 纯关键词」的召回（改写/近义查询命中提升）。
2. **在线就绪体检**：开 `/admin/ops`，「陪伴配置体检」灯应为**绿**；
   或 `GET /api/companion/capabilities/advice` 的 `consistency` 里**不应**出现
   「记忆向量召回已开但未配嵌入源」——出现即嵌入源没生效，按 `fix` 修。
3. 嵌入端点连通性可用在线探针（`src/companion/embedding_readiness.probe_embedding`）确认
   `embed("ping")` 返回非空向量。

> 注意：向量召回作用于**回复路径**（`process_message` 注入情节记忆）。
> **主动开场/ritual 不走向量召回**（用确定性的 `select_proactive_topic`），属预期，不必排障。

## 4. 提示层安全栈（Tier-0，零风险，建议同开）

`companion.persona_guard / empathy_strategy / wellbeing` 全开——纯 prompt 注入，
关着才危险（人格崩 / 漏接危机）。

## 5. 再往上：分阶段开启（按风险逐档）

到 `/api/companion/capabilities` 看能力阶梯，按 tier 顺序点亮，每档开前用其校准端点核对：

- **Tier-2 全自动文本真发**：`l2_autosend.enabled` → `deliver`（真发到客户）+ `companion_send_gate`
  （出站安全闸，随真发同开）+ `l2_autosend.translate`（外语客户译后再发）。
  开闸前看 `/api/companion/capabilities/delivery-calibration`。
- **Tier-3 主动触达**：`companion.proactive_topic / proactive_care`（先 `dry_run` 校准，
  看 `/api/companion/proactive/preview`）。
- **Tier-4 全自动语音**：`voice_autosend`（需文本真发主开关已开）。

每一档都有「自洽体检」兜底：开关互相矛盾 / 前置缺失会在 advice 端点 + ops 总览灯报出来。
