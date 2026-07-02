"""防复读运行时调参顾问（纯函数，无副作用、可复现、易测）。

把 ``MetricsStore.snapshot()["anti_repeat"]`` 累计指标翻译成**可落地的调参建议**：

- **嵌入缓存容量**：命中率长期极高 → 容量过剩，可下调省内存；命中率偏低（复用不足、
  重复嵌入）→ 可上调。样本不足静默（不给噪音建议）。
- **语义层开关**：语义层长期零命中（字符层已足够）→ 可关语义省本地嵌入调用；语义层
  贡献显著（拦下字符层漏掉的改写复读）→ 明确其价值、建议保留。

风格对齐 ``src/utils/ai_quality_alert.py``：入参为已 dump 的指标 dict + config dict，
返回 {sample_ok, suggestions[], observed}。**不读全局单例、不发事件、不写库**——由调用方
（endpoint / watchdog）决定如何呈现。阈值全部走 ``inbox.auto_draft.anti_repeat.advisor.*``。
"""

from __future__ import annotations

from typing import Any, Dict, List


def _advisor_cfg(cfg: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(cfg, dict):
        return {}
    return ((((cfg.get("inbox") or {}).get("auto_draft") or {})
             .get("anti_repeat") or {}).get("advisor") or {})


def evaluate_anti_repeat_tuning(
    metrics: Dict[str, Any],
    cfg: Dict[str, Any] | None = None,
    *,
    current_cache_max: int = 512,
) -> Dict[str, Any]:
    """依据累计防复读指标产出调参建议（纯函数）。

    metrics：``snapshot()["anti_repeat"]`` 形态的 dict。
    cfg：全量 config dict（读 advisor 阈值）；current_cache_max：当前生效的缓存上限。
    """
    adv = _advisor_cfg(cfg or {})
    min_samples = int(adv.get("min_samples", 200) or 200)
    hit_high = float(adv.get("cache_hit_high_pct", 98.0) or 98.0)
    hit_low = float(adv.get("cache_hit_low_pct", 70.0) or 70.0)
    sem_min_checks = int(adv.get("semantic_min_checks", min_samples) or min_samples)
    sem_value_share = float(adv.get("semantic_value_share_pct", 20.0) or 20.0)
    cache_floor = int(adv.get("cache_max_floor", 64) or 64)

    m = metrics or {}
    ec = m.get("embed_cache") or {}
    hit = int(ec.get("hit") or 0)
    miss = int(ec.get("miss") or 0)
    ec_total = hit + miss
    hit_rate = float(ec.get("hit_rate_pct") or 0.0)
    checks = int(m.get("checks") or 0)
    sem_share = float(m.get("semantic_share_pct") or 0.0)
    sem_trig = int(m.get("semantic_triggered") or 0)
    cur = max(1, int(current_cache_max or 512))

    suggestions: List[Dict[str, Any]] = []
    _key = "inbox.auto_draft.anti_repeat.semantic.embed_cache_max"

    # ① 嵌入缓存容量（仅在语义层真跑过、发生过嵌入时才有意义）
    if ec_total >= min_samples:
        if hit_rate >= hit_high and cur > cache_floor:
            new = max(cache_floor, cur // 2)
            suggestions.append({
                "id": "embed_cache_shrink", "level": "info",
                "detail": (f"嵌入缓存命中率 {hit_rate:.0f}% ≥ {hit_high:.0f}%（n={ec_total}）——"
                           f"容量 {cur} 绰绰有余，可下调至 {new} 省内存。"),
                "suggested": {_key: new},
            })
        elif hit_rate < hit_low:
            new = cur * 2
            suggestions.append({
                "id": "embed_cache_grow", "level": "warning",
                "detail": (f"嵌入缓存命中率 {hit_rate:.0f}% < {hit_low:.0f}%（n={ec_total}）——"
                           f"复用不足致重复嵌入，可上调至 {new} 提高稳态命中。"),
                "suggested": {_key: new},
            })

    # ② 语义层价值 / 开关
    if checks >= sem_min_checks:
        if sem_trig == 0:
            suggestions.append({
                "id": "semantic_no_value", "level": "info",
                "detail": (f"近 {checks} 次判定中语义层零命中（字符层已足够）——"
                           "可关闭语义层省本地嵌入开销："
                           "inbox.auto_draft.anti_repeat.semantic.enabled=false。"),
                "suggested": {"inbox.auto_draft.anti_repeat.semantic.enabled": False},
            })
        elif sem_share >= sem_value_share:
            suggestions.append({
                "id": "semantic_valuable", "level": "info",
                "detail": (f"语义层贡献了 {sem_share:.0f}% 的复读拦截（字符层漏掉的改写复读），"
                           "价值显著，建议保留。"),
            })

    sample_ok = (ec_total >= min_samples) or (checks >= sem_min_checks)
    return {
        "sample_ok": sample_ok,
        "suggestions": suggestions,
        "observed": {
            "checks": checks,
            "semantic_triggered": sem_trig,
            "semantic_share_pct": sem_share,
            "embed_cache_hit_rate_pct": hit_rate,
            "embed_cache_total": ec_total,
            "current_cache_max": cur,
        },
        "thresholds": {
            "min_samples": min_samples,
            "cache_hit_high_pct": hit_high,
            "cache_hit_low_pct": hit_low,
            "semantic_min_checks": sem_min_checks,
            "semantic_value_share_pct": sem_value_share,
        },
    }


__all__ = ["evaluate_anti_repeat_tuning"]
