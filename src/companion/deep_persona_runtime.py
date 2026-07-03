"""深度人设运行期依赖注入（E1/E2 点火用）。

问题：巩固跑在 ingest（无 ai_client），语义召回/LLM 精修需要 embedder/LLM；全仓库无
ai_client 全局单例。方案：一个**轻量运行期注册表**——由持有配置的一方（skill_manager/
主程序初始化时）用 config 定型同步 embedder + 可选 llm，ingest 巩固与回复路按需取用。

设计：
  - **同步 embedder**（避开 async/sync 边界）：用 OpenAI 兼容同步客户端打 config
    ``ai.embedding_*`` 端点。best-effort：任何异常/未配置 → None（调用方回落字面/跳过）。
  - **短超时 + 缓存**：回复路只 embed 当前消息一次；带 LRU 小缓存避免同句重复 embed。
  - 默认不启用（``companion.deep_persona.semantic_recall`` / ``llm_refine`` 为 flag）。
"""
from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()
_EMBEDDER: Optional[Callable[[str], Optional[List[float]]]] = None
_LLM: Optional[Callable[[str], str]] = None
_CFG: Dict[str, Any] = {}
_EMB_CACHE: "OrderedDict[str, List[float]]" = OrderedDict()
_EMB_CACHE_MAX = 256
# G1 观测：证明语义召回"值不值"——命中率(缓存)/失败率/延迟。
_EMB_STATS: Dict[str, float] = {
    "calls": 0, "cache_hits": 0, "failures": 0, "latency_ms_total": 0.0,
}


def embedder_stats() -> Dict[str, Any]:
    """embedder 用量观测：calls/cache_hits/failures/avg_latency_ms。best-effort。"""
    with _LOCK:
        d = dict(_EMB_STATS)
    calls = int(d.get("calls", 0) or 0)
    net = calls - int(d.get("cache_hits", 0) or 0) - int(d.get("failures", 0) or 0)
    d["avg_latency_ms"] = round(d["latency_ms_total"] / net, 1) if net > 0 else 0.0
    return d


_DP_FLAGS: Dict[str, Any] = {}


def configure_from_config(config: Dict[str, Any]) -> None:
    """从主配置定型同步 embedder（懒建客户端）+ 记录 deep_persona 子开关。"""
    global _CFG, _DP_FLAGS
    with _LOCK:
        _CFG = dict((config or {}).get("ai", {}) or {})
        _DP_FLAGS = dict(
            ((config or {}).get("companion", {}) or {}).get("deep_persona", {}) or {})


def semantic_recall_enabled() -> bool:
    return bool(_DP_FLAGS.get("enabled")) and bool(_DP_FLAGS.get("semantic_recall"))


def llm_refine_enabled() -> bool:
    return bool(_DP_FLAGS.get("enabled")) and bool(_DP_FLAGS.get("llm_refine"))


def self_memory_enabled() -> bool:
    return bool(_DP_FLAGS.get("enabled")) and bool(_DP_FLAGS.get("self_memory"))


def trend_log_enabled() -> bool:
    return bool(_DP_FLAGS.get("enabled")) and bool(_DP_FLAGS.get("trend_log"))


def _build_sync_embedder() -> Optional[Callable[[str], Optional[List[float]]]]:
    cfg = _CFG
    base = str(cfg.get("embedding_base_url") or cfg.get("base_url") or "").strip()
    model = str(cfg.get("embedding_model") or "").strip()
    key = str(cfg.get("embedding_api_key") or cfg.get("api_key") or "").strip()
    if not base or not model:
        return None
    try:
        from openai import OpenAI  # type: ignore
    except Exception:
        return None
    try:
        client = OpenAI(api_key=key or "sk-none", base_url=base, timeout=8.0)
    except Exception:
        return None

    import time as _time

    def _embed(text: str) -> Optional[List[float]]:
        t = str(text or "").strip()
        if not t:
            return None
        with _LOCK:
            _EMB_STATS["calls"] += 1
            if t in _EMB_CACHE:
                _EMB_CACHE.move_to_end(t)
                _EMB_STATS["cache_hits"] += 1
                return _EMB_CACHE[t]
        _t0 = _time.monotonic()
        try:
            r = client.embeddings.create(model=model, input=[t])
            vec = list(r.data[0].embedding)
        except Exception:
            with _LOCK:
                _EMB_STATS["failures"] += 1
            return None
        _ms = (_time.monotonic() - _t0) * 1000.0
        with _LOCK:
            _EMB_STATS["latency_ms_total"] += _ms
            _EMB_CACHE[t] = vec
            _EMB_CACHE.move_to_end(t)
            while len(_EMB_CACHE) > _EMB_CACHE_MAX:
                _EMB_CACHE.popitem(last=False)
        return vec

    return _embed


def get_embedder() -> Optional[Callable[[str], Optional[List[float]]]]:
    """取同步 embedder（懒建）。未配置/建失败 → None。"""
    global _EMBEDDER
    if _EMBEDDER is None:
        with _LOCK:
            if _EMBEDDER is None:
                _EMBEDDER = _build_sync_embedder()
    return _EMBEDDER


def set_embedder(fn: Optional[Callable[[str], Optional[List[float]]]]) -> None:
    """测试/自定义注入 embedder。"""
    global _EMBEDDER
    with _LOCK:
        _EMBEDDER = fn


def set_llm(fn: Optional[Callable[[str], str]]) -> None:
    """注入 LLM 精修函数 ``llm_fn(prompt)->str``（测试/自定义）。"""
    global _LLM
    with _LOCK:
        _LLM = fn


def _build_sync_llm() -> Optional[Callable[[str], str]]:
    """从 config 建同步 chat LLM（E2 画像精修用，off 热路；避开 async 边界）。"""
    cfg = _CFG
    base = str(cfg.get("base_url") or "").strip()
    model = str(cfg.get("model") or "").strip()
    key = str(cfg.get("api_key") or "").strip()
    if not base or not model:
        return None
    try:
        from openai import OpenAI  # type: ignore
        client = OpenAI(api_key=key or "sk-none", base_url=base, timeout=20.0)
    except Exception:
        return None

    def _llm(prompt: str) -> str:
        try:
            r = client.chat.completions.create(
                model=model, temperature=0.4, max_tokens=300,
                messages=[{"role": "user", "content": str(prompt or "")}],
            )
            return str(r.choices[0].message.content or "")
        except Exception:
            return ""

    return _llm


def get_llm() -> Optional[Callable[[str], str]]:
    """取同步 LLM（懒建）。未配置/建失败 → None。"""
    global _LLM
    if _LLM is None:
        with _LOCK:
            if _LLM is None:
                _LLM = _build_sync_llm()
    return _LLM


def reset() -> None:
    global _EMBEDDER, _LLM, _CFG, _DP_FLAGS
    with _LOCK:
        _EMBEDDER = None
        _LLM = None
        _CFG = {}
        _DP_FLAGS = {}
        _EMB_CACHE.clear()
        for k in ("calls", "cache_hits", "failures", "latency_ms_total"):
            _EMB_STATS[k] = 0


__all__ = [
    "configure_from_config", "get_embedder", "set_embedder",
    "set_llm", "get_llm", "reset",
    "semantic_recall_enabled", "llm_refine_enabled", "embedder_stats",
    "self_memory_enabled", "trend_log_enabled",
]
