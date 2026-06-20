"""Phase O1：主动关怀「约定/事件」抽取层（纯函数）。

从一条消息文本里识别**未来时间锚点 + 事件主题 + 情绪极性**，产出 `CareCommitment`，
供 O2 落「到期主动关怀」队列、O3 到点生成引用具体事的主动消息。

设计纪律（与 reactivation / outreach_planner 一致）：
- **纯函数**：不触网、不调 LLM（情绪复用既有 `analyze_emotion`，失败软降级）、可单测。
- **只认未来**：解析出的事件时刻 ≤ now 即丢弃（不为已过去的事排跟进）。
- **宁缺毋滥**：只在出现明确时间锚点时产出；找不到主题就用锚点附近的短摘要兜底。
- **跟进时刻**：默认事件日**当晚 20:00**主动关心（"今天那事怎么样了"）；生日类当日 09:00 道贺。

仅做中文为主 + 少量英文锚点（出海社交主力语种）；不追求 NLP 完备，规则确定可控。
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional

logger = logging.getLogger(__name__)

_DAY = 86400.0
# 默认跟进时刻：事件当日的钟点（晚上 20 点回访"怎么样了"）
_FOLLOWUP_HOUR = 20
_BIRTHDAY_HOUR = 9  # 道贺类当日早上 9 点

# 事件主题词典（命中即作 topic；按出海私域陪聊高频场景）
_TOPIC_LEXICON = [
    "面试", "笔试", "考试", "考研", "高考", "答辩", "复试",
    "复查", "体检", "检查", "手术", "看病", "就诊", "产检",
    "出差", "旅行", "旅游", "出游", "搬家", "入职", "离职", "转正",
    "比赛", "演出", "表演", "演讲", "汇报", "述职", "路演",
    "约会", "相亲", "见家长", "领证", "婚礼", "纪念日", "生日",
    "签约", "谈判", "开庭", "面签", "返校", "开学", "毕业",
    "interview", "exam", "surgery", "trip", "birthday", "meeting",
]
# 道贺类（当日早上而非晚上回访）
_GREETING_TOPICS = {"生日", "纪念日", "婚礼", "领证", "毕业", "birthday"}

# 周几 → Monday=0..Sunday=6
_WEEKDAY = {
    "一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6,
    "1": 0, "2": 1, "3": 2, "4": 3, "5": 4, "6": 5, "7": 6,
}
_WEEKDAY_EN = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


@dataclass
class CareCommitment:
    due_at: float          # 跟进时刻（epoch 秒）
    event_at: float        # 事件本身的时刻（epoch 秒，当日 0 点对齐）
    topic: str             # 事件主题（"面试"/"复查"/"生日" 或锚点附近摘要）
    sentiment: str         # "positive" | "negative" | "neutral"
    anchor_text: str       # 命中的时间短语（"周五"/"明天"/"3月5日"）
    source_text: str       # 原消息（截断）
    confidence: float      # 0..1

    def as_dict(self) -> dict:
        return {
            "due_at": self.due_at, "event_at": self.event_at, "topic": self.topic,
            "sentiment": self.sentiment, "anchor_text": self.anchor_text,
            "source_text": self.source_text, "confidence": round(self.confidence, 3),
        }


def _start_of_day(dt: datetime) -> datetime:
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def _sentiment_of(text: str) -> str:
    """复用既有情绪分析取极性；失败 → neutral（best-effort，绝不抛）。"""
    try:
        from src.utils.emotional_context import analyze_emotion
        v = float(analyze_emotion(text).get("valence", 0.0) or 0.0)
    except Exception:
        return "neutral"
    if v >= 0.25:
        return "positive"
    if v <= -0.25:
        return "negative"
    return "neutral"


def _resolve_event_date(text: str, base: datetime) -> Optional[tuple]:
    """返回 (event_day_datetime(0点对齐), anchor_text) 或 None。只认未来日期。

    解析顺序：绝对日期 → 周几（含下周/这周）→ 相对日（明天/后天…）→ X天/周后。
    """
    today = _start_of_day(base)

    # 1) 绝对：M月D日/号
    m = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]", text)
    if m:
        mo, da = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12 and 1 <= da <= 31:
            year = base.year
            try:
                cand = today.replace(month=mo, day=da)
            except ValueError:
                cand = None
            if cand is not None:
                if cand < today:  # 今年已过 → 明年
                    try:
                        cand = cand.replace(year=year + 1)
                    except ValueError:
                        cand = None
                if cand is not None:
                    return (cand, m.group(0))

    # 2) 绝对：MM/DD 或 MM-DD（避免误吞比分/比例：要求像日期）
    m = re.search(r"(?<!\d)(\d{1,2})[/\-](\d{1,2})(?!\d)", text)
    if m:
        mo, da = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12 and 1 <= da <= 31:
            try:
                cand = today.replace(month=mo, day=da)
                if cand < today:
                    cand = cand.replace(year=base.year + 1)
                return (cand, m.group(0))
            except ValueError:
                pass

    # 3) 周几（下下周X / 下周X / 这周X / 本周X / 周X / 星期X / 礼拜X）
    m = re.search(r"(下下|下|这|本)?\s*(周|星期|礼拜)\s*([一二三四五六日天1-7])", text)
    if m:
        prefix, wd = (m.group(1) or ""), m.group(3)
        target_wd = _WEEKDAY.get(wd)
        if target_wd is not None:
            cur_wd = today.weekday()
            if prefix in ("下", "下下"):
                # 到下一个自然周的周一，再加目标周几（下下再 +7）
                days_to_next_monday = 7 - cur_wd if cur_wd > 0 else 7
                delta = days_to_next_monday + target_wd + (7 if prefix == "下下" else 0)
            else:
                # 本周/这周/裸"周X"：本周内该周几；今天即该周几则视为今天（due 未来性后置过滤）
                delta = (target_wd - cur_wd) % 7
            return (today + timedelta(days=delta), m.group(0).strip())

    # 4) 英文周几（next friday / friday）
    m = re.search(r"\b(next\s+)?(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
                  text, re.IGNORECASE)
    if m:
        nxt, wd = m.group(1), m.group(2).lower()
        target_wd = _WEEKDAY_EN[wd]
        cur_wd = today.weekday()
        delta = (target_wd - cur_wd) % 7
        if nxt:
            delta = ((target_wd - cur_wd) % 7) + 7
        elif delta == 0:
            delta = 7
        return (today + timedelta(days=delta), m.group(0).strip())

    # 5) 相对日
    rel = [
        ("大后天", 3), ("后天", 2), ("明天", 1), ("明晚", 1), ("明早", 1),
        ("tomorrow", 1),
    ]
    for kw, days in rel:
        if kw in text.lower() if kw == "tomorrow" else kw in text:
            return (today + timedelta(days=days), kw)

    # 6) X天后 / X周后 / X个星期后
    m = re.search(r"(\d{1,3})\s*天后", text)
    if m:
        return (today + timedelta(days=int(m.group(1))), m.group(0))
    m = re.search(r"(\d{1,2})\s*(周|个?星期|个?礼拜)后", text)
    if m:
        return (today + timedelta(days=int(m.group(1)) * 7), m.group(0))

    # 7) 月底 / 周末 / 下周末
    if "月底" in text:
        # 下月 1 号 - 1 天
        if today.month == 12:
            eom = today.replace(year=today.year + 1, month=1, day=1) - timedelta(days=1)
        else:
            eom = today.replace(month=today.month + 1, day=1) - timedelta(days=1)
        if eom >= today:
            return (eom, "月底")
    if "周末" in text or "weekend" in text.lower():
        # 最近的周六
        cur_wd = today.weekday()
        delta = (5 - cur_wd) % 7
        if "下" in text and "周末" in text:
            delta = ((5 - cur_wd) % 7) + 7
        elif delta == 0:
            delta = 7
        return (today + timedelta(days=delta), "周末")

    return None


def _pick_topic(text: str) -> Optional[str]:
    low = text.lower()
    for kw in _TOPIC_LEXICON:
        if kw.isascii():
            if re.search(r"\b" + re.escape(kw) + r"\b", low):
                return kw
        elif kw in text:
            return kw
    return None


def extract_commitments(
    text: str,
    *,
    now: Optional[float] = None,
    max_snippet: int = 160,
) -> List[CareCommitment]:
    """从一条消息抽取关怀约定（通常 0 或 1 条）。无时间锚点 → 空列表。"""
    t = (text or "").strip()
    if not t:
        return []
    now_ts = float(now if now is not None else time.time())
    base = datetime.fromtimestamp(now_ts)

    resolved = _resolve_event_date(t, base)
    if resolved is None:
        return []
    event_day, anchor = resolved

    topic = _pick_topic(t)
    is_greeting = topic in _GREETING_TOPICS if topic else False
    hour = _BIRTHDAY_HOUR if is_greeting else _FOLLOWUP_HOUR
    due_dt = event_day.replace(hour=hour)
    due_at = due_dt.timestamp()
    event_at = event_day.timestamp()

    # 只认未来：跟进时刻必须在 now 之后（已过去的事不排跟进）
    if due_at <= now_ts:
        return []

    if not topic:
        # 无主题词 → 用锚点附近摘要兜底，置信度降一档
        topic = t[:40].strip()
        confidence = 0.5
    else:
        confidence = 0.85

    return [CareCommitment(
        due_at=due_at,
        event_at=event_at,
        topic=topic,
        sentiment=_sentiment_of(t),
        anchor_text=anchor,
        source_text=t[:max_snippet],
        confidence=confidence,
    )]


__all__ = ["CareCommitment", "extract_commitments"]
