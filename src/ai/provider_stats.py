"""P58：通用 provider 用量观测（进程级单例，按 namespace 注册）。

把 P57「翻译引擎 stats + 降级」模式抽象为可复用工具：OCR / ASR 等任何
「多后端 + 故障转移」的外部 provider 都能复用同一套 调用/成功/失败/延迟/降级 计数。

风格对齐 src/ai/llm_cost.py 与 translation_engine_stats.py：无新增依赖；
JSON 供 /api/workspace/metrics，Prometheus 文本由 Web 路由读 dump_prom() 拼接。
绝不记录任何原文/译文，只存元数据。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional


class ProviderStats:
    """按 provider 名聚合 调用/成功/失败/平均延迟 + 全局降级次数。"""

    __slots__ = ("_lock", "_rows", "_started_at", "_last_ts", "_fallbacks",
                 "_total", "_prefix", "_cache_hits", "_labels")

    def __init__(self, metric_prefix: str = "provider") -> None:
        self._lock = threading.RLock()
        self._rows: Dict[str, Dict[str, float]] = {}
        self._started_at = time.time()
        self._last_ts = 0.0
        self._fallbacks = 0
        self._total = 0
        self._cache_hits = 0
        self._labels: Dict[str, int] = {}
        self._prefix = str(metric_prefix or "provider")

    def record(
        self, name: str, *, ok: bool, latency_ms: int = 0, cost_usd: float = 0.0,
    ) -> None:
        """记一次 provider 调用。``cost_usd`` 累加该 provider 的花费（如 TTS 字符计费）。"""
        name = str(name or "unknown")
        with self._lock:
            row = self._rows.get(name)
            if row is None:
                row = {"calls": 0, "ok": 0, "fail": 0, "latency_ms_sum": 0,
                       "cost_usd_sum": 0.0}
                self._rows[name] = row
            row["calls"] += 1
            row["ok" if ok else "fail"] += 1
            row["latency_ms_sum"] += max(0, int(latency_ms or 0))
            row["cost_usd_sum"] = row.get("cost_usd_sum", 0.0) + max(0.0, float(cost_usd or 0))
            self._total += 1
            self._last_ts = time.time()

    def record_fallback(self) -> None:
        with self._lock:
            self._fallbacks += 1

    def record_cache_hit(self) -> None:
        """记一次缓存命中（未触达 provider，省一次调用/花费）。"""
        with self._lock:
            self._cache_hits += 1
            self._last_ts = time.time()

    def record_label(self, value: str) -> None:
        """记一次「标签」分布（通用维度，如 TTS 情绪 / ASR 语言）。空值忽略。"""
        v = str(value or "").strip()
        if not v:
            return
        with self._lock:
            self._labels[v] = self._labels.get(v, 0) + 1

    def dump(self) -> Dict[str, Any]:
        with self._lock:
            rows = []
            total_cost = 0.0
            for name, v in sorted(self._rows.items()):
                calls = v["calls"]
                cost = round(v.get("cost_usd_sum", 0.0), 4)
                total_cost += v.get("cost_usd_sum", 0.0)
                rows.append({
                    "provider": name,
                    "calls": int(calls),
                    "ok": int(v["ok"]),
                    "fail": int(v["fail"]),
                    "success_rate": round(v["ok"] / calls, 4) if calls else 0,
                    "avg_latency_ms": round(v["latency_ms_sum"] / calls, 1) if calls else 0,
                    "cost_usd": cost,
                })
            # 缓存命中率 = hits / (hits + 实际调用)
            denom = self._cache_hits + self._total
            return {
                "started_at": self._started_at,
                "last_record_ts": self._last_ts,
                "total_attempts": self._total,
                "fallbacks": self._fallbacks,
                "cache_hits": self._cache_hits,
                "cache_hit_rate": round(self._cache_hits / denom, 4) if denom else 0,
                "total_cost_usd": round(total_cost, 4),
                "labels": dict(sorted(self._labels.items(), key=lambda kv: -kv[1])),
                "rows": rows,
            }

    def dump_prom(self) -> str:
        p = self._prefix
        lines = [
            f"# HELP {p}_attempts_total {p} attempts by provider",
            f"# TYPE {p}_attempts_total counter",
            f"# HELP {p}_fail_total {p} failures by provider",
            f"# TYPE {p}_fail_total counter",
            f"# HELP {p}_fallbacks_total {p} fallbacks (primary failed)",
            f"# TYPE {p}_fallbacks_total counter",
        ]
        lines += [
            f"# HELP {p}_cache_hits_total {p} cache hits (provider not called)",
            f"# TYPE {p}_cache_hits_total counter",
            f"# HELP {p}_cost_usd_total {p} cumulative cost in USD by provider",
            f"# TYPE {p}_cost_usd_total counter",
        ]
        lines += [
            f"# HELP {p}_label_total {p} label distribution (e.g. TTS emotion)",
            f"# TYPE {p}_label_total counter",
        ]
        with self._lock:
            lines.append(f"{p}_fallbacks_total {self._fallbacks}")
            lines.append(f"{p}_cache_hits_total {self._cache_hits}")
            for lv, cnt in self._labels.items():
                lines.append(f'{p}_label_total{{label="{_esc(lv)}"}} {int(cnt)}')
            for name, v in self._rows.items():
                lbl = f'provider="{_esc(name)}"'
                lines.append(f'{p}_attempts_total{{{lbl}}} {int(v["calls"])}')
                lines.append(f'{p}_fail_total{{{lbl}}} {int(v["fail"])}')
                cost = round(v.get("cost_usd_sum", 0.0), 6)
                if cost:
                    lines.append(f'{p}_cost_usd_total{{{lbl}}} {cost}')
        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        with self._lock:
            self._rows.clear()
            self._labels.clear()
            self._fallbacks = 0
            self._total = 0
            self._cache_hits = 0
            self._last_ts = 0.0


def _esc(s: str) -> str:
    return str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


_REGISTRY: Dict[str, ProviderStats] = {}
_LOCK = threading.Lock()


def get_provider_stats(namespace: str, metric_prefix: Optional[str] = None) -> ProviderStats:
    """按 namespace 返回单例（如 "ocr" / "asr"）。metric_prefix 缺省 = namespace。"""
    ns = str(namespace or "provider")
    inst = _REGISTRY.get(ns)
    if inst is None:
        with _LOCK:
            inst = _REGISTRY.get(ns)
            if inst is None:
                inst = ProviderStats(metric_prefix or ns)
                _REGISTRY[ns] = inst
    return inst


def all_provider_stats() -> Dict[str, Dict[str, Any]]:
    """所有已注册 namespace 的 dump（供统一 metrics 端点）。"""
    with _LOCK:
        names = list(_REGISTRY.keys())
    return {ns: _REGISTRY[ns].dump() for ns in names}


def all_provider_prom() -> str:
    with _LOCK:
        insts = list(_REGISTRY.values())
    return "".join(i.dump_prom() for i in insts)


__all__ = [
    "ProviderStats",
    "get_provider_stats",
    "all_provider_stats",
    "all_provider_prom",
]
