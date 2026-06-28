"""把"真实运营信号"翻成"该不该往上爬一档"的建议（纯函数）。

看板前几增量答"能不能开 / 开了生效没"；本层答"**开了好不好 / 该不该扩量**"：
吃已有的运营指标——
  - 文本自动回复质量：``InboxStore.get_quality_stats``（自动放行率/改写率/弃用率…）
  - 主动触达质量：``MetricsStore.companion_quality_overview``（care+reactivation 好评率）
  - 真发投递：近窗口 autosend/autosend_failed
给每档一个 verdict（healthy 可推进 / caution 待改进 / insufficient 样本不足 / failing 出问题）
+ 一句**数据驱动的下一步建议**，把"手动拍脑袋开闸"变成"看数灰度放量"。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

# 默认阈值（可被 thresholds 覆盖；运营/CI 可调）
DEFAULTS = {
    "text_min_n": 20,         # 文本质量判定所需最少处置样本
    "text_pass_target": 0.55,  # 自动放行率达标线
    "text_edit_max": 0.50,     # 坐席改写率上限
    "text_reject_max": 0.25,   # 弃用率上限
    "text_high_risk_max": 0.30,
    "proactive_min_fb": 8,     # 主动触达判定所需最少人工反馈
    "proactive_like_target": 0.75,
    "delivery_fail_warn": 0.10,  # 真发失败率告警线
}


def _pct(x: float) -> float:
    return round(float(x) * 100, 1)


def text_autosend_signal(qs: Optional[Dict[str, Any]], th: Dict[str, Any]) -> Dict[str, Any]:
    """Tier2 全自动文本：从草稿处置质量判初稿好不好、能否扩量真发。"""
    base = {"tier": 2, "key": "l2_autosend_deliver", "label": "全自动文本质量"}
    if not isinstance(qs, dict):
        return {**base, "verdict": "unavailable", "advice": "质量数据不可用",
                "metric": {}}
    total = int(qs.get("total") or 0)
    auto = float(qs.get("auto_pass_rate") or 0.0)
    edit = float(qs.get("edit_rate") or 0.0)
    rej = float(qs.get("reject_rate") or 0.0)
    hr = float(qs.get("high_risk_rate") or 0.0)
    metric = {"total": total, "auto_pass_rate": auto, "edit_rate": edit,
              "reject_rate": rej, "high_risk_rate": hr}
    if total < th["text_min_n"]:
        return {**base, "verdict": "insufficient", "metric": metric,
                "advice": f"AI 处置样本不足（{total}/{th['text_min_n']}），继续积累再判断扩量"}
    if (auto >= th["text_pass_target"] and edit <= th["text_edit_max"]
            and rej <= th["text_reject_max"]):
        note = "（高风险占比偏高，留意 L3/L4）" if hr > th["text_high_risk_max"] else ""
        return {**base, "verdict": "healthy", "metric": metric,
                "advice": f"初稿质量达标：自动放行 {_pct(auto)}%、改写 {_pct(edit)}%、"
                          f"弃用 {_pct(rej)}% → 可灰度扩大真发{note}"}
    # caution：点名最该改的那个指标
    if rej > th["text_reject_max"]:
        why = f"弃用率偏高 {_pct(rej)}%，先改进话术/KB 再扩量"
    elif edit > th["text_edit_max"]:
        why = f"坐席改写率偏高 {_pct(edit)}%，初稿质量待提升"
    elif auto < th["text_pass_target"]:
        why = f"自动放行率偏低 {_pct(auto)}%，多为人工处置，扩量收益有限"
    else:
        why = "质量指标未全部达标，暂缓扩量"
    return {**base, "verdict": "caution", "metric": metric, "advice": why}


def proactive_signal(qo: Optional[Dict[str, Any]], th: Dict[str, Any]) -> Dict[str, Any]:
    """Tier3 主动触达：从 care+reactivation 好评率判 dry_run 能否转真发。"""
    base = {"tier": 3, "key": "proactive_topic", "label": "主动触达好评"}
    if not isinstance(qo, dict):
        return {**base, "verdict": "unavailable", "advice": "主动触达质量数据不可用",
                "metric": {}}
    like = dislike = 0
    for sec in ("care", "reactivation"):
        fb = ((qo.get(sec) or {}).get("feedback") or {})
        like += int(fb.get("like") or 0)
        dislike += int(fb.get("dislike") or 0)
    total = like + dislike
    rate = (like / total) if total else 0.0
    metric = {"like": like, "dislike": dislike, "total": total,
              "like_rate": round(rate, 3),
              "blacklist": int(qo.get("disliked_blacklist_size") or 0)}
    if total < th["proactive_min_fb"]:
        return {**base, "verdict": "insufficient", "metric": metric,
                "advice": f"主动触达反馈样本不足（{total}/{th['proactive_min_fb']}），"
                          "保持 dry_run 多采样"}
    if rate >= th["proactive_like_target"]:
        return {**base, "verdict": "healthy", "metric": metric,
                "advice": f"好评率 {_pct(rate)}% 达标 → dry_run 可转真发（小流量灰度）"}
    return {**base, "verdict": "caution", "metric": metric,
            "advice": f"好评率 {_pct(rate)}% 偏低，继续 dry_run 调参（看 skip 原因/dislike 黑名单）"}


def delivery_signal(delivery: Optional[Dict[str, Any]], th: Dict[str, Any]) -> Dict[str, Any]:
    """真发投递健康：近窗口失败率。"""
    base = {"tier": 2, "key": "delivery_health", "label": "真发投递健康"}
    d = delivery or {}
    sent = d.get("autosend")
    failed = d.get("autosend_failed")
    if sent is None and failed is None:
        return {**base, "verdict": "unknown", "advice": "审计不可用，无法评估投递",
                "metric": {}}
    sent = int(sent or 0)
    failed = int(failed or 0)
    denom = sent + failed
    fr = (failed / denom) if denom else 0.0
    metric = {"autosend": sent, "autosend_failed": failed, "fail_rate": round(fr, 3)}
    if denom == 0:
        return {**base, "verdict": "idle", "metric": metric, "advice": "近窗口无真发记录"}
    if fr > th["delivery_fail_warn"]:
        return {**base, "verdict": "failing", "metric": metric,
                "advice": f"真发失败率 {_pct(fr)}%（{failed}/{denom}）偏高，排查投递通道"}
    return {**base, "verdict": "healthy", "metric": metric,
            "advice": f"真发 {sent} 条，失败率 {_pct(fr)}% 正常"}


_VERDICT_RANK = {"failing": 0, "caution": 1, "insufficient": 2,
                 "unavailable": 3, "unknown": 3, "idle": 4, "healthy": 5}


def readiness_signals(
    *,
    text_quality: Optional[Dict[str, Any]] = None,
    proactive_quality: Optional[Dict[str, Any]] = None,
    delivery: Optional[Dict[str, Any]] = None,
    thresholds: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """聚合三路信号 + 头条建议（最该处理的那条）。"""
    th = dict(DEFAULTS)
    if thresholds:
        th.update({k: v for k, v in thresholds.items() if v is not None})
    signals = [
        text_autosend_signal(text_quality, th),
        delivery_signal(delivery, th),
        proactive_signal(proactive_quality, th),
    ]
    headline = min(signals, key=lambda s: _VERDICT_RANK.get(s["verdict"], 9))
    return {
        "signals": signals,
        "headline": {"key": headline["key"], "verdict": headline["verdict"],
                     "advice": headline["advice"]},
        "thresholds": th,
    }


__all__ = [
    "DEFAULTS", "text_autosend_signal", "proactive_signal",
    "delivery_signal", "readiness_signals",
]
