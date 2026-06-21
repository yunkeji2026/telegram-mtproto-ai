"""N 线 核心1：共享回复大脑 companion_context 单测。

验证 A/B 两线复用的人设路由 / 情绪 hint / 标准上下文装配一致且向后兼容。
"""
import pytest

from src.utils.companion_context import (
    build_companion_context,
    emotion_hint,
    record_relationship_message,
    record_story_completion,
    reset_relationship_providers,
    resolve_entitlement,
    resolve_funnel_stage,
    resolve_intimacy_score,
    route_persona_id,
    set_relationship_providers,
)


# ── route_persona_id ────────────────────────────────────────────────────────

def test_route_empty_returns_blank():
    assert route_persona_id(None, "private") == ""
    assert route_persona_id([], "group") == ""


def test_route_private_uses_first():
    assert route_persona_id(["a", "b", "c"], "private") == "a"
    assert route_persona_id(["a"], "") == "a"


def test_route_group_uses_second():
    assert route_persona_id(["a", "b", "c"], "group") == "b"
    assert route_persona_id(["a", "b"], "supergroup") == "b"


def test_route_channel_uses_third_when_available():
    assert route_persona_id(["a", "b", "c"], "channel") == "c"


def test_route_channel_with_two_personas_falls_back_to_second():
    # 与 A 线原逻辑等价：_is_group 含 channel，故 channel 且 len==2 → 第二个
    assert route_persona_id(["a", "b"], "channel") == "b"


def test_route_group_single_persona_falls_back_to_first():
    assert route_persona_id(["a"], "group") == "a"
    assert route_persona_id(["a"], "channel") == "a"


def test_route_matches_a_line_original_logic():
    """逐 chat_type 比对 A 线原内联三元表达式。"""
    def _original(ids, chat_type):
        _is_group = chat_type in ("group", "supergroup", "channel")
        if not ids:
            return ""
        if chat_type == "channel" and len(ids) > 2:
            return ids[2]
        if _is_group and len(ids) > 1:
            return ids[1]
        return ids[0]

    for ids in ([], ["a"], ["a", "b"], ["a", "b", "c"]):
        for ct in ("private", "group", "supergroup", "channel", ""):
            assert route_persona_id(ids, ct) == _original(ids, ct), (ids, ct)


# ── emotion_hint ─────────────────────────────────────────────────────────────

class _FakeEnhancer:
    def __init__(self, emotion="happy", raises=False):
        self._emotion = emotion
        self._raises = raises

    def analyze_message_emotion(self, text):
        if self._raises:
            raise RuntimeError("boom")
        return {"emotion": self._emotion}


def test_emotion_hint_none_enhancer_neutral():
    assert emotion_hint("hello", None) == "neutral"


def test_emotion_hint_empty_text_neutral():
    assert emotion_hint("", _FakeEnhancer("happy")) == "neutral"


def test_emotion_hint_reads_enhancer():
    assert emotion_hint("我好开心", _FakeEnhancer("happy")) == "happy"


def test_emotion_hint_swallows_errors():
    assert emotion_hint("x", _FakeEnhancer(raises=True)) == "neutral"


# ── build_companion_context ──────────────────────────────────────────────────

def test_build_basic_private():
    ctx = build_companion_context(platform="telegram", chat_id="123")
    assert ctx["platform"] == "telegram"
    assert ctx["chat_id"] == "123"
    assert ctx["chat_type"] == "private"
    assert ctx["is_group"] is False


def test_build_group_flags():
    ctx = build_companion_context(
        platform="telegram", chat_id=-100, chat_type="supergroup"
    )
    assert ctx["is_group"] is True
    assert ctx["chat_type"] == "supergroup"


def test_build_persona_id_takes_priority_over_list():
    ctx = build_companion_context(
        platform="telegram", chat_id="1",
        persona_id="explicit", account_persona_ids=["x", "y"],
    )
    assert ctx["account_persona_id"] == "explicit"


def test_build_persona_routes_from_list_when_no_explicit():
    ctx = build_companion_context(
        platform="telegram", chat_id="1", chat_type="group",
        account_persona_ids=["x", "y"],
    )
    assert ctx["account_persona_id"] == "y"


def test_build_includes_emotion_hint_only_when_non_neutral():
    ctx_neutral = build_companion_context(
        platform="telegram", chat_id="1", text="hi", emotion_enhancer=None
    )
    assert "user_emotion_hint" not in ctx_neutral
    ctx_happy = build_companion_context(
        platform="telegram", chat_id="1", text="开心",
        emotion_enhancer=_FakeEnhancer("happy"),
    )
    assert ctx_happy["user_emotion_hint"] == "happy"


def test_build_extra_merges_and_skips_none():
    ctx = build_companion_context(
        platform="telegram", chat_id="1",
        extra={"channel": "protocol", "contact_id": None, "intimacy_score": 42},
    )
    assert ctx["channel"] == "protocol"
    assert ctx["intimacy_score"] == 42
    assert "contact_id" not in ctx


def test_build_b_line_shape_matches_expectation():
    """模拟 B 线协议私聊调用：标准键齐备，可直接喂 skill_manager。"""
    ctx = build_companion_context(
        platform="telegram", chat_id="555", text="在吗",
        chat_type="private", persona_id="warm_companion",
        extra={"channel": "protocol"},
    )
    assert ctx == {
        "platform": "telegram",
        "chat_id": "555",
        "chat_type": "private",
        "is_group": False,
        "account_persona_id": "warm_companion",
        "channel": "protocol",
    }


# ── Q3：关系事实源 provider（intimacy / funnel / recorder）─────────────────

@pytest.fixture(autouse=True)
def _clean_providers():
    """每例前后清空进程级 provider，避免相互污染。"""
    reset_relationship_providers()
    yield
    reset_relationship_providers()


def test_resolve_returns_none_without_provider():
    assert resolve_intimacy_score("acct", "123") is None
    assert resolve_funnel_stage("acct", "123") is None


def test_resolve_intimacy_passes_channel_account_external():
    seen = {}

    def _fake(*, channel, account_id, external_id):
        seen.update(channel=channel, account_id=account_id, external_id=external_id)
        return 73.0

    set_relationship_providers(intimacy_lookup=_fake)
    assert resolve_intimacy_score("acctA", 999) == 73.0
    assert seen == {"channel": "telegram", "account_id": "acctA", "external_id": "999"}


def test_resolve_intimacy_none_value_and_empty_chatkey():
    set_relationship_providers(intimacy_lookup=lambda **_: None)
    assert resolve_intimacy_score("a", "1") is None
    # 空 chat_key 直接短路，不调 provider
    assert resolve_intimacy_score("a", "") is None
    assert resolve_intimacy_score("a", None) is None


def test_resolve_swallows_exception():
    def _boom(**_):
        raise RuntimeError("db down")

    set_relationship_providers(intimacy_lookup=_boom, funnel_lookup=_boom)
    assert resolve_intimacy_score("a", "1") is None
    assert resolve_funnel_stage("a", "1") is None


def test_resolve_funnel_normalizes_and_blank_to_none():
    set_relationship_providers(funnel_lookup=lambda **_: "  engaged  ")
    assert resolve_funnel_stage("a", "1") == "engaged"
    set_relationship_providers(funnel_lookup=lambda **_: "")
    reset_relationship_providers()
    set_relationship_providers(funnel_lookup=lambda **_: "   ")
    assert resolve_funnel_stage("a", "1") is None


def test_record_noop_without_recorder():
    # 未注册 recorder → 不抛、不报错（默认零行为）
    record_relationship_message("a", "1", "in", text_preview="hi")


def test_record_passes_expected_kwargs_and_truncates():
    calls = []

    def _rec(*, channel, account_id, external_id, direction, text_preview, display_name):
        calls.append({
            "channel": channel, "account_id": account_id,
            "external_id": external_id, "direction": direction,
            "text_preview": text_preview, "display_name": display_name,
        })

    set_relationship_providers(message_recorder=_rec)
    long_text = "x" * 300
    record_relationship_message(
        "acctB", 42, "in", text_preview=long_text, display_name="Bob",
    )
    assert len(calls) == 1
    c = calls[0]
    assert c["channel"] == "telegram"
    assert c["account_id"] == "acctB"
    assert c["external_id"] == "42"
    assert c["direction"] == "in"
    assert c["display_name"] == "Bob"
    assert len(c["text_preview"]) == 120  # 截断到 120


def test_record_empty_chatkey_short_circuits():
    calls = []
    set_relationship_providers(message_recorder=lambda **k: calls.append(k))
    record_relationship_message("a", "", "in", text_preview="hi")
    record_relationship_message("a", None, "out", text_preview="hi")
    assert calls == []


def test_record_swallows_recorder_exception():
    def _boom(**_):
        raise RuntimeError("write fail")

    set_relationship_providers(message_recorder=_boom)
    # 不抛
    record_relationship_message("a", "1", "out", text_preview="bye")


def test_set_providers_is_partial_update():
    set_relationship_providers(intimacy_lookup=lambda **_: 10.0)
    set_relationship_providers(funnel_lookup=lambda **_: "warming")
    # 两者都还在（partial 覆盖，不互相清空）
    assert resolve_intimacy_score("a", "1") == 10.0
    assert resolve_funnel_stage("a", "1") == "warming"


# ── record_story_completion（剧情收场镜像） ──────────────────────────────────

def test_story_recorder_noop_when_unregistered():
    reset_relationship_providers()
    # 未注册 → no-op，返回 False，不抛
    assert record_story_completion("a", "1", "coffee_date", intimacy_bonus=4) is False


def test_story_recorder_passes_kwargs_and_returns_true():
    calls = []

    def _rec(*, channel, account_id, external_id, scenario_id, ending,
             intimacy_bonus, title):
        calls.append({
            "channel": channel, "account_id": account_id,
            "external_id": external_id, "scenario_id": scenario_id,
            "ending": ending, "intimacy_bonus": intimacy_bonus, "title": title,
        })
        return "evt-1"  # 非 None → 视为成功

    set_relationship_providers(story_recorder=_rec)
    ok = record_story_completion(
        "acctZ", 99, "coffee_date", ending="warm", intimacy_bonus=4.0,
        title="初次约会")
    assert ok is True
    assert len(calls) == 1
    c = calls[0]
    assert c["channel"] == "telegram"
    assert c["account_id"] == "acctZ"
    assert c["external_id"] == "99"
    assert c["scenario_id"] == "coffee_date"
    assert c["ending"] == "warm"
    assert c["intimacy_bonus"] == 4.0
    assert c["title"] == "初次约会"


def test_story_recorder_false_when_recorder_returns_none():
    set_relationship_providers(story_recorder=lambda **_: None)
    assert record_story_completion("a", "1", "coffee_date", intimacy_bonus=2) is False


def test_story_recorder_short_circuits_on_empty_inputs():
    calls = []
    set_relationship_providers(story_recorder=lambda **k: calls.append(k) or "x")
    assert record_story_completion("a", "", "coffee_date") is False
    assert record_story_completion("a", "1", "") is False  # 空 scenario_id
    assert calls == []


def test_story_recorder_swallows_exception():
    def _boom(**_):
        raise RuntimeError("write fail")

    set_relationship_providers(story_recorder=_boom)
    assert record_story_completion("a", "1", "coffee_date", intimacy_bonus=3) is False


# ── resolve_entitlement（Stage 1 付费权益接线） ──────────────────────────────

def test_resolve_entitlement_none_when_unregistered():
    reset_relationship_providers()
    assert resolve_entitlement("tg:acc:u1") is None


def test_resolve_entitlement_returns_dict_from_provider():
    reset_relationship_providers()
    seen = []
    ent = {"tier": "vip", "grants": ["story_ch1"], "unlocked": []}
    set_relationship_providers(
        entitlement_resolver=lambda ck: seen.append(ck) or ent)
    out = resolve_entitlement("tg:acc:u1")
    assert out == ent
    assert seen == ["tg:acc:u1"]  # 以 contact_key 调用


def test_resolve_entitlement_none_on_blank_key():
    set_relationship_providers(entitlement_resolver=lambda ck: {"tier": "vip"})
    assert resolve_entitlement("") is None
    assert resolve_entitlement(None) is None


def test_resolve_entitlement_swallows_exception():
    def _boom(ck):
        raise RuntimeError("store down")
    set_relationship_providers(entitlement_resolver=_boom)
    assert resolve_entitlement("tg:acc:u1") is None


def test_resolve_entitlement_none_when_provider_returns_non_dict():
    set_relationship_providers(entitlement_resolver=lambda ck: "not-a-dict")
    assert resolve_entitlement("tg:acc:u1") is None
