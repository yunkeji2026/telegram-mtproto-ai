"""B2：KPI 漏斗告警检测逻辑（纯函数，与 DB 解耦，可单独测试）。

算法设计：
  - 输入：``count_stage_transitions_by_day()`` + 每日转化率（由 timeseries API 计算）
  - 历史窗口：今天之前最多 7 天的有效率值
  - 检测方法：若「当日值 vs 7 日均值」下降幅度 ≥ drop_pct_threshold（默认 30%），
    且当日绝对值 ≤ abs_floor_pct（默认 80%），且当日分母量 ≥ min_daily_volume（默认 3），
    才生成告警
  - 冷启动保护：有效历史天数 < min_days_required（默认 5）时跳过，防空数据期误报

优化亮点：
  1. 纯函数 —— 无 IO、无副作用，单元测试无需 DB
  2. 量保护 —— min_daily_volume 防 1/2 个事件时 50%→0% 的伪下跌
  3. abs_floor_pct 双重门槛 —— 防高基数微降触发无意义告警（如 95%→90% 不值得噪音）
  4. 双倍阈值升级 —— drop ≥ 2×threshold 自动 critical，提升可行性分级
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

_DEFAULT_THRESHOLDS: Dict[str, Any] = {
    "drop_pct_threshold": 30.0,
    "abs_floor_pct": 80.0,
    "min_days_required": 5,
    "min_daily_volume": 3,
}

# (rate_key, 中文 label, 分母 stage key)
_KPI_DEFS: List[tuple] = [
    ("engaged_rate",  "互动率",  "INITIAL"),
    ("handoff_rate",  "引流率",  "ENGAGED"),
    ("line_add_rate", "加友率",  "HANDOFF_SENT"),
    ("bonded_rate",   "成交率",  "LINE_ADDED"),
]


def detect_kpi_drops(
    series: List[Dict[str, Any]],
    *,
    thresholds: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """从 funnel timeseries series 检测 KPI 下跌。

    Args:
        series: ``/api/funnel/timeseries`` 的 ``series`` 字段（升序，今天最后一项）。
                每项含 ``{day, by_stage, rates}``；``rates`` 由调用方预先计算好。
        thresholds: 覆盖 ``_DEFAULT_THRESHOLDS`` 的任意子集。

    Returns:
        list[dict]，每项是一条待插入告警的原始数据，格式::

            {kind, severity, message, detail: {rate_key, label, today_val,
              avg_7d, drop_pct, today_volume, history_days, today_day}}

        空列表 = 无需告警。
    """
    thr = dict(_DEFAULT_THRESHOLDS)
    if thresholds:
        thr.update(thresholds)

    if not series or len(series) < 2:
        return []

    today = series[-1]
    today_rates = today.get("rates") or {}
    today_by_stage = today.get("by_stage") or {}

    window = series[-8:-1]

    drop_pct_thr = float(thr["drop_pct_threshold"])
    abs_floor = float(thr["abs_floor_pct"])
    min_days = int(thr["min_days_required"])
    min_vol = int(thr["min_daily_volume"])

    alerts: List[Dict[str, Any]] = []

    for rate_key, label, volume_stage in _KPI_DEFS:
        today_val = today_rates.get(rate_key)
        if today_val is None:
            continue

        vol = int(today_by_stage.get(volume_stage) or 0)
        if vol < min_vol:
            continue

        hist_vals = []
        for item in window:
            v = (item.get("rates") or {}).get(rate_key)
            if v is not None:
                hist_vals.append(float(v))

        if len(hist_vals) < min_days:
            continue

        avg_7d = sum(hist_vals) / len(hist_vals)

        if today_val > abs_floor:
            continue

        if avg_7d <= 0:
            continue
        drop_pct = (avg_7d - today_val) / avg_7d * 100

        if drop_pct < drop_pct_thr:
            continue

        severity = "critical" if drop_pct >= drop_pct_thr * 2 else "warn"
        kind = f"kpi_drop_{rate_key}"
        message = (
            f"{label} 下跌 {drop_pct:.0f}%："
            f"今日 {today_val:.1f}%，近 {len(hist_vals)} 日均值 {avg_7d:.1f}%"
        )
        alerts.append({
            "kind": kind,
            "severity": severity,
            "message": message,
            "detail": {
                "rate_key": rate_key,
                "label": label,
                "today_val": round(float(today_val), 1),
                "avg_7d": round(avg_7d, 1),
                "drop_pct": round(drop_pct, 1),
                "today_volume": vol,
                "history_days": len(hist_vals),
                "today_day": today.get("day"),
            },
        })

    return alerts
