"""P35 — 客户流失预警模型（ChurnPredictor）。

纯规则评分，基于可观测信号：
  - silence_days：最近无活动天数（最强信号）
  - last_msg_risk：末条入站消息含投诉/流失关键词
  - qa_score_low：会话质检评分低（说明服务体验差）
  - complaint_count：检测到的投诉类信号数

风险分级：
  high   ≥ 70分 → 立即创建跟进任务
  medium 40-69  → 标注提醒，人工判断
  low    < 40   → 正常，不干预
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

# ── 关键词（轻量内联，不依赖 P30 模块） ───────────────────────────────────────
_COMPLAINT_KW = [
    "投诉", "举报", "太差", "垃圾", "不好用", "骗子", "退款", "退货",
    "complaint", "terrible", "awful", "scam", "refund", "return",
]
_CHURN_KW = [
    "不买了", "取消", "算了", "不想要了", "找别家", "转店",
    "cancel", "quit", "unsubscribe", "switch",
]
_THREAT_KW = [
    "报警", "律师", "起诉", "消协", "媒体", "曝光",
    "sue", "lawyer", "report", "media",
]


class ChurnPredictor:
    """P35：客户流失风险评估器。"""

    def predict(
        self,
        conversation_id: str,
        *,
        last_ts: float = 0.0,
        last_msg_text: str = "",
        last_msg_direction: str = "in",
        qa_score: int = -1,          # -1 = 未计算
        silence_threshold_days: int = 7,
        now: Optional[float] = None,
    ) -> Tuple[str, int, List[str]]:
        """评估单次会话的流失风险。

        Returns:
            (risk_level: 'high'|'medium'|'low', risk_score: int, reasons: List[str])
        """
        now = now or time.time()
        score = 0
        reasons: List[str] = []

        # 信号 1: 沉默时间（最强权重）
        silence_sec = max(0, now - float(last_ts or now))
        silence_days = silence_sec / 86400
        if silence_days >= silence_threshold_days * 2:
            score += 40
            reasons.append(f"超长沉默 {int(silence_days)} 天未互动")
        elif silence_days >= silence_threshold_days:
            score += 25
            reasons.append(f"沉默 {int(silence_days)} 天无回应")

        # 信号 2: 末条消息为客户且含风险词
        text_lc = last_msg_text.lower() if last_msg_text else ""
        if last_msg_direction in ("in", "inbound") and text_lc:
            complaint_hit = [kw for kw in _COMPLAINT_KW if kw in text_lc]
            churn_hit = [kw for kw in _CHURN_KW if kw in text_lc]
            threat_hit = [kw for kw in _THREAT_KW if kw in text_lc]

            if threat_hit:
                score += 35
                reasons.append(f"末消息含法律/媒体威胁：{threat_hit[:2]}")
            if churn_hit:
                score += 25
                reasons.append(f"末消息含放弃购买意图：{churn_hit[:2]}")
            if complaint_hit:
                score += 15
                reasons.append(f"末消息含投诉词：{complaint_hit[:2]}")

        # 信号 3: QA 质检评分低
        if 0 <= qa_score < 50:
            score += 20
            reasons.append(f"会话质检评分低（{qa_score}/100），服务体验差")
        elif 0 <= qa_score < 65:
            score += 10
            reasons.append(f"会话质检评分偏低（{qa_score}/100）")

        # 信号 4: 坐席未回复（末条为入站且沉默超过阈值）
        if last_msg_direction in ("in", "inbound") and silence_days >= 1:
            score += 15
            reasons.append("客户最后发言后坐席超 1 天未回复")

        score = min(100, score)
        if score >= 70:
            risk_level = "high"
        elif score >= 40:
            risk_level = "medium"
        else:
            risk_level = "low"

        return risk_level, score, reasons

    def batch_predict(
        self,
        conversations: List[Dict[str, Any]],
        *,
        silence_threshold_days: int = 7,
        now: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """批量评估，返回排序后的流失风险列表（score 从高到低）。

        Args:
            conversations: list of {conversation_id, last_ts, last_text,
                                     last_dir, qa_score, display_name, platform}
        """
        now = now or time.time()
        results = []
        for c in conversations:
            cid = str(c.get("conversation_id") or "")
            qa_raw = c.get("qa_score") or ""
            qa_score = -1
            if qa_raw and qa_raw not in ("{}", ""):
                try:
                    import json
                    qa_score = int(json.loads(qa_raw).get("score") or -1)
                except Exception:
                    pass

            level, score, reasons = self.predict(
                cid,
                last_ts=float(c.get("last_ts") or 0),
                last_msg_text=str(c.get("last_text") or ""),
                last_msg_direction=str(c.get("last_dir") or "in"),
                qa_score=qa_score,
                silence_threshold_days=silence_threshold_days,
                now=now,
            )
            if level == "low":
                continue  # 低风险不返回，减少噪音
            results.append({
                "conversation_id": cid,
                "display_name": c.get("display_name", ""),
                "platform": c.get("platform", ""),
                "contact_id": c.get("contact_id", ""),
                "claimed_by": c.get("claimed_by", ""),
                "last_ts": c.get("last_ts", 0),
                "risk_level": level,
                "risk_score": score,
                "reasons": reasons,
            })

        results.sort(key=lambda x: x["risk_score"], reverse=True)
        return results
