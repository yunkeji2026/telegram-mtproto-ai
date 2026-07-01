"""AI 回复质量退化告警评估（F1，纯函数）。

基于 ``InboxStore.ai_safety_summary`` 的**处置结果口径**（草稿采纳/弃用率 + 高危量）判定
「AI 发得靠不靠谱」是否退化到需值班介入。纯函数、无 I/O：喂 ``cur``（当前窗口 summary）+
``prev``（上一等长窗口 summary，供环比）+ 阈值，产出 ``{light, problems}``，由
:class:`~src.inbox.health_watchdog.HealthWatchdog` 落 ``ops_incidents``（kind=``ai_quality``）
——与 health/billing/draft_quality 同一条运维事件闭环（可 ack/指派/自动恢复）。

分级：
  - ``fail`` → red（建议立即处理）；``warn`` → yellow（有空即看）；无问题 → green。
样本不足（``reviewed < min_samples``）静默返回 green——**不在薄数据上乱报**（采纳率在个位
数样本上抖动无意义）。默认阈值保守且在 watchdog 侧**默认关**，须按真实分布校准后再开。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# 默认阈值（保守）。生产经 config ``inbox.ai_quality_alert.*`` 覆盖并 opt-in 开启。
DEFAULT_AI_QUALITY_THRESHOLDS: Dict[str, Any] = {
    "min_samples": 20,      # 人工审过样本下限；不足则不评（防薄数据误报）
    "adopt_min": 0.40,      # 采纳率低于此 → warn
    "adopt_severe": 0.20,   # 采纳率低于此 → fail（AI 草稿几乎没人直接用）
    "reject_max": 0.35,     # 弃用率高于此 → warn
    "reject_severe": 0.60,  # 弃用率高于此 → fail（AI 草稿大量被整条丢弃）
    "high_risk_min": 5,     # 高危量绝对下限（低于此不看环比，防小数波动误报）
    "high_risk_spike": 5,   # 高危量环比增量 ≥ 此 → warn（风控边缘量抬升）
}

# 阈值键 → 类型转换器（率为 float、计数为 int）。写 config overlay 前用它白名单+强制类型。
_THRESHOLD_CASTERS: Dict[str, Any] = {
    "min_samples": int, "adopt_min": float, "adopt_severe": float,
    "reject_max": float, "reject_severe": float,
    "high_risk_min": int, "high_risk_spike": int,
}


def sanitize_ai_quality_thresholds(raw: Any) -> Dict[str, Any]:
    """把外部（校准 UI body）阈值输入收敛为可安全写入 ``config.local.yaml`` 的合法键值：
    **只留白名单键**（防注入任意配置键）、强制类型（率→float / 计数→int）、丢弃
    ``None``/不可解析/越界（率须 ∈[0,1]、计数须 ≥0）。返回 ``{key: casted}``（可空）。

    纯函数：F2b++ 「校准满意 → 一键写回 config」端点据此清洗后逐键过
    :meth:`ConfigManager.set_overlay_flag`，与能力开关同一治理化写入机制。
    """
    out: Dict[str, Any] = {}
    if not isinstance(raw, dict):
        return out
    for key, caster in _THRESHOLD_CASTERS.items():
        if key not in raw or raw[key] is None:
            continue
        try:
            val = caster(raw[key])
        except (TypeError, ValueError):
            continue
        if caster is float and not (0.0 <= val <= 1.0):
            continue
        if caster is int and val < 0:
            continue
        out[key] = val
    return out


def evaluate_ai_quality(
    cur: Optional[Dict[str, Any]],
    prev: Optional[Dict[str, Any]] = None,
    thresholds: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """评估 AI 回复质量是否退化，返回 ``{"light": str, "problems": [ {id,name,status,detail} ]}``。

    ``cur``/``prev`` 为 ``ai_safety_summary`` 结果（含 reviewed/adopt_rate/reject_rate/high_risk）。
    ``thresholds`` 覆盖 :data:`DEFAULT_AI_QUALITY_THRESHOLDS`（None 值忽略，回落默认）。
    """
    t = dict(DEFAULT_AI_QUALITY_THRESHOLDS)
    if thresholds:
        t.update({k: thresholds[k] for k in DEFAULT_AI_QUALITY_THRESHOLDS
                  if k in thresholds and thresholds[k] is not None})
    cur = cur or {}
    prev = prev or {}
    problems: List[Dict[str, Any]] = []

    reviewed = int(cur.get("reviewed") or 0)
    # 采纳/弃用率仅在样本充足时评（薄数据抖动无意义）。
    if reviewed >= int(t["min_samples"]):
        adopt = float(cur.get("adopt_rate") or 0.0)
        if adopt < float(t["adopt_min"]):
            problems.append({
                "id": "adopt_rate_low",
                "name": "草稿采纳率",
                "status": "fail" if adopt < float(t["adopt_severe"]) else "warn",
                "detail": (f"近窗口采纳率 {adopt:.0%} < 阈值 {float(t['adopt_min']):.0%}"
                           f"（n={reviewed}，坐席在大量改写/弃用 AI 草稿）"),
            })
        reject = float(cur.get("reject_rate") or 0.0)
        if reject > float(t["reject_max"]):
            problems.append({
                "id": "reject_rate_high",
                "name": "草稿弃用率",
                "status": "fail" if reject > float(t["reject_severe"]) else "warn",
                "detail": (f"近窗口弃用率 {reject:.0%} > 阈值 {float(t['reject_max']):.0%}"
                           f"（n={reviewed}，AI 草稿大量被整条丢弃）"),
            })

    # 高危量环比激增：安全前瞻指标（非停摆）→ 恒 warn。带绝对下限防小数波动。
    high_risk = int(cur.get("high_risk") or 0)
    prev_high_risk = int(prev.get("high_risk") or 0)
    delta_hr = high_risk - prev_high_risk
    if high_risk >= int(t["high_risk_min"]) and delta_hr >= int(t["high_risk_spike"]):
        problems.append({
            "id": "high_risk_spike",
            "name": "高危草稿量激增",
            "status": "warn",
            "detail": (f"高危草稿 {high_risk}（环比 +{delta_hr}），"
                       "风控边缘量抬升，建议抽查近期生成"),
        })

    light = "green"
    if problems:
        light = "red" if any(p["status"] == "fail" for p in problems) else "yellow"
    return {"light": light, "problems": problems}


def _median(vals: List[float]):
    s = sorted(vals)
    n = len(s)
    if not n:
        return 0
    m = n // 2
    return s[m] if n % 2 else round((s[m - 1] + s[m]) / 2, 3)


def calibrate_ai_quality(
    daily: Optional[List[Dict[str, Any]]],
    thresholds: Optional[Dict[str, Any]] = None,
    *,
    window_days: int = 7,
) -> Dict[str, Any]:
    """据按日计数序列（:meth:`InboxStore.ai_quality_daily_series`）滚动 ``window_days`` 窗口，
    **复刻 watchdog 的告警评估**（同 :func:`evaluate_ai_quality` + 同去抖），反推「若按此阈值，
    历史窗口会告警几次 / 在警几天 / 各信号命中几次 / 窗口内指标分布如何」——上线前用真实数据
    定阈值，而非拍脑袋。纯函数、无 I/O。

    ``daily`` 升序 ``[{day, reviewed, approved, edited, rejected, high_risk, ...}]``。返回
    ``{window_days, evaluated_windows, alerts(去抖后触发次数), days_in_alert, worst_light,
       by_signal{id:命中窗口数}, distribution{adopt_rate/reject_rate/high_risk:{min,median,max,n}},
       points:[{day, light, problem_ids}]}``。分布取自各评估窗口的窗口值（与 watchdog 同视角）。
    """
    win = max(1, int(window_days or 7))
    series = list(daily or [])
    n = len(series)

    def _agg(lo: int, hi: int) -> Dict[str, Any]:
        approved = edited = rejected = high_risk = 0
        for i in range(lo, hi + 1):
            r = series[i]
            approved += int(r.get("approved") or 0)
            edited += int(r.get("edited") or 0)
            rejected += int(r.get("rejected") or 0)
            high_risk += int(r.get("high_risk") or 0)
        reviewed = approved + edited + rejected

        def _rate(a: int, b: int) -> float:
            return round(a / b, 3) if b else 0.0

        return {"reviewed": reviewed, "approved": approved, "edited": edited,
                "rejected": rejected, "high_risk": high_risk,
                "adopt_rate": _rate(approved, reviewed),
                "edit_rate": _rate(edited, reviewed),
                "reject_rate": _rate(rejected, reviewed)}

    order = {"green": 0, "yellow": 1, "red": 2}
    alerts = 0
    days_in_alert = 0
    by_signal: Dict[str, int] = {}
    points: List[Dict[str, Any]] = []
    worst = "green"
    last_sig: Optional[str] = None
    adopt_samp: List[float] = []
    reject_samp: List[float] = []
    hr_samp: List[float] = []

    for i in range(win - 1, n):
        cur = _agg(i - win + 1, i)
        prev = _agg(i - 2 * win + 1, i - win) if (i - 2 * win + 1) >= 0 else {}
        res = evaluate_ai_quality(cur, prev, thresholds)
        probs = res["problems"]
        light = res["light"]
        ids = sorted(p["id"] for p in probs)
        sig = "|".join(sorted(f"{p['id']}:{p['status']}" for p in probs))
        if probs:
            days_in_alert += 1
            if sig != last_sig:  # 签名变化=新告警（复刻 watchdog 去抖）
                alerts += 1
            for pid in ids:
                by_signal[pid] = by_signal.get(pid, 0) + 1
            if order[light] > order[worst]:
                worst = light
            last_sig = sig
        else:
            last_sig = None
        if cur["reviewed"] > 0:
            adopt_samp.append(cur["adopt_rate"])
            reject_samp.append(cur["reject_rate"])
        hr_samp.append(cur["high_risk"])
        points.append({"day": str(series[i].get("day") or ""),
                       "light": light, "problem_ids": ids,
                       "adopt_rate": cur["adopt_rate"], "reject_rate": cur["reject_rate"],
                       "high_risk": cur["high_risk"]})

    def _dist(vals: List[float]) -> Dict[str, Any]:
        return {"min": (min(vals) if vals else 0), "median": _median(vals),
                "max": (max(vals) if vals else 0), "n": len(vals)}

    return {
        "window_days": win,
        "evaluated_windows": len(points),
        "alerts": alerts,
        "days_in_alert": days_in_alert,
        "worst_light": worst,
        "by_signal": by_signal,
        "distribution": {
            "adopt_rate": _dist(adopt_samp),
            "reject_rate": _dist(reject_samp),
            "high_risk": _dist(hr_samp),
        },
        "points": points,
    }
