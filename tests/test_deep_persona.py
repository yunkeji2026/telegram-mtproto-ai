"""深度人设 5 层纯核心门禁（确定性、可复现）。"""
from datetime import datetime

import pytest

from src.companion.deep_persona import (
    build_callback_opener,
    build_deep_persona_block,
    build_relationship_profile,
    detect_recurring_phrases,
    format_inside_jokes,
    format_relationship_profile,
    format_tastes,
    life_theme,
    maybe_imperfection_hint,
    pick_life_beat,
    rank_by_affect,
    temporal_anchor,
    to_experiential_recall,
)

NOW = datetime(2026, 7, 3, 22, 30)  # 周五晚上


# ── L1 生活线 ──────────────────────────────────────────────────────
def test_life_beat_deterministic_and_progresses():
    persona = {"id": "lin_xiaoyu", "life_arc": {"theme": "攒钱去大阪",
               "beats": ["便利店忙翻", "考完日语能力试", "拍了新vlog", "抹茶店打卡"]}}
    b1 = pick_life_beat(persona, datetime(2026, 7, 3))
    b1b = pick_life_beat(persona, datetime(2026, 7, 3))
    b2 = pick_life_beat(persona, datetime(2026, 7, 4))
    assert b1 in persona["life_arc"]["beats"]
    assert b1 == b1b               # 同一天稳定
    assert b2 in persona["life_arc"]["beats"]
    # 一周内应出现推进（不是永远同一条）
    days = {pick_life_beat(persona, datetime(2026, 7, d)) for d in range(3, 10)}
    assert len(days) >= 2


def test_life_beat_none_without_arc():
    assert pick_life_beat({"id": "x"}, NOW) is None
    assert pick_life_beat({}, NOW) is None


def test_life_beat_phase_stable_over_stride():
    persona = {"id": "p", "life_arc": {"beats": ["A", "B", "C", "D"], "stride_days": 3}}
    # 3 天为一相位窗，窗内稳定
    b3 = pick_life_beat(persona, datetime(2026, 7, 3))
    b4 = pick_life_beat(persona, datetime(2026, 7, 4))
    b5 = pick_life_beat(persona, datetime(2026, 7, 5))
    assert b3 == b4 == b5


def test_callback_probability_gate():
    loops = [{"topic": "换工作", "ts": "2026-07-01T10:00:00", "salience": 0.9}]
    # roll 未命中概率 → None
    assert build_callback_opener(loops, NOW, stage="intimate", roll=0.9,
                                 probability=0.35) is None
    # roll 命中 → 出
    assert build_callback_opener(loops, NOW, stage="intimate", roll=0.1,
                                 probability=0.35) is not None


def test_life_theme_and_format():
    persona = {"id": "a", "life_arc": {"theme": "攒钱去大阪", "beats": ["便利店忙"]}}
    assert life_theme(persona) == "攒钱去大阪"
    out = build_deep_persona_block(persona, now=NOW,
                                   cfg={"enabled": True, "life_line": True})
    assert "你最近的生活" in out and "便利店忙" in out


# ── L2 关系画像 + 回指 ─────────────────────────────────────────────
def test_relationship_profile_assembly_and_cap():
    p = build_relationship_profile(
        display_name="小明", stable_facts=["养了猫", "做设计", "喜欢露营"],
        dominant_emotion="低落", cares_about=["工作压力"], milestones=["一起追过某剧"],
        sensitive=["前任"], max_chars=200)
    assert "小明" in p and "养了猫" in p
    assert "低落" in p and "雷区" in p
    assert len(p) <= 200


def test_relationship_profile_empty():
    assert build_relationship_profile() == ""
    assert format_relationship_profile("") == ""


def test_callback_picks_salient_in_window():
    loops = [
        {"topic": "换工作的事", "ts": "2026-07-01T10:00:00", "salience": 0.9},
        {"topic": "买咖啡机", "ts": "2026-07-02T10:00:00", "salience": 0.2},
    ]
    out = build_callback_opener(loops, NOW, stage="intimate")
    assert out and "换工作的事" in out


def test_callback_suppressed_when_negative():
    loops = [{"topic": "换工作", "ts": "2026-07-01T10:00:00", "salience": 0.9}]
    assert build_callback_opener(loops, NOW, stage="intimate", suppress=True) is None


def test_callback_blocked_initial_stage():
    loops = [{"topic": "换工作", "ts": "2026-07-01T10:00:00", "salience": 0.9}]
    assert build_callback_opener(loops, NOW, stage="initial") is None


def test_callback_out_of_window():
    loops = [{"topic": "很久以前", "ts": "2026-01-01T10:00:00", "salience": 0.9}]
    assert build_callback_opener(loops, NOW, stage="intimate", max_days=30) is None


# ── L3 口味 + 内部梗 ───────────────────────────────────────────────
def test_format_tastes():
    persona = {"tastes": {"likes": ["抹茶"], "dislikes": ["香菜"],
                          "opinions": ["熬夜不值得"]}}
    out = format_tastes(persona)
    assert "抹茶" in out and "香菜" in out and "不必一味附和" in out


def test_format_tastes_empty():
    assert format_tastes({}) == ""
    assert format_tastes({"tastes": "x"}) == ""


def test_detect_recurring_phrases():
    msgs = ["我们去撸串吧", "今晚撸串不", "又想撸串了", "随便聊聊"]
    got = detect_recurring_phrases(msgs, min_count=3, top_k=5)
    assert "撸串" in got


def test_detect_recurring_ignores_rare():
    msgs = ["只说一次的词", "别的话题", "再换一个"]
    assert detect_recurring_phrases(msgs, min_count=3) == []


def test_inside_jokes_format():
    assert format_inside_jokes([]) == ""
    out = format_inside_jokes(["撸串", "你的经典借口"])
    assert "撸串" in out and "默契" in out


# ── L4 经历式记忆 ──────────────────────────────────────────────────
def test_rank_by_affect_desc():
    ev = [{"what": "A", "salience": 0.2}, {"what": "B", "salience": 0.9},
          {"what": "C"}]
    r = rank_by_affect(ev)
    assert [e["what"] for e in r] == ["B", "A", "C"]


def test_experiential_recall_narrative():
    ev = [{"what": "狗狗Max在公园跑丢又找回", "emotion": "焦虑", "when": "上个月",
           "salience": 0.9}]
    out = to_experiential_recall(ev)
    assert "Max" in out and "焦虑" in out and "叙事式回指" in out


def test_experiential_empty():
    assert to_experiential_recall([]) == ""


# ── C1 情感×时近×相关 加权召回 ────────────────────────────────────
def test_select_experiential_relevance_boost():
    from src.companion.deep_persona import select_experiential
    ev = [
        {"what": "上次聊到露营装备", "salience": 0.4, "ts": "2026-07-01T10:00:00"},
        {"what": "工作压力那次崩溃", "salience": 0.9, "ts": "2026-01-01T10:00:00"},
    ]
    # 当前话题是露营 → 相关性 + 时近让低 salience 的露营经历浮上来
    top = select_experiential(ev, now=NOW, query_text="周末想去露营", top_k=1)
    assert top and "露营" in top[0]["what"]


def test_select_experiential_no_query_falls_back_affect():
    from src.companion.deep_persona import select_experiential
    ev = [{"what": "A", "salience": 0.2, "ts": "2026-07-02T10:00:00"},
          {"what": "B", "salience": 0.95, "ts": "2026-07-02T10:00:00"}]
    top = select_experiential(ev, now=NOW, query_text="", top_k=1)
    assert top[0]["what"] == "B"


# ── C2 生活线主动分享 opener ───────────────────────────────────────
def test_life_beat_opener_shares_beat():
    from src.companion.deep_persona import build_life_beat_opener
    persona = {"id": "lin", "life_arc": {"beats": ["便利店忙翻了"]}}
    op = build_life_beat_opener(persona, NOW)
    assert op.get("mode") == "life_share"
    assert "便利店忙翻了" in op.get("fact", "")
    assert op.get("directive")


def test_life_beat_opener_blocked_on_crisis():
    from src.companion.deep_persona import build_life_beat_opener
    persona = {"id": "lin", "life_arc": {"beats": ["便利店忙"]}}
    assert build_life_beat_opener(persona, NOW, gate="block") == {}


def test_life_beat_opener_no_arc():
    from src.companion.deep_persona import build_life_beat_opener
    assert build_life_beat_opener({"id": "x"}, NOW) == {}


# ── D2 反打扰节奏 ───────────────────────────────────────────────────
def test_life_share_allowed_empty_history():
    from src.companion.deep_persona import life_share_allowed
    assert life_share_allowed([], NOW) is True


def test_life_share_blocked_by_min_gap():
    from src.companion.deep_persona import life_share_allowed
    recent = [NOW.timestamp() - 3600]  # 1 小时前分享过
    assert life_share_allowed(recent, NOW, min_gap_hours=48) is False


def test_life_share_blocked_by_weekly_cap():
    from src.companion.deep_persona import life_share_allowed
    base = NOW.timestamp()
    recent = [base - 3 * 86400, base - 5 * 86400]  # 一周内 2 次
    assert life_share_allowed(recent, NOW, max_per_week=2, min_gap_hours=1) is False


def test_life_share_allowed_after_gap_and_under_cap():
    from src.companion.deep_persona import life_share_allowed
    recent = [NOW.timestamp() - 5 * 86400]  # 5 天前一次
    assert life_share_allowed(recent, NOW, max_per_week=2, min_gap_hours=48) is True


# ── D3 语义 sim_fn 注入缝 ───────────────────────────────────────────
def test_select_experiential_custom_sim_fn():
    from src.companion.deep_persona import select_experiential
    ev = [{"what": "毫不相关的字面", "salience": 0.3, "ts": "2026-07-02T10:00:00"},
          {"what": "另一件事", "salience": 0.3, "ts": "2026-07-02T10:00:00"}]
    # 注入一个"语义"sim：只对第一条给高分
    def _sim(q, t):
        return 1.0 if "毫不相关" in t else 0.0
    top = select_experiential(ev, now=NOW, query_text="任意", top_k=1, sim_fn=_sim)
    assert top[0]["what"] == "毫不相关的字面"


def test_select_experiential_bad_sim_fn_falls_back():
    from src.companion.deep_persona import select_experiential
    def _boom(q, t):
        raise RuntimeError("x")
    ev = [{"what": "露营", "salience": 0.5, "ts": "2026-07-02T10:00:00"}]
    # sim_fn 抛错 → 回落字面，不崩
    top = select_experiential(ev, now=NOW, query_text="露营", top_k=1, sim_fn=_boom)
    assert top and top[0]["what"] == "露营"


# ── D1 LLM 画像精修守卫 ─────────────────────────────────────────────
def test_refine_profile_llm_accepts_clean():
    from src.companion.deep_persona import refine_profile_llm
    persona = {"boundaries": {"topics_to_avoid": ["政治"]}}
    out = refine_profile_llm("关于TA：喜欢露营", persona,
                             llm_fn=lambda p: "TA 是个热爱露营的人")
    assert out == "TA 是个热爱露营的人"


def test_refine_profile_llm_rejects_drift():
    from src.companion.deep_persona import refine_profile_llm
    persona = {"boundaries": {"topics_to_avoid": ["政治"]}}
    base = "关于TA：喜欢露营"
    # 润色结果混入禁忌 → 回落原文
    out = refine_profile_llm(base, persona, llm_fn=lambda p: "TA 爱聊政治")
    assert out == base


def test_refine_profile_llm_no_fn_or_empty():
    from src.companion.deep_persona import refine_profile_llm
    assert refine_profile_llm("画像", {}) == "画像"       # 无 llm_fn
    assert refine_profile_llm("画像", {}, llm_fn=lambda p: "") == "画像"  # 空输出回落


# ── E5 分享时段闸 ───────────────────────────────────────────────────
def test_life_share_time_quiet_hours():
    from src.companion.deep_persona import life_share_time_ok
    from datetime import datetime as _dt
    assert life_share_time_ok(_dt(2026, 7, 3, 3, 0)) is False   # 深夜 3 点静默
    assert life_share_time_ok(_dt(2026, 7, 3, 14, 0)) is True   # 下午允许
    assert life_share_time_ok(_dt(2026, 7, 3, 8, 0)) is True    # 8 点(不含起点边界)允许


def test_life_share_time_cross_midnight():
    from src.companion.deep_persona import life_share_time_ok
    from datetime import datetime as _dt
    # 静默 22-6（跨午夜）
    assert life_share_time_ok(_dt(2026, 7, 3, 23, 0),
                              quiet_start_hour=22, quiet_end_hour=6) is False
    assert life_share_time_ok(_dt(2026, 7, 3, 12, 0),
                              quiet_start_hour=22, quiet_end_hour=6) is True


# ── E1 余弦 + 向量 sim_fn ───────────────────────────────────────────
def test_cosine_sim():
    from src.companion.deep_persona import cosine_sim
    assert cosine_sim([1, 0], [1, 0]) == pytest.approx(1.0)
    assert cosine_sim([1, 0], [0, 1]) == pytest.approx(0.0)
    assert cosine_sim([1, 0], []) == 0.0
    assert cosine_sim([0, 0], [1, 1]) == 0.0


def test_embedding_sim_fn_ranks_by_vector():
    from src.companion.deep_persona import make_embedding_sim_fn, select_experiential
    emap = {"露营那次": [1.0, 0.0], "工作崩溃": [0.0, 1.0]}
    sim = make_embedding_sim_fn([1.0, 0.0], emap)  # query 向量偏"露营"
    ev = [{"what": "露营那次", "salience": 0.3, "ts": "2026-07-02T10:00:00"},
          {"what": "工作崩溃", "salience": 0.5, "ts": "2026-07-02T10:00:00"}]
    top = select_experiential(ev, now=NOW, query_text="q", top_k=1, sim_fn=sim)
    assert top[0]["what"] == "露营那次"  # 语义相关压过略高 salience


def test_embedding_sim_fn_missing_emb_zero():
    from src.companion.deep_persona import make_embedding_sim_fn
    sim = make_embedding_sim_fn([1.0, 0.0], {"有向量的": [1.0, 0.0]})
    assert sim("q", "没向量的事件") == 0.0


# ── L5 拟人细节 ────────────────────────────────────────────────────
def test_temporal_anchor_night_weekend():
    out = temporal_anchor(datetime(2026, 7, 3, 23, 30))  # 周五深夜
    assert "深夜" in out and "很晚" in out


def test_temporal_anchor_monday_morning():
    out = temporal_anchor(datetime(2026, 7, 6, 9, 0))  # 周一上午
    assert "周一" in out


def test_imperfection_gated_and_probabilistic():
    # 未熟阶段不出
    assert maybe_imperfection_hint(enabled=True, stage="warming", roll=0.0) == ""
    # 熟 + roll 命中 → 出
    assert maybe_imperfection_hint(enabled=True, stage="intimate", roll=0.05) != ""
    # 熟 + roll 未命中 → 不出
    assert maybe_imperfection_hint(enabled=True, stage="intimate", roll=0.9) == ""
    # 关 → 不出
    assert maybe_imperfection_hint(enabled=False, stage="intimate", roll=0.0) == ""


# ── 总装配开关 ─────────────────────────────────────────────────────
def test_master_disabled_returns_empty():
    persona = {"id": "a", "life_arc": ["x"], "tastes": {"likes": ["y"]}}
    assert build_deep_persona_block(persona, now=NOW, cfg={"enabled": False,
                                    "life_line": True, "tastes": True}) == ""
    assert build_deep_persona_block(persona, now=NOW, cfg={}) == ""


def test_full_assembly_layers_composed():
    persona = {"id": "lin", "life_arc": {"theme": "T", "beats": ["便利店忙"]},
               "tastes": {"likes": ["抹茶"]}}
    ctx = {
        "relationship_profile": "关于TA：养猫",
        "inside_jokes": ["撸串"],
        "experiential_events": [{"what": "露营那次", "emotion": "开心", "salience": 0.8}],
        "open_loops": [{"topic": "换工作", "ts": "2026-07-01T10:00:00", "salience": 0.9}],
        "suppress_callbacks": False,
    }
    cfg = {"enabled": True, "life_line": True, "tastes": True, "relationship": True,
           "inside_jokes": True, "experiential": True, "texture": True}
    out = build_deep_persona_block(persona, now=NOW, cfg=cfg, stage="intimate",
                                   deep_ctx=ctx, imperfection_roll=0.9)
    for needle in ["深度人设增强", "便利店忙", "抹茶", "养猫", "撸串", "露营那次",
                   "换工作", "此刻"]:
        assert needle in out, needle


def test_assembler_suppresses_callback_on_crisis():
    """安全不变量：对方脆弱（危机）时，装配块绝不含"不问就回指"往事。"""
    persona = {"id": "lin"}
    ctx = {"open_loops": [{"topic": "换工作", "ts": "2026-07-01T10:00:00",
                           "salience": 0.9}], "suppress_callbacks": True,
           "callback_roll": 0.0}
    cfg = {"enabled": True, "relationship": True}
    out = build_deep_persona_block(persona, now=NOW, cfg=cfg, stage="intimate",
                                   deep_ctx=ctx)
    assert "后来怎么样了" not in out


def test_assembler_imperfection_gated_by_stage():
    """拟人小瑕疵只在熟络阶段出，即使概率命中。"""
    persona = {"id": "lin"}
    cfg = {"enabled": True, "texture": True, "imperfection_probability": 1.0}
    out_new = build_deep_persona_block(persona, now=NOW, cfg=cfg, stage="warming",
                                       deep_ctx={}, imperfection_roll=0.0)
    assert "真人小瑕疵" not in out_new
    out_close = build_deep_persona_block(persona, now=NOW, cfg=cfg, stage="steady",
                                         deep_ctx={}, imperfection_roll=0.0)
    assert "真人小瑕疵" in out_close
