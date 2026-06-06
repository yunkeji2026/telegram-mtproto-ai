"""
Q1 — 对话摘要自动归档

提供纯函数 `generate_conv_summary`：
- 零 LLM 依赖（规则 + 字段拼接），亚毫秒级，可在 resolve 后同步调用
- 在 DraftService.resolve_with_audit approve/edit_send/autosend 后触发
- 存入 conversation_meta.summary，联系人画像 & 工作简报均可展示
- 若配置了 AI，提供可选的 `enrich_summary_async` 用 LLM 升级摘要质量

摘要格式（中文，面向主管阅读）：
    "{情绪} {意图} 咨询，{消息数}条消息交互，CSAT {评分}，
     由 {agent} {动作}处置，耗时约 {时长}。{关键词摘要}"
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

# ── 情绪/意图显示映射 ─────────────────────────────────────────────
_EMOTION_ZH: Dict[str, str] = {
    "angry":     "😡 愤怒",
    "frustrated":"😤 不满",
    "neutral":   "😐 平静",
    "happy":     "😊 满意",
    "grateful":  "🙏 感激",
    "anxious":   "😰 焦虑",
    "confused":  "😕 困惑",
}
_ACTION_ZH: Dict[str, str] = {
    "approve":    "批准",
    "edit_send":  "编辑后发送",
    "autosend":   "自动发送",
    "reject":     "拒绝",
    "force_override": "主管强制放行",
}
_LEVEL_ZH: Dict[str, str] = {
    "manual":   "人工",
    "review":   "审核",
    "auto_ai":  "AI自动",
    "L1": "人工", "L2": "审核", "L3": "AI自动", "L4": "高风险",
}
_RISK_ZH: Dict[str, str] = {
    "low": "低风险", "medium": "中风险", "high": "高风险", "critical": "极高风险",
}


def generate_conv_summary(
    *,
    conv_meta: Dict[str, Any],
    action: str,
    agent_id: str,
    sent_text: str = "",
    created_ts: float = 0.0,
    resolved_ts: Optional[float] = None,
) -> str:
    """Q1：根据对话元数据生成纯规则摘要字符串（中文，100-200字）。

    参数：
        conv_meta:    InboxStore.get_conv_meta 的返回值
        action:       approve / edit_send / autosend / reject / force_override
        agent_id:     处置坐席 ID
        sent_text:    实际发送文本（用于提取关键词）
        created_ts:   草稿创建时间戳（用于计算耗时）
        resolved_ts:  处置时间戳（默认 now）
    """
    now = resolved_ts or time.time()

    # 情绪 & 意图
    emotion = str(conv_meta.get("last_emotion") or "neutral")
    intent = str(conv_meta.get("last_intent") or "")
    emotion_zh = _EMOTION_ZH.get(emotion, emotion)
    intent_part = f" {intent}" if intent else ""

    # 消息数
    msg_count = int(conv_meta.get("msg_count") or 0)
    msg_part = f"，{msg_count} 条消息交互" if msg_count > 0 else ""

    # CSAT
    csat = conv_meta.get("csat_score")
    if csat is not None and float(csat) >= 0:
        stars = "⭐" * round(float(csat))
        csat_part = f"，CSAT {float(csat):.1f}{stars}"
    else:
        csat_part = ""

    # 处置动作
    action_zh = _ACTION_ZH.get(action, action)
    agent_part = f"由 {agent_id}" if agent_id else "系统"
    action_part = f"{agent_part} {action_zh}"

    # 耗时（草稿创建→处置）
    if created_ts and created_ts > 0:
        delta = max(0, now - created_ts)
        if delta < 60:
            time_part = f"，耗时 {int(delta)}s"
        elif delta < 3600:
            time_part = f"，耗时 {int(delta/60)}min"
        else:
            time_part = f"，耗时 {delta/3600:.1f}h"
    else:
        time_part = ""

    # 风险
    risk = str(conv_meta.get("last_risk") or "low")
    if risk in ("high", "critical"):
        risk_part = f"【{_RISK_ZH[risk]}】 "
    else:
        risk_part = ""

    # 回复关键词（取发送文本前 40 字）
    preview = (str(sent_text or "").strip())[:40]
    reply_part = (f"\u3002\u56de\u590d\u6458\u8981\uff1a\u201c{preview}\u2026\u201d" if preview else "")

    summary = (
        f"{risk_part}{emotion_zh}{intent_part} 咨询{msg_part}{csat_part}，"
        f"{action_part}{time_part}{reply_part}"
    )
    return summary


def enrich_summary_with_history(
    summary: str,
    intent_history: List[str],
    emotion_history: List[str],
) -> str:
    """在规则摘要基础上追加意图流转说明（仍零 LLM）。"""
    if len(intent_history) <= 1:
        return summary

    # 意图变化（去重相邻重复）
    deduped = []
    for x in intent_history:
        if not deduped or deduped[-1] != x:
            deduped.append(x)
    if len(deduped) > 1:
        flow = " → ".join(deduped[-3:])  # 最近 3 步
        summary += f"（意图流转：{flow}）"

    return summary
