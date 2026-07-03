"""Wave-Next A/B 门禁：关系画像巩固编排 + open_loop 自动收尾 + 节流 + stats。"""
import pytest

from src.companion.deep_persona import (
    build_profile_from_signals,
    find_resolved_loops,
    run_deep_persona_consolidation,
)
from src.companion.deep_persona_stats import DeepPersonaStats
from src.companion.deep_persona_store import DeepPersonaStore


# ── build_profile_from_signals ────────────────────────────────────
def test_profile_from_signals_uses_topics_and_experience():
    inbound = ["最近在健身减脂", "健身完好累", "又去健身了", "今天也健身",
               "工作压力好大", "工作又加班", "工作真的烦"]
    exp = [{"what": "一起看了流星雨", "emotion": "感动", "salience": 0.9}]
    p = build_profile_from_signals(
        display_name="阿明", inbound_texts=inbound, dominant_emotion="低落",
        experiential=exp)
    assert "阿明" in p
    assert "健身" in p or "工作" in p
    assert "流星雨" in p
    assert "低落" in p
    assert len(p) <= 200


def test_profile_from_signals_empty_safe():
    assert build_profile_from_signals() == ""


# ── find_resolved_loops ───────────────────────────────────────────
def test_find_resolved_loops_matches_topic():
    loops = [{"topic": "换工作的事"}, {"topic": "买咖啡机"}]
    out = find_resolved_loops(loops, "换工作的事终于定了，下周入职新公司")
    assert "换工作的事" in out
    assert "买咖啡机" not in out


def test_find_resolved_loops_no_match():
    loops = [{"topic": "换工作"}]
    assert find_resolved_loops(loops, "今天天气不错") == []
    assert find_resolved_loops([], "任意") == []


# ── run_deep_persona_consolidation（用假 inbox store）────────────────
class _FakeInbox:
    def __init__(self, msgs, meta=None, conv=None):
        self._msgs = msgs
        self._meta = meta or {}
        self._conv = conv or {}

    def list_messages(self, cid, *, limit=200):
        return self._msgs

    def get_conv_meta(self, cid):
        return self._meta

    def get_conversation(self, cid):
        return self._conv


def test_consolidation_writes_profile_and_jokes(tmp_path):
    deep = DeepPersonaStore(str(tmp_path / "d.db"))
    deep.add_experiential("c1", "一起爬山看日出", emotion="开心", salience=0.8)
    inbox = _FakeInbox(
        msgs=[{"text": "又想撸串了", "direction": "in"},
              {"text": "今晚撸串不", "direction": "in"},
              {"text": "撸串走起", "direction": "in"},
              {"text": "好的", "direction": "out"}],
        meta={"last_emotion": "积极"},
        conv={"display_name": "阿强"},
    )
    r = run_deep_persona_consolidation(inbox, deep, "c1")
    assert r["jokes"] >= 1
    assert r["profile"] is True
    assert "撸串" in deep.get_inside_jokes("c1")
    prof = deep.get_relationship_profile("c1")
    assert "阿强" in prof and "爬山看日出" in prof


def test_consolidation_safe_on_bad_stores():
    r = run_deep_persona_consolidation(None, None, "c1")
    assert r["profile"] is False and r["jokes"] == 0 and r["drift"] == []


def test_detect_persona_drift_hits_forbidden():
    from src.companion.deep_persona import detect_persona_drift
    persona = {"speaking": {"forbidden_phrases": ["作为AI"]},
               "boundaries": {"topics_to_avoid": ["政治"]}}
    assert "作为AI" in detect_persona_drift(persona, profile="关于TA：作为AI我...")
    assert "政治" in detect_persona_drift(persona, inside_jokes=["聊政治"])
    assert detect_persona_drift(persona, profile="关于TA：喜欢露营") == []


def test_consolidation_drift_guard_skips_write(tmp_path):
    deep = DeepPersonaStore(str(tmp_path / "d.db"))
    # 构造画像会含"政治"（人设禁忌）→ 漂移守卫应拒写
    inbox = _FakeInbox(
        msgs=[{"text": "我们聊政治吧", "direction": "in"},
              {"text": "政治真有意思", "direction": "in"},
              {"text": "继续聊政治", "direction": "in"}],
        meta={"last_emotion": "平稳"}, conv={"display_name": "阿民"})
    persona = {"boundaries": {"topics_to_avoid": ["政治"]}}
    r = run_deep_persona_consolidation(inbox, deep, "c1", persona=persona)
    assert r["profile"] is False
    assert "政治" in r["drift"]
    assert deep.get_relationship_profile("c1") == ""  # 未写


def test_consolidation_self_heal_clears_polluted_profile(tmp_path):
    """D4：旧画像已被污染（含禁忌），新一轮又漂移 → 清掉旧脏画像（自愈）。"""
    deep = DeepPersonaStore(str(tmp_path / "d.db"))
    deep.set_relationship_profile("c1", "关于TA：聊政治很起劲")  # 预置脏画像
    inbox = _FakeInbox(
        msgs=[{"text": "继续聊政治", "direction": "in"},
              {"text": "政治话题", "direction": "in"},
              {"text": "政治真有意思", "direction": "in"}],
        meta={"last_emotion": "平稳"}, conv={"display_name": "阿民"})
    persona = {"boundaries": {"topics_to_avoid": ["政治"]}}
    r = run_deep_persona_consolidation(inbox, deep, "c1", persona=persona)
    assert r["profile"] is False and "政治" in r["drift"]
    assert r["healed"] is True
    assert deep.get_relationship_profile("c1") == ""  # 脏画像被清


def test_consolidation_backfills_missing_embeddings(tmp_path):
    """G2：巩固时给缺向量的历史事件批量回填（embedder 就绪）。"""
    deep = DeepPersonaStore(str(tmp_path / "d.db"))
    deep.add_experiential("c1", "露营那次", salience=0.8)   # 无向量
    inbox = _FakeInbox(msgs=[{"text": "hi", "direction": "in"}],
                       meta={"last_emotion": "平稳"}, conv={"display_name": "A"})
    r = run_deep_persona_consolidation(
        inbox, deep, "c1", embedder=lambda t: [0.1, 0.2, 0.3])
    assert r["emb_backfilled"] == 1
    ev = deep.get_experiential("c1")
    assert ev[0]["emb"] == [0.1, 0.2, 0.3]


def test_life_shares_record_and_get(tmp_path):
    deep = DeepPersonaStore(str(tmp_path / "d.db"))
    assert deep.get_life_shares("c1") == []
    deep.record_life_share("c1", ts=1000.0)
    deep.record_life_share("c1", ts=2000.0)
    got = deep.get_life_shares("c1")
    assert len(got) == 2 and 2000.0 in got


def test_consolidation_idempotent(tmp_path):
    """巩固幂等：跑两次画像稳定、内部梗不重复膨胀（防漂移/堆积）。"""
    deep = DeepPersonaStore(str(tmp_path / "d.db"))
    inbox = _FakeInbox(
        msgs=[{"text": "又想撸串了", "direction": "in"},
              {"text": "今晚撸串不", "direction": "in"},
              {"text": "撸串走起", "direction": "in"}],
        meta={"last_emotion": "积极"}, conv={"display_name": "阿强"})
    run_deep_persona_consolidation(inbox, deep, "c1")
    p1 = deep.get_relationship_profile("c1")
    j1 = deep.get_inside_jokes("c1")
    run_deep_persona_consolidation(inbox, deep, "c1")
    p2 = deep.get_relationship_profile("c1")
    j2 = deep.get_inside_jokes("c1")
    assert p1 == p2
    assert j1 == j2  # 去重，不膨胀


# ── 节流 ───────────────────────────────────────────────────────────
def test_due_for_consolidation_throttle(tmp_path):
    deep = DeepPersonaStore(str(tmp_path / "d.db"))
    assert deep.due_for_consolidation("c1", min_interval_sec=900) is True
    assert deep.due_for_consolidation("c1", min_interval_sec=900) is False  # 立刻再问=节流
    assert deep.due_for_consolidation("c2", min_interval_sec=900) is True   # 另一会话独立
    assert deep.due_for_consolidation("", min_interval_sec=900) is False


# ── stats ──────────────────────────────────────────────────────────
def test_stats_incr_and_dump():
    s = DeepPersonaStats()
    s.incr("consolidations")
    s.incr("jokes_detected", 3)
    s.incr("unknown_key")  # 未知键忽略
    d = s.dump()
    assert d["consolidations"] == 1 and d["jokes_detected"] == 3
    prom = s.dump_prom()
    assert "deep_persona_consolidations_total 1" in prom
    assert "deep_persona_jokes_detected_total 3" in prom


def test_stats_reset():
    s = DeepPersonaStats()
    s.incr("profiles_built")
    s.reset()
    assert s.dump()["profiles_built"] == 0
