"""记忆向量召回的「嵌入源就绪度」检查（陪护开起来的关键安全闸）。

背景（代码核验得出的真实坑）：``memory.vector.enabled=true`` 只是打开「查询时去嵌入用户消息」
的闸门，真正的向量来自 ``ai_client.embed()``，其后端由 ``ai.embedding_base_url`` /
``ai.embedding_model`` 决定。主对话 LLM（DeepSeek 等）通常**没有 embedding 端点**——此时
``embed()`` 返回 ``[]``，召回**静默退化为纯关键词、不报错**。于是「以为开了记忆深度，其实没开」。

本模块把这个隐性前置显性化：纯函数判断嵌入源是否**配置齐全**（零网络），并提供可选的
**在线探针**（真打一次 embed，确认后端可达且返回向量）。供能力看板自洽体检 + 开启前 preflight。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

_OFF = {"", "none", "off", "disabled", "false", "0"}


def _ai_cfg(config: Any) -> Dict[str, Any]:
    if isinstance(config, dict):
        ai = config.get("ai")
        if isinstance(ai, dict):
            return ai
    return {}


def embedding_source_configured(config: Any) -> bool:
    """嵌入源是否「配置齐全」（不打网络，只看 config）。

    认可两条真实可用路径（与 ``AIClient.embed`` 实现一致）：
      1) OpenAI 兼容端点：``ai.embedding_base_url`` 非空 + ``ai.embedding_model`` 非空可用
         （Ollama / LM Studio / OpenAI 云均走这条）；
      2) Gemini 原生：``ai.provider == 'gemini'`` + 有 ``embedding_model`` + 有 ``api_key``。
    """
    ai = _ai_cfg(config)
    model = str(ai.get("embedding_model") or "").strip().lower()
    if model in _OFF:
        return False
    base = str(ai.get("embedding_base_url") or "").strip()
    if base:
        return True
    provider = str(ai.get("provider") or "").strip().lower()
    if provider == "gemini":
        key = str(ai.get("embedding_api_key") or ai.get("api_key") or "").strip()
        return bool(key)
    return False


def check_embedding_readiness(config: Any) -> Dict[str, Any]:
    """组合「向量召回开关」与「嵌入源配置」给出就绪结论（纯函数，零网络）。

    返回 ``{vector_enabled, configured, ready, severity, reason, fix}``：
      - ready=True：开关开 + 源配齐（真能向量召回，仍建议跑一次在线探针确认可达）；
      - vector_enabled 但未配源 → severity=error（**静默退化关键词**，最坑），给出 fix；
      - 未开向量 → severity=ok（不参与，仅提示如何开）。
    """
    ai = _ai_cfg(config)
    vector_enabled = bool(
        (((config or {}).get("memory") or {}).get("vector") or {}).get("enabled", False)
        if isinstance(config, dict) else False
    )
    configured = embedding_source_configured(config)
    out: Dict[str, Any] = {
        "vector_enabled": vector_enabled,
        "configured": configured,
        "embedding_model": str(ai.get("embedding_model") or ""),
        "embedding_base_url": str(ai.get("embedding_base_url") or ""),
        "ready": False,
        "severity": "ok",
        "reason": "",
        "fix": "",
    }
    if vector_enabled and not configured:
        out["severity"] = "error"
        out["reason"] = ("记忆向量召回已开，但未配置嵌入源（ai.embedding_base_url/embedding_model 空）"
                         "→ embed() 返回空，召回静默退化为纯关键词（看似开了实则没开）")
        out["fix"] = ("配 ai.embedding_base_url + embedding_model（本地 Ollama nomic-embed-text 最省），"
                      "或关掉 memory.vector.enabled。见 docs/COMPANION_TURN_ON.md")
        return out
    if not vector_enabled and configured:
        out["severity"] = "ok"
        out["reason"] = "嵌入源已配，但 memory.vector.enabled=false → 召回仍走纯关键词"
        out["fix"] = "开 memory.vector.enabled:true 即启用向量融合召回（开前先 backfill 历史行）"
        return out
    if vector_enabled and configured:
        out["ready"] = True
        out["reason"] = "向量召回已开且嵌入源已配（建议跑一次在线探针确认后端可达）"
        return out
    out["reason"] = "未开向量召回（memory.vector.enabled=false）；记忆仅纯关键词召回"
    return out


async def probe_embedding(ai_client: Any, *, text: str = "ping") -> Dict[str, Any]:
    """在线探针：真打一次 ``ai_client.embed``，确认后端可达且返回非空向量。

    返回 ``{ok, dim, error}``。绝不抛（任何异常 → ok=False + error 文本）。
    供开启前 preflight / CLI 用；纯 config 检查用 ``check_embedding_readiness``。
    """
    if ai_client is None or not hasattr(ai_client, "embed"):
        return {"ok": False, "dim": 0, "error": "ai_client 不可用或无 embed 方法"}
    try:
        vec = await ai_client.embed(text)
        dim = len(vec) if vec else 0
        if dim <= 0:
            return {"ok": False, "dim": 0,
                    "error": "embed() 返回空向量（嵌入端点未配置/不可达/模型名错）"}
        return {"ok": True, "dim": dim, "error": ""}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "dim": 0, "error": f"{type(exc).__name__}: {exc}"}


__all__ = [
    "embedding_source_configured",
    "check_embedding_readiness",
    "probe_embedding",
]
