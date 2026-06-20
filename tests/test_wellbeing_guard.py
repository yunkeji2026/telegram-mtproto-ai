"""R4 wellbeing / 反谄媚守卫（情感陪聊安全底线）。

覆盖：危机分级识别、惯用语零误伤、安全指令组装（危机优先 + 热线 + 反谄媚常驻）、
开关行为，以及与 emotional_context 的集成（危机块置顶 + 标记 user_context）。
"""

from __future__ import annotations

from src.utils.wellbeing_guard import build_wellbeing_block, detect_crisis


# ── 危机分级识别 ────────────────────────────────────────────────────────

def test_detect_severe_self_harm():
    for msg in [
        "我不想活了",
        "活着没意思，不如死了算了",
        "我想自杀",
        "I want to die",
        "想一了百了，了结这一切",
    ]:
        sig = detect_crisis(msg)
        assert sig["level"] == "severe", msg
        assert sig["category"] == "self_harm"


def test_detect_elevated_despair():
    for msg in [
        "我真的快撑不下去了",
        "好绝望，看不到希望",
        "没有人在乎我",
        "我觉得我就是个废物",
    ]:
        sig = detect_crisis(msg)
        assert sig["level"] == "elevated", msg
        assert sig["category"] == "despair"


def test_idioms_not_flagged():
    # 日常夸张/亲昵，绝不能误判为危机
    for msg in [
        "今天累死了",
        "笑死我了哈哈哈",
        "好饿啊饿死了",
        "想死你了宝贝",
        "这个deadline要了我的命",
        "热死了快开空调",
    ]:
        assert detect_crisis(msg)["level"] == "none", msg


def test_empty_and_neutral():
    assert detect_crisis("")["level"] == "none"
    assert detect_crisis("   ")["level"] == "none"
    assert detect_crisis("今天天气不错我们去喝奶茶吧")["level"] == "none"


def test_severe_precedence_over_despair():
    # 同时含绝望 + 自伤 → 取 severe
    sig = detect_crisis("我好绝望，真的不想活了")
    assert sig["level"] == "severe"


# ── 安全指令组装 ────────────────────────────────────────────────────────

def test_block_severe_includes_safety_and_antisyc():
    block = build_wellbeing_block("我不想活了")
    assert "安全优先" in block
    assert "真诚陪伴" in block  # 反谄媚常驻
    # 危机指令在反谄媚之前（优先级）
    assert block.index("安全优先") < block.index("真诚陪伴")


def test_block_hotline_appended_only_on_severe():
    block = build_wellbeing_block("我想自杀", hotline="全国心理援助热线 12356")
    assert "12356" in block
    # 非危机时不带热线
    calm = build_wellbeing_block("今天好开心", hotline="12356")
    assert "12356" not in calm


def test_block_elevated_uses_despair_directive():
    block = build_wellbeing_block("我好绝望没人在乎我")
    assert "关怀优先" in block
    assert "安全优先" not in block


def test_block_neutral_only_antisyc():
    block = build_wellbeing_block("我们聊聊吧")
    assert "安全优先" not in block and "关怀优先" not in block
    assert "真诚陪伴" in block


def test_block_switches_off():
    # 全关 → 空串
    assert build_wellbeing_block(
        "我不想活了", enable_crisis=False, enable_anti_sycophancy=False
    ) == ""
    # 只关反谄媚，危机仍在
    only_crisis = build_wellbeing_block(
        "我不想活了", enable_anti_sycophancy=False
    )
    assert "安全优先" in only_crisis and "真诚陪伴" not in only_crisis


# ── 与 emotional_context 集成 ───────────────────────────────────────────

def test_emotional_context_prepends_crisis_block():
    from src.utils.emotional_context import build_emotional_context_block
    ctx: dict = {}
    out = build_emotional_context_block("我不想活了，活着没意思", ctx)
    assert "安全优先" in out
    # 危机块在最前（先于情感/关系块）
    assert out.index("安全优先") == 0 or out.startswith("【⚠️ 安全优先】")
    # 标记落到 user_context 供上层日志/指标
    assert ctx.get("_wellbeing_crisis_level") == "severe"


def test_emotional_context_wellbeing_off():
    from src.utils.emotional_context import build_emotional_context_block
    ctx: dict = {}
    out = build_emotional_context_block(
        "我不想活了", ctx,
        enable_wellbeing=False, enable_anti_sycophancy=False,
    )
    assert "安全优先" not in out and "真诚陪伴" not in out
    assert "_wellbeing_crisis_level" not in ctx
