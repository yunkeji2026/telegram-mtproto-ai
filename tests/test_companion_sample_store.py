"""主动话题试发采样·评分回流存储：CompanionSampleStore。

覆盖：记录→评分→列表/筛选→stats 聚合（含按 mode + 好评率）→非法 rating 拒绝→
未知 id 评分返回 False→count。:memory: 模式零落盘。
"""
from src.integrations.companion_sample_store import (
    CompanionSampleStore,
    build_few_shot_block,
    build_tuning_advice,
    get_companion_sample_store,
)


def _store():
    return CompanionSampleStore(":memory:")


def test_record_and_get_id():
    s = _store()
    sid = s.record_sample(conversation_id="telegram:default:1", mode="follow_up",
                          fact="在备考", context_facts_n=2, silent_hours=48.0,
                          text="上次你说在备考，后来还顺利吗？")
    assert isinstance(sid, int) and sid > 0
    assert s.count() == 1


def test_rate_up_and_stats():
    s = _store()
    sid = s.record_sample(mode="follow_up", text="在么")
    assert s.rate(sid, "up", rated_by="alice") is True
    st = s.stats()
    assert st["total"] == 1 and st["rated"] == 1 and st["up"] == 1
    assert st["up_rate"] == 1.0
    assert st["by_mode"]["follow_up"]["up"] == 1


def test_rate_down_with_edit():
    s = _store()
    sid = s.record_sample(mode="gentle_checkin", text="好久不见")
    assert s.rate(sid, "down", edited_text="最近忙吗？想你了", note="太干") is True
    rows = s.list_recent(rating="down")
    assert len(rows) == 1
    assert rows[0]["edited_text"] == "最近忙吗？想你了"
    assert rows[0]["rating"] == "down"


def test_invalid_rating_rejected():
    s = _store()
    sid = s.record_sample(text="x")
    assert s.rate(sid, "meh") is False
    assert s.stats()["rated"] == 0


def test_rate_unknown_id_returns_false():
    s = _store()
    assert s.rate(99999, "up") is False


def test_up_rate_mixed():
    s = _store()
    for _ in range(3):
        s.rate(s.record_sample(mode="follow_up", text="a"), "up")
    s.rate(s.record_sample(mode="follow_up", text="b"), "down")
    st = s.stats()
    assert st["up"] == 3 and st["down"] == 1
    assert st["up_rate"] == 0.75


def test_list_unrated_filter():
    s = _store()
    s.record_sample(text="a")
    s.rate(s.record_sample(text="b"), "up")
    assert len(s.list_recent(rating="unrated")) == 1
    assert len(s.list_recent()) == 2


def test_singleton_reuse():
    a = get_companion_sample_store(":memory:")
    b = get_companion_sample_store(":memory:")
    assert a is b


# ── 调参建议（纯函数） ──────────────────────────────────────────────────

def test_advice_insufficient_samples():
    adv = build_tuning_advice({"rated": 2, "up_rate": 1.0, "by_mode": {}},
                              [], min_samples=5)
    assert adv["overall"]["verdict"] == "insufficient"
    assert "样本不足" in adv["suggestions"][0]


def test_advice_low_mode_gets_hints():
    stats = {
        "rated": 10, "up": 3, "down": 7, "up_rate": 0.3,
        "by_mode": {"follow_up": {"up": 3, "down": 7}},
    }
    adv = build_tuning_advice(stats, [], min_samples=5, low_up_rate=0.6)
    assert adv["overall"]["verdict"] == "low"
    fm = next(m for m in adv["by_mode"] if m["mode"] == "follow_up")
    assert fm["verdict"] == "low"
    assert fm["suggestions"]  # 命中针对性建议
    assert any("context_facts" in s for s in fm["suggestions"])


def test_advice_good_no_mode_hints():
    stats = {
        "rated": 10, "up": 9, "down": 1, "up_rate": 0.9,
        "by_mode": {"follow_up": {"up": 9, "down": 1}},
    }
    adv = build_tuning_advice(stats, [], min_samples=5)
    assert adv["overall"]["verdict"] == "good"
    fm = next(m for m in adv["by_mode"] if m["mode"] == "follow_up")
    assert fm["verdict"] == "good"
    assert fm["suggestions"] == []


def test_advice_few_shot_collects_liked_and_improved():
    rated = [
        {"rating": "up", "text": "上次你说在备考，后来顺利吗？", "mode": "follow_up"},
        {"rating": "down", "text": "好久不见", "edited_text": "最近忙吗？想你了",
         "mode": "gentle_checkin"},
        {"rating": "down", "text": "在么", "edited_text": "", "mode": "follow_up"},
    ]
    adv = build_tuning_advice({"rated": 3, "up_rate": 0.33, "by_mode": {}}, rated,
                              min_samples=1)
    assert adv["few_shot"]["liked"] == ["上次你说在备考，后来顺利吗？"]
    assert len(adv["few_shot"]["improved"]) == 1  # 仅带改写文案的差评入选
    assert adv["few_shot"]["improved"][0]["better"] == "最近忙吗？想你了"


# ── few-shot 风格示范块（纯函数） ──────────────────────────────────────

def test_few_shot_empty_when_no_samples():
    assert build_few_shot_block([]) == ""


def test_few_shot_prefers_improved_then_liked():
    rows = [
        {"rating": "up", "text": "高赞A"},
        {"rating": "down", "text": "差评原文", "edited_text": "改写更好版"},
        {"rating": "up", "text": "高赞B"},
    ]
    block = build_few_shot_block(rows, max_examples=3)
    assert "【风格示范】" in block
    # 改写版优先排前
    assert block.index("改写更好版") < block.index("高赞A")
    assert "不要照抄" in block


def test_few_shot_respects_max_and_dedup():
    rows = [
        {"rating": "up", "text": "同一句"},
        {"rating": "up", "text": "同一句"},
        {"rating": "up", "text": "另一句"},
        {"rating": "up", "text": "第三句"},
    ]
    block = build_few_shot_block(rows, max_examples=2)
    assert block.count("- ") == 2  # 去重 + 截断到 2


def test_few_shot_zero_max_disables():
    rows = [{"rating": "up", "text": "x"}]
    assert build_few_shot_block(rows, max_examples=0) == ""


def test_few_shot_skips_down_without_edit():
    rows = [{"rating": "down", "text": "差评", "edited_text": ""}]
    assert build_few_shot_block(rows) == ""  # 差评无改写 → 不入示范


def test_few_shot_filters_by_mode():
    rows = [
        {"rating": "up", "text": "回访口吻", "mode": "follow_up"},
        {"rating": "up", "text": "问候口吻", "mode": "gentle_checkin"},
    ]
    fu = build_few_shot_block(rows, mode="follow_up")
    assert "回访口吻" in fu and "问候口吻" not in fu
    gc = build_few_shot_block(rows, mode="gentle_checkin")
    assert "问候口吻" in gc and "回访口吻" not in gc


def test_few_shot_empty_when_mode_has_no_samples():
    rows = [{"rating": "up", "text": "x", "mode": "follow_up"}]
    assert build_few_shot_block(rows, mode="gentle_checkin") == ""


def test_few_shot_no_mode_uses_all():
    rows = [
        {"rating": "up", "text": "A", "mode": "follow_up"},
        {"rating": "up", "text": "B", "mode": "gentle_checkin"},
    ]
    block = build_few_shot_block(rows, max_examples=5)
    assert "A" in block and "B" in block  # 不传 mode → 全用


def test_advice_sorts_modes_worst_first():
    stats = {
        "rated": 20, "up": 12, "down": 8, "up_rate": 0.6,
        "by_mode": {
            "follow_up": {"up": 9, "down": 1},      # 0.9
            "gentle_checkin": {"up": 3, "down": 7}, # 0.3
        },
    }
    adv = build_tuning_advice(stats, [], min_samples=5)
    assert adv["by_mode"][0]["mode"] == "gentle_checkin"  # 最差排最前
