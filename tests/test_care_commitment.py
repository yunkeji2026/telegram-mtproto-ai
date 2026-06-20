"""Phase O1：主动关怀约定抽取层单测。

固定 now = 2026-06-17（周三）10:00，确定性断言。覆盖：相对日/周几/下周/绝对日期/
X天后/月底/英文锚点/生日道贺时刻/无锚点空列表/过去事件过滤/置信度/情绪极性。
"""
from datetime import datetime

from src.contacts.care_commitment import CareCommitment, extract_commitments

# 周三
NOW = datetime(2026, 6, 17, 10, 0, 0).timestamp()
NOW_LATE = datetime(2026, 6, 17, 22, 0, 0).timestamp()


def _one(text, now=NOW):
    out = extract_commitments(text, now=now)
    assert len(out) == 1, f"expected 1 commitment, got {out}"
    return out[0]


def _due_date(c: CareCommitment):
    d = datetime.fromtimestamp(c.due_at)
    return (d.year, d.month, d.day, d.hour)


def test_tomorrow():
    c = _one("明天面试，好紧张")
    assert _due_date(c) == (2026, 6, 18, 20)
    assert c.topic == "面试"
    assert c.confidence == 0.85


def test_this_week_weekday():
    c = _one("周五要去复查")
    assert _due_date(c) == (2026, 6, 19, 20)  # 周三→周五 +2
    assert c.topic == "复查"


def test_next_week_weekday():
    c = _one("下周五有个面试")
    # 下周一 = 06-22，+4 = 06-26（周五）
    assert _due_date(c) == (2026, 6, 26, 20)


def test_next_next_week():
    c = _one("下下周二考试")
    # 下周一 06-22 +7 = 06-29，+1 = 06-30（周二）
    assert _due_date(c) == (2026, 6, 30, 20)


def test_absolute_date_future():
    c = _one("12月31日跨年")
    assert _due_date(c) == (2026, 12, 31, 20)


def test_absolute_date_past_rolls_next_year():
    c = _one("3月5日体检")
    assert _due_date(c) == (2027, 3, 5, 20)  # 今年3月已过 → 明年
    assert c.topic == "体检"


def test_days_later():
    c = _one("3天后考试")
    assert _due_date(c) == (2026, 6, 20, 20)
    assert c.topic == "考试"


def test_weeks_later():
    c = _one("2周后出差")
    assert _due_date(c) == (2026, 7, 1, 20)  # +14 天
    assert c.topic == "出差"


def test_month_end():
    c = _one("月底搬家")
    assert _due_date(c) == (2026, 6, 30, 20)
    assert c.topic == "搬家"


def test_birthday_greeting_hour():
    c = _one("下周三是我生日哦")
    # 生日道贺当日早上 9 点
    assert _due_date(c) == (2026, 6, 24, 9)
    assert c.topic == "生日"


def test_english_anchor():
    c = _one("I have an interview next friday")
    assert _due_date(c) == (2026, 6, 26, 20)
    assert c.topic == "interview"


def test_no_anchor_returns_empty():
    assert extract_commitments("今天好累啊，不想说话", now=NOW) == []
    assert extract_commitments("", now=NOW) == []
    assert extract_commitments("随便聊聊天气", now=NOW) == []


def test_past_event_filtered():
    # now 已是周三 22:00，"周三"事件 due 今天 20:00 < now → 过滤
    assert extract_commitments("周三开会", now=NOW_LATE) == []


def test_topicless_anchor_lower_confidence():
    c = _one("明天那个事记得")
    assert c.confidence == 0.5
    assert c.anchor_text == "明天"


def test_sentiment_values():
    c = _one("明天面试，好紧张好害怕")
    assert c.sentiment in ("positive", "negative", "neutral")
    d = c.as_dict()
    assert set(["due_at", "event_at", "topic", "sentiment", "anchor_text",
                "source_text", "confidence"]).issubset(d.keys())


def test_event_at_is_day_aligned():
    c = _one("明天面试")
    ev = datetime.fromtimestamp(c.event_at)
    assert (ev.hour, ev.minute, ev.second) == (0, 0, 0)
