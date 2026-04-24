"""P6-4：LLM 成本 & token 观测。

单例进程级计数器，按 ``(model, tier, account_id)`` 聚合 tokens 与估算成本。
无新增依赖；Prometheus 文本由 Web 路由读 ``dump_prom()`` 拼接。

成本估算：配置化单价（每 1K tokens），缺失时按 0 计。不引入实时汇率或外部 API。

隐私：绝不记录任何 prompt/reply 原文，只存元数据。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional, Tuple


class LlmCostTracker:
    """按 (model, tier, account_id) 累积 tokens + 估算成本。

    - ``record(model, pt, ct, tier, account_id, latency_ms)`` → 原子自增
    - ``dump()`` → 返回嵌套字典（运维 API 用）
    - ``dump_prom()`` → Prometheus 文本（/metrics 拼接用）
    - 价格表由 ``set_pricing(model_prices)`` 注入；格式：
      ``{"gpt-4o-mini": {"prompt": 0.00015, "completion": 0.0006}}``
      （单位：USD / 1K tokens）
    """

    __slots__ = (
        "_lock", "_counters", "_pricing", "_started_at",
        "_last_record_ts", "_total_cost_usd", "_total_calls",
    )

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._counters: Dict[Tuple[str, str, str], Dict[str, float]] = {}
        self._pricing: Dict[str, Dict[str, float]] = {}
        self._started_at: float = time.time()
        self._last_record_ts: float = 0.0
        self._total_cost_usd: float = 0.0
        self._total_calls: int = 0

    def set_pricing(self, pricing: Dict[str, Dict[str, float]]) -> None:
        """注入价格表；key 为 model 名（小写归一化）。"""
        with self._lock:
            norm: Dict[str, Dict[str, float]] = {}
            for k, v in (pricing or {}).items():
                if not isinstance(v, dict):
                    continue
                norm[str(k).lower()] = {
                    "prompt": float(v.get("prompt", 0) or 0),
                    "completion": float(v.get("completion", 0) or 0),
                }
            self._pricing = norm

    def _price_for(self, model: str) -> Tuple[float, float]:
        key = (model or "").lower()
        p = self._pricing.get(key)
        if p is None:
            # 模糊匹配：去掉版本号后缀（gpt-4o-mini-2024-07-18 → gpt-4o-mini）
            for k, v in self._pricing.items():
                if key.startswith(k):
                    p = v
                    break
        if p is None:
            return (0.0, 0.0)
        return (p.get("prompt", 0.0), p.get("completion", 0.0))

    def record(
        self,
        *,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        tier: str = "default",
        account_id: str = "default",
        latency_ms: Optional[int] = None,
    ) -> Dict[str, Any]:
        """原子记录一次 LLM 调用。返回该 bucket 的累计值（运维用）。"""
        model = str(model or "unknown")
        tier = str(tier or "default")
        account_id = str(account_id or "default")
        pt = int(prompt_tokens or 0)
        ct = int(completion_tokens or 0)
        pp, cp = self._price_for(model)
        cost = (pt / 1000.0) * pp + (ct / 1000.0) * cp
        key = (model, tier, account_id)
        with self._lock:
            row = self._counters.get(key)
            if row is None:
                row = {
                    "calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
                    "cost_usd": 0.0, "latency_ms_sum": 0,
                }
                self._counters[key] = row
            row["calls"] += 1
            row["prompt_tokens"] += pt
            row["completion_tokens"] += ct
            row["cost_usd"] += cost
            if latency_ms is not None:
                row["latency_ms_sum"] += int(latency_ms)
            self._total_cost_usd += cost
            self._total_calls += 1
            self._last_record_ts = time.time()
            return dict(row)

    def dump(self) -> Dict[str, Any]:
        """返回完整状态（供 JSON API 使用）。"""
        with self._lock:
            rows = []
            for (m, t, a), v in sorted(self._counters.items()):
                rows.append({
                    "model": m, "tier": t, "account_id": a,
                    **v,
                    "avg_latency_ms": (
                        v["latency_ms_sum"] / v["calls"] if v["calls"] else 0
                    ),
                })
            return {
                "started_at": self._started_at,
                "last_record_ts": self._last_record_ts,
                "total_calls": self._total_calls,
                "total_cost_usd": round(self._total_cost_usd, 6),
                "pricing_models": sorted(self._pricing.keys()),
                "rows": rows,
            }

    def dump_prom(self) -> str:
        """Prometheus 文本。"""
        lines = []
        with self._lock:
            lines.append(
                "# HELP messenger_rpa_llm_total_cost_usd Accumulated LLM cost in USD"
            )
            lines.append("# TYPE messenger_rpa_llm_total_cost_usd counter")
            lines.append(
                f"messenger_rpa_llm_total_cost_usd {self._total_cost_usd:.6f}"
            )
            lines.append(
                "# HELP messenger_rpa_llm_total_calls LLM calls (all accounts+tiers)"
            )
            lines.append("# TYPE messenger_rpa_llm_total_calls counter")
            lines.append(f"messenger_rpa_llm_total_calls {self._total_calls}")

            lines.append(
                "# HELP messenger_rpa_llm_tokens_total Total LLM tokens by bucket"
            )
            lines.append("# TYPE messenger_rpa_llm_tokens_total counter")
            lines.append(
                "# HELP messenger_rpa_llm_cost_usd_total LLM cost USD by bucket"
            )
            lines.append("# TYPE messenger_rpa_llm_cost_usd_total counter")
            lines.append(
                "# HELP messenger_rpa_llm_calls_total LLM calls by bucket"
            )
            lines.append("# TYPE messenger_rpa_llm_calls_total counter")
            for (m, t, a), v in self._counters.items():
                labels = (
                    f'model="{_esc(m)}",tier="{_esc(t)}",account="{_esc(a)}"'
                )
                lines.append(
                    f'messenger_rpa_llm_tokens_total{{{labels},kind="prompt"}}'
                    f' {v["prompt_tokens"]}'
                )
                lines.append(
                    f'messenger_rpa_llm_tokens_total{{{labels},kind="completion"}}'
                    f' {v["completion_tokens"]}'
                )
                lines.append(
                    f'messenger_rpa_llm_cost_usd_total{{{labels}}} '
                    f'{v["cost_usd"]:.6f}'
                )
                lines.append(
                    f'messenger_rpa_llm_calls_total{{{labels}}} {v["calls"]}'
                )
        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._total_cost_usd = 0.0
            self._total_calls = 0
            self._last_record_ts = 0.0


def _esc(s: str) -> str:
    return str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


_SINGLETON: Optional[LlmCostTracker] = None
_LOCK = threading.Lock()


def get_llm_cost() -> LlmCostTracker:
    global _SINGLETON
    if _SINGLETON is None:
        with _LOCK:
            if _SINGLETON is None:
                _SINGLETON = LlmCostTracker()
    return _SINGLETON


__all__ = ["LlmCostTracker", "get_llm_cost"]
