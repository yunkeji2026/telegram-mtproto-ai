"""策略自动调优顾问 — 基于 A/B 追踪数据检测低效策略并生成优化建议

设计原则:
  - 惰性求值: 仅在仪表盘/API 访问时计算，不占后台资源
  - 三级告警: info / warn / critical
  - 可操作建议: 每条 advisory 附带具体参数调整方向
  - 复合质量评分: 综合多维指标给每个策略打分 0-100
  - Auto-Pilot: 持续低效策略自动降级 + 审计日志
"""

import time
import logging
from typing import Dict, List, Optional, Any, Tuple

logger = logging.getLogger("StrategyAdvisor")

# ── 评分权重 ──────────────────────────────────────
W_RESPONSE_TIME = 0.20   # 响应速度
W_SILENCE_RATE = 0.35    # 一次解决率（静默率高 = 好）
W_SAME_INTENT = 0.30     # 同意图追问（低 = 好）
W_TEMPLATE_EFF = 0.15    # 模板效率（省 API 成本）

# ── 阈值 ─────────────────────────────────────────
THRESHOLD_SAME_INTENT_WARN = 35.0
THRESHOLD_SAME_INTENT_CRIT = 55.0
THRESHOLD_RESPONSE_MS_WARN = 3000
THRESHOLD_RESPONSE_MS_CRIT = 6000
THRESHOLD_FOLLOW_UP_WARN = 55.0
THRESHOLD_FOLLOW_UP_CRIT = 75.0
MIN_SAMPLES = 10  # 少于此数不产生 advisory

# ── Auto-Pilot ──────────────────────────────────
AUTO_SCORE_THRESHOLD = 30          # 评分低于此值的策略视为候选降级
AUTO_MIN_SAMPLES = 20              # Auto-Pilot 最小样本数（比 advisory 更保守）
AUTO_SCORE_GAP = 15                # 切换到的目标策略必须比当前高至少这么多分
PARAM_ADJUST_RULES = {
    "high_same_intent": {"context_rounds": 2, "max_tokens": 128},
    "slow_response": {"max_tokens": -128, "context_rounds": -1},
    "high_follow_up": {"context_rounds": 2, "temperature": -0.1},
}


class Advisory:
    __slots__ = ("level", "strategy_id", "metric", "value", "threshold",
                 "message", "suggestion", "ts")

    def __init__(self, level: str, strategy_id: str, metric: str,
                 value: float, threshold: float, message: str, suggestion: str):
        self.level = level
        self.strategy_id = strategy_id
        self.metric = metric
        self.value = value
        self.threshold = threshold
        self.message = message
        self.suggestion = suggestion
        self.ts = time.time()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "level": self.level,
            "strategy_id": self.strategy_id,
            "metric": self.metric,
            "value": self.value,
            "threshold": self.threshold,
            "message": self.message,
            "suggestion": self.suggestion,
            "ts": self.ts,
        }


def compute_quality_score(s: Dict) -> float:
    """根据策略汇总指标计算复合质量评分 0-100"""
    # 响应时间评分: 0-500ms=100, 500-3000ms线性衰减, >6000ms=0
    avg_ms = s.get("avg_ms", 0)
    if avg_ms <= 500:
        rt_score = 100
    elif avg_ms >= 6000:
        rt_score = 0
    else:
        rt_score = max(0, 100 - (avg_ms - 500) / 55)

    # 静默率评分: silence_rate 越高越好（一次解决）
    silence = s.get("silence_rate", 50)
    sr_score = min(100, silence * 1.25)

    # 同意图追问惩罚: 越低越好
    same = s.get("same_intent_rate", 0)
    si_score = max(0, 100 - same * 2)

    # 模板效率: template_hit_rate 越高 → API 调用越少 → 成本越低
    tpl = s.get("template_hit_rate", 0)
    te_score = min(100, tpl * 1.5)

    score = (W_RESPONSE_TIME * rt_score +
             W_SILENCE_RATE * sr_score +
             W_SAME_INTENT * si_score +
             W_TEMPLATE_EFF * te_score)
    return round(min(100, max(0, score)), 1)


def analyze(summary: List[Dict], strategies_config: Dict = None) -> Dict[str, Any]:
    """分析策略效果，返回评分 + 告警 + 建议

    Args:
        summary: StrategyTracker.strategy_summary() 的结果
        strategies_config: 当前策略配置（用于生成具体参数建议）

    Returns:
        {"scores": {sid: float}, "advisories": [Advisory.to_dict()],
         "best": sid, "worst": sid}
    """
    advisories: List[Advisory] = []
    scores: Dict[str, float] = {}
    strategies_config = strategies_config or {}

    for s in summary:
        sid = s["strategy_id"]
        total = s.get("total", 0)
        scores[sid] = compute_quality_score(s)

        if total < MIN_SAMPLES:
            continue

        # ── 同意图追问率检查 ──
        sir = s.get("same_intent_rate", 0)
        if sir >= THRESHOLD_SAME_INTENT_CRIT:
            advisories.append(Advisory(
                "critical", sid, "same_intent_rate", sir, THRESHOLD_SAME_INTENT_CRIT,
                f"策略 {sid} 同意图追问率 {sir}%，严重偏高",
                "建议增加 context_rounds 或切换到更高 max_tokens 的策略，让 AI 给出更完整的回答",
            ))
        elif sir >= THRESHOLD_SAME_INTENT_WARN:
            advisories.append(Advisory(
                "warn", sid, "same_intent_rate", sir, THRESHOLD_SAME_INTENT_WARN,
                f"策略 {sid} 同意图追问率 {sir}%，偏高",
                "可适当提高 temperature 增加回复多样性，或增加 context_rounds 让 AI 理解更多上下文",
            ))

        # ── 响应时间检查 ──
        avg_ms = s.get("avg_ms", 0)
        if avg_ms >= THRESHOLD_RESPONSE_MS_CRIT:
            advisories.append(Advisory(
                "critical", sid, "avg_response_ms", avg_ms, THRESHOLD_RESPONSE_MS_CRIT,
                f"策略 {sid} 平均响应 {avg_ms}ms，严重超时",
                "建议降低 max_tokens（当前输出可能过长）或考虑启用 skip_ai 走模板快速回复",
            ))
        elif avg_ms >= THRESHOLD_RESPONSE_MS_WARN:
            advisories.append(Advisory(
                "warn", sid, "avg_response_ms", avg_ms, THRESHOLD_RESPONSE_MS_WARN,
                f"策略 {sid} 平均响应 {avg_ms}ms，偏慢",
                "可减小 max_tokens 或 context_rounds 以缩短响应时间",
            ))

        # ── 追问率检查 ──
        fur = s.get("follow_up_rate", 0)
        if fur >= THRESHOLD_FOLLOW_UP_CRIT:
            advisories.append(Advisory(
                "critical", sid, "follow_up_rate", fur, THRESHOLD_FOLLOW_UP_CRIT,
                f"策略 {sid} 追问率 {fur}%，用户频繁追问",
                "回复质量可能不足，建议升级为 deep_support 策略或增大 max_tokens + context_rounds",
            ))
        elif fur >= THRESHOLD_FOLLOW_UP_WARN:
            advisories.append(Advisory(
                "warn", sid, "follow_up_rate", fur, THRESHOLD_FOLLOW_UP_WARN,
                f"策略 {sid} 追问率 {fur}%，偏高",
                "考虑适当增加 context_rounds 让回复更贴合上下文",
            ))

    # ── 最佳 / 最差策略 ──
    best = max(scores, key=scores.get) if scores else None
    worst = min(scores, key=scores.get) if scores else None

    # ── 最差策略额外建议 ──
    if worst and best and worst != best and scores.get(worst, 0) < 40:
        w_summary = next((s for s in summary if s["strategy_id"] == worst), {})
        if w_summary.get("total", 0) >= MIN_SAMPLES:
            advisories.append(Advisory(
                "info", worst, "quality_score", scores[worst], 40,
                f"策略 {worst} 综合评分 {scores[worst]}，显著低于最佳策略 {best}({scores[best]})",
                f"建议参考 {best} 的参数配置优化 {worst}，或将其映射的意图切换到 {best}",
            ))

    advisories.sort(key=lambda a: {"critical": 0, "warn": 1, "info": 2}.get(a.level, 3))

    return {
        "scores": scores,
        "advisories": [a.to_dict() for a in advisories],
        "best": best,
        "worst": worst,
    }


# ── Auto-Pilot：自动切换 ──────────────────────────

def generate_auto_actions(
    summary: List[Dict],
    intent_strategy_map: Dict[str, str],
    strategies_config: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """检测持续低效策略，生成自动切换动作列表。

    每个动作: {"type": "remap_intent", "intent": str,
               "from_strategy": str, "to_strategy": str,
               "reason": str, "score_from": float, "score_to": float}
    """
    if not summary or not intent_strategy_map:
        return []

    scores: Dict[str, float] = {}
    totals: Dict[str, int] = {}
    for s in summary:
        sid = s["strategy_id"]
        scores[sid] = compute_quality_score(s)
        totals[sid] = s.get("total", 0)

    if not scores:
        return []

    best_sid = max(scores, key=scores.get)
    best_score = scores[best_sid]
    actions: List[Dict] = []

    for intent, mapped_sid in intent_strategy_map.items():
        cur_score = scores.get(mapped_sid)
        if cur_score is None:
            continue
        cur_total = totals.get(mapped_sid, 0)
        if cur_total < AUTO_MIN_SAMPLES:
            continue
        if cur_score >= AUTO_SCORE_THRESHOLD:
            continue
        if best_sid == mapped_sid:
            continue
        if best_score - cur_score < AUTO_SCORE_GAP:
            continue
        actions.append({
            "type": "remap_intent",
            "intent": intent,
            "from_strategy": mapped_sid,
            "to_strategy": best_sid,
            "reason": f"评分 {cur_score} 低于阈值 {AUTO_SCORE_THRESHOLD}，"
                      f"目标 {best_sid} 评分 {best_score}（差 {round(best_score - cur_score, 1)}）",
            "score_from": cur_score,
            "score_to": best_score,
        })

    return actions


# ── 参数微调建议 ──────────────────────────────────

def suggest_param_adjustments(
    summary: List[Dict],
    strategies_config: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """根据指标异常，生成带具体数值的参数调整建议（可一键应用）。

    返回: [{"strategy_id": str, "param": str, "current": val,
            "suggested": val, "reason": str}, ...]
    """
    suggestions: List[Dict] = []

    for s in summary:
        sid = s["strategy_id"]
        if s.get("total", 0) < MIN_SAMPLES:
            continue
        cfg = strategies_config.get(sid, {})
        if not cfg:
            continue

        sir = s.get("same_intent_rate", 0)
        if sir >= THRESHOLD_SAME_INTENT_WARN:
            cr = cfg.get("context_rounds", 5)
            new_cr = min(cr + 2, 15)
            if new_cr != cr:
                suggestions.append({
                    "strategy_id": sid, "param": "context_rounds",
                    "current": cr, "suggested": new_cr,
                    "reason": f"同意图追问率 {sir}%，增加上下文轮数可提升回复完整度",
                })
            mt = cfg.get("max_tokens", 512)
            new_mt = min(mt + 128, 2048)
            if new_mt != mt:
                suggestions.append({
                    "strategy_id": sid, "param": "max_tokens",
                    "current": mt, "suggested": new_mt,
                    "reason": f"同意图追问率 {sir}%，增加输出长度让 AI 给出更完整回答",
                })

        avg_ms = s.get("avg_ms", 0)
        if avg_ms >= THRESHOLD_RESPONSE_MS_WARN:
            mt = cfg.get("max_tokens", 512)
            new_mt = max(mt - 128, 64)
            if new_mt != mt:
                suggestions.append({
                    "strategy_id": sid, "param": "max_tokens",
                    "current": mt, "suggested": new_mt,
                    "reason": f"平均响应 {avg_ms}ms 偏慢，减少输出长度可缩短等待",
                })
            cr = cfg.get("context_rounds", 5)
            new_cr = max(cr - 1, 1)
            if new_cr != cr:
                suggestions.append({
                    "strategy_id": sid, "param": "context_rounds",
                    "current": cr, "suggested": new_cr,
                    "reason": f"平均响应 {avg_ms}ms 偏慢，减少上下文轮数可加速推理",
                })

        fur = s.get("follow_up_rate", 0)
        if fur >= THRESHOLD_FOLLOW_UP_WARN and sir < THRESHOLD_SAME_INTENT_WARN:
            temp = cfg.get("temperature", 0.7)
            new_temp = round(min(temp + 0.15, 1.2), 2)
            if new_temp != temp:
                suggestions.append({
                    "strategy_id": sid, "param": "temperature",
                    "current": temp, "suggested": new_temp,
                    "reason": f"追问率 {fur}% 但同意图率低，提高温度增加回复多样性",
                })

    return suggestions


# ── L3: A/B 测试自动评估 ──────────────────────────

AB_MIN_SAMPLES_PER_VARIANT = 30
AB_SIGNIFICANCE_GAP = 12.0  # 胜者评分需高于败者至少 12 分


def evaluate_ab_tests(
    ab_tests: Dict[str, Any],
    summary: List[Dict],
    strategies_config: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    评估所有活跃的 A/B 测试，返回结论列表。

    每项结论:
      {"intent": str, "status": "conclusive"|"inconclusive"|"insufficient",
       "winner": str|None, "loser": str|None,
       "scores": {sid: float}, "samples": {sid: int},
       "reason": str, "action": "promote"|"continue"|"wait"}
    """
    scores_map: Dict[str, float] = {}
    totals_map: Dict[str, int] = {}
    for s in summary:
        sid = s["strategy_id"]
        scores_map[sid] = compute_quality_score(s)
        totals_map[sid] = s.get("total", 0)

    results = []
    for intent, ab in ab_tests.items():
        if not ab.get("enabled") or not ab.get("variants"):
            continue

        variant_ids = [v.get("strategy_id") for v in ab["variants"] if v.get("strategy_id")]
        if len(variant_ids) < 2:
            continue

        variant_scores = {}
        variant_samples = {}
        all_sufficient = True
        for sid in variant_ids:
            variant_scores[sid] = scores_map.get(sid, 0)
            variant_samples[sid] = totals_map.get(sid, 0)
            if variant_samples[sid] < AB_MIN_SAMPLES_PER_VARIANT:
                all_sufficient = False

        if not all_sufficient:
            results.append({
                "intent": intent,
                "status": "insufficient",
                "winner": None, "loser": None,
                "scores": variant_scores, "samples": variant_samples,
                "reason": f"数据不足（需每组 ≥{AB_MIN_SAMPLES_PER_VARIANT} 条）",
                "action": "wait",
            })
            continue

        ranked = sorted(variant_scores.items(), key=lambda x: -x[1])
        best_sid, best_score = ranked[0]
        worst_sid, worst_score = ranked[-1]
        gap = best_score - worst_score

        if gap >= AB_SIGNIFICANCE_GAP:
            results.append({
                "intent": intent,
                "status": "conclusive",
                "winner": best_sid, "loser": worst_sid,
                "scores": variant_scores, "samples": variant_samples,
                "reason": f"{best_sid} 评分 {best_score:.0f} 显著优于 {worst_sid} 评分 {worst_score:.0f}（差距 {gap:.0f}）",
                "action": "promote",
            })
        else:
            results.append({
                "intent": intent,
                "status": "inconclusive",
                "winner": None, "loser": None,
                "scores": variant_scores, "samples": variant_samples,
                "reason": f"差距仅 {gap:.0f} 分，不足以判定（需 ≥{AB_SIGNIFICANCE_GAP}）",
                "action": "continue",
            })

    return results
