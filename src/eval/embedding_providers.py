"""真实嵌入 provider —— 解锁记忆向量召回 + 语义去重的离线/CI 实跑。

背景：主对话 LLM（DeepSeek 等）**无 embedding 端点**，故 `ai_client.embed` 常返空，
记忆向量召回/语义去重门禁长期 skip。本模块提供**可插拔、零改生产默认**的真实嵌入源，
按优先级探测，任一可用即返回同步 ``EmbedFn``，均不可用 → None（门禁照旧优雅 skip）：

  1) **OpenAI 兼容 embedding 端点**（生产/自托管路）：
     env ``AITR_EMBED_BASE_URL`` / ``AITR_EMBED_MODEL`` / ``AITR_EMBED_API_KEY``，
     或 config ``ai.embedding_base_url`` / ``ai.embedding_model``
     （LM Studio / Ollama / OpenAI / 智谱 兼容；配了就用，并先探针一次确认可用）。
  2) **本地 sentence-transformers**（CI/离线路，零 key、模型缓存后离线）：
     默认多语 ``paraphrase-multilingual-MiniLM-L12-v2``。**opt-in**——需 env
     ``AITR_EMBED_LOCAL=1`` 才启用，避免默认 CI 背上 torch 冷加载（数十秒）。

设计同 faq/translation/memory 评测：核心评测与 provider 解耦，本模块只负责"找一个能用的
真实嵌入器"。探测失败一律静默返回 None，绝不抛。
"""

from __future__ import annotations

import importlib.util
import logging
import os
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("embedding_providers")

EmbedFn = Callable[[str], Optional[List[float]]]

_DEFAULT_ST_MODEL = "paraphrase-multilingual-MiniLM-L12-v2"
_ST_MODEL_CACHE: Dict[str, Any] = {}  # model_name -> SentenceTransformer（进程内单例，免重复冷加载）


def _truthy(v: Optional[str]) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


def _probe_timeout():
    """embedding 端点超时：连接快失败（不可达秒级 skip），读取给足（真实嵌入需算）。
    可用 env 覆写；httpx 不可用（理论上 openai 必带）则回落纯 float。"""
    connect = _env_float("AITR_EMBED_CONNECT_TIMEOUT", 3.0)
    read = _env_float("AITR_EMBED_READ_TIMEOUT", 30.0)
    try:
        import httpx
        return httpx.Timeout(read, connect=connect)
    except Exception:  # noqa: BLE001
        return connect


def _load_config_if_none(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if config is not None:
        return config
    try:
        import yaml
        with open("config/config.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _from_openai_compatible(config: Optional[Dict[str, Any]]) -> Optional[EmbedFn]:
    """env 或 config 配了 OpenAI 兼容 embedding 端点 → 探针通过则返回 embed_fn。"""
    base = (os.environ.get("AITR_EMBED_BASE_URL") or "").strip()
    model = (os.environ.get("AITR_EMBED_MODEL") or "").strip()
    key = (os.environ.get("AITR_EMBED_API_KEY") or "").strip()
    if not base:
        ai = (_load_config_if_none(config) or {}).get("ai", {}) or {}
        base = str(ai.get("embedding_base_url") or "").strip()
        model = model or str(ai.get("embedding_model") or "").strip()
        key = key or str(ai.get("embedding_api_key") or ai.get("api_key") or "").strip()
    if not base or not model or model.lower() in ("none", "off", "disabled"):
        return None
    if not base.rstrip("/").endswith("/v1"):
        base = base.rstrip("/") + "/v1"
    if key in ("", "YOUR_AI_API_KEY", "YOUR_API_KEY"):
        key = "noauth"  # 本地端点（Ollama/LM Studio）常不校验 key
    try:
        from openai import OpenAI
        # 短连接超时 + 不重试：配了但**不可达**的端点（防火墙丢包/端点没起）应在数秒内
        # 探针失败→优雅 skip，而非默认 max_retries=2 × 30s 叠加把全量回归拖到 pytest 超时。
        # read 仍给足（真实嵌入需算），连接失败才是要 fail-fast 的场景。
        client = OpenAI(
            api_key=key, base_url=base,
            timeout=_probe_timeout(), max_retries=0,
        )
        probe = client.embeddings.create(model=model, input=["测试嵌入可用性"])
        if not (probe and probe.data and probe.data[0].embedding):
            return None
    except Exception as e:  # noqa: BLE001
        logger.debug("openai-compat embedding 探针失败: %s", e)
        return None

    def _embed(text: str) -> Optional[List[float]]:
        try:
            r = client.embeddings.create(model=model, input=[text or ""])
            return list(r.data[0].embedding) if (r and r.data) else None
        except Exception:
            return None

    logger.info("[embedding] 使用 OpenAI 兼容端点 base=%s model=%s", base, model)
    return _embed


def _from_sentence_transformers() -> Optional[EmbedFn]:
    """本地 sentence-transformers（opt-in via AITR_EMBED_LOCAL=1）。"""
    if not _truthy(os.environ.get("AITR_EMBED_LOCAL")):
        return None
    if importlib.util.find_spec("sentence_transformers") is None:
        return None
    model_name = (os.environ.get("AITR_EMBED_ST_MODEL") or _DEFAULT_ST_MODEL).strip()
    try:
        model = _ST_MODEL_CACHE.get(model_name)
        if model is None:
            from sentence_transformers import SentenceTransformer
            model = SentenceTransformer(model_name)
            _ST_MODEL_CACHE[model_name] = model
    except Exception as e:  # noqa: BLE001
        logger.debug("sentence-transformers 加载失败 model=%s: %s", model_name, e)
        return None

    def _embed(text: str) -> Optional[List[float]]:
        try:
            vec = model.encode([text or ""])
            return [float(x) for x in vec[0]]
        except Exception:
            return None

    logger.info("[embedding] 使用本地 sentence-transformers model=%s", model_name)
    return _embed


def build_embed_fn(config: Optional[Dict[str, Any]] = None) -> Optional[EmbedFn]:
    """按优先级返回首个可用真实 embed_fn；均不可用 → None（门禁 skip）。"""
    for builder in (
        lambda: _from_openai_compatible(config),
        _from_sentence_transformers,
    ):
        try:
            fn = builder()
        except Exception:  # noqa: BLE001
            fn = None
        if fn is not None:
            return fn
    return None


def describe_availability(config: Optional[Dict[str, Any]] = None) -> str:
    """人读诊断：当前哪条 provider 可用（不缓存模型，仅探测）。"""
    oa = _from_openai_compatible(config) is not None
    local_optin = _truthy(os.environ.get("AITR_EMBED_LOCAL"))
    st_installed = importlib.util.find_spec("sentence_transformers") is not None
    return (
        "embedding providers: "
        f"openai_compat={'yes' if oa else 'no'}  "
        f"local_st(installed={'yes' if st_installed else 'no'},"
        f" optin={'yes' if local_optin else 'no'})"
    )


__all__ = ["EmbedFn", "build_embed_fn", "describe_availability"]
