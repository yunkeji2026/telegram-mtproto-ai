"""每人设相册 P0 门禁：DB store（CRUD/去重/命中/统计）+ 纯匹配器（触发词/池/轮播/闸门）。"""
import random

from src.companion import persona_media as pm
from src.companion.persona_media_store import PersonaMediaStore


def _store():
    return PersonaMediaStore(":memory:")


# ── store ──────────────────────────────────────────────────────────────────
def test_add_get_roundtrip_deserializes_json():
    s = _store()
    row = s.add("lin", "photo", "/a/b.jpg", "/static/b.jpg",
                triggers=["自拍", "selfie"], tags=["portrait"],
                caption="hi", caption_i18n={"en": "hi there"}, weight=3)
    assert row["persona_id"] == "lin" and row["media_type"] == "photo"
    assert row["triggers"] == ["自拍", "selfie"] and row["tags"] == ["portrait"]
    assert row["caption_i18n"] == {"en": "hi there"} and row["weight"] == 3
    assert row["enabled"] is True
    got = s.get(row["id"])
    assert got and got["url"] == "/static/b.jpg"


def test_list_filters():
    s = _store()
    s.add("lin", "photo", "/1", "/u1", triggers=["a"])
    s.add("lin", "video", "/2", "/u2", enabled=False)
    s.add("mia", "photo", "/3", "/u3")
    assert len(s.list("lin")) == 2
    assert len(s.list("lin", enabled_only=True)) == 1
    assert len(s.list("lin", media_type="video")) == 1
    assert len(s.list()) == 3


def test_find_by_sha_dedup():
    s = _store()
    s.add("lin", "photo", "/1", "/u1", sha256="deadbeef")
    assert s.find_by_sha("lin", "deadbeef") is not None
    assert s.find_by_sha("lin", "nope") is None
    assert s.find_by_sha("mia", "deadbeef") is None


def test_update_whitelist_and_immutable():
    s = _store()
    row = s.add("lin", "photo", "/1", "/u1", triggers=["a"])
    up = s.update(row["id"], triggers=["b", "c"], caption="x", enabled=False,
                  weight=5, file_path="/hacked", url="/hacked")
    assert up["triggers"] == ["b", "c"] and up["caption"] == "x"
    assert up["enabled"] is False and up["weight"] == 5
    # file_path/url 不在白名单 → 不可改
    assert up["file_path"] == "/1" and up["url"] == "/u1"


def test_delete_returns_row():
    s = _store()
    row = s.add("lin", "photo", "/1", "/u1")
    deleted = s.delete(row["id"])
    assert deleted and deleted["id"] == row["id"]
    assert s.get(row["id"]) is None
    assert s.delete("nonexist") is None


def test_record_hit_and_stats():
    s = _store()
    r1 = s.add("lin", "photo", "/1", "/u1")
    s.add("lin", "video", "/2", "/u2")
    s.add("lin", "photo", "/3", "/u3", enabled=False)
    s.record_hit(r1["id"])
    s.record_hit(r1["id"])
    assert s.get(r1["id"])["hits"] == 2
    st = s.stats("lin")
    assert st["total"] == 3 and st["photo"] == 2 and st["video"] == 1
    assert st["enabled"] == 2


def test_analytics_hits_and_top():
    s = _store()
    r1 = s.add("lin", "photo", "/1", "/u1", caption="dance")
    r2 = s.add("lin", "video", "/2", "/u2")
    s.add("mia", "photo", "/3", "/u3")  # 另一人设，验证全局 vs 单人设聚合
    for _ in range(3):
        s.record_hit(r1["id"])
    s.record_hit(r2["id"])
    # 单人设聚合
    a = s.analytics("lin")
    assert a["total"] == 2 and a["total_hits"] == 4
    assert [t["id"] for t in a["top"]] == [r1["id"], r2["id"]]  # 命中降序
    assert a["top"][0]["hits"] == 3 and a["top"][0]["caption"] == "dance"
    # 全局聚合（persona_id=None）跨全部人设
    g = s.analytics()
    assert g["total"] == 3 and g["total_hits"] == 4
    # top_n 截断 + 只含命中过的（mia 那条 hits=0 不入 top）
    assert all(t["hits"] > 0 for t in g["top"])


# ── matcher ──────────────────────────────────────────────────────────────────
def _rows():
    return [
        {"id": "kw1", "media_type": "photo", "enabled": True, "weight": 1,
         "min_bond_level": 0, "triggers": ["跳舞", "dance"], "url": "/kw1"},
        {"id": "gen1", "media_type": "photo", "enabled": True, "weight": 1,
         "min_bond_level": 0, "triggers": [], "url": "/gen1"},
        {"id": "vid1", "media_type": "video", "enabled": True, "weight": 1,
         "min_bond_level": 3, "triggers": ["跳舞"], "url": "/vid1"},
    ]


def test_keyword_hit_beats_generic():
    got = pm.select_media(_rows(), "给我跳舞看看", generic_ok=True,
                          rng=random.Random(1))
    # 命中关键词 → 只在关键词池里挑（kw1 或 vid1），不会回落 gen1
    assert got and got["id"] in ("kw1", "vid1")


def test_generic_only_when_generic_ok():
    rows = _rows()
    # 无关键词命中的普通问候
    assert pm.select_media(rows, "在吗", generic_ok=False) is None
    got = pm.select_media(rows, "在吗", generic_ok=True, rng=random.Random(1))
    assert got and got["id"] == "gen1"


def test_media_type_filter():
    got = pm.select_media(_rows(), "跳舞", media_types=["photo"],
                          rng=random.Random(1))
    assert got and got["id"] == "kw1"  # vid1 是 video 被过滤


def test_bond_level_gate():
    # bond_level=0 < vid1.min_bond_level=3 → vid1 被闸门挡，仅 kw1 候选
    got = pm.select_media(_rows(), "跳舞", bond_level=0, rng=random.Random(1))
    assert got and got["id"] == "kw1"


def test_avoid_id_rotation():
    rows = [
        {"id": "a", "enabled": True, "weight": 1, "triggers": ["x"], "min_bond_level": 0},
        {"id": "b", "enabled": True, "weight": 1, "triggers": ["x"], "min_bond_level": 0},
    ]
    # avoid a → 必出 b
    for seed in range(5):
        got = pm.select_media(rows, "x", avoid_id="a", rng=random.Random(seed))
        assert got["id"] == "b"


def test_weight_bias_deterministic():
    rows = [
        {"id": "light", "enabled": True, "weight": 1, "triggers": ["x"], "min_bond_level": 0},
        {"id": "heavy", "enabled": True, "weight": 100, "triggers": ["x"], "min_bond_level": 0},
    ]
    picks = [pm.select_media(rows, "x", rng=random.Random(s))["id"] for s in range(20)]
    assert picks.count("heavy") > picks.count("light")


def test_explain_match():
    ex = pm.explain_match(_rows(), "跳舞", generic_ok=True)
    assert ex["pool"] == "keyword" and ex["keyword_count"] == 2
    ex2 = pm.explain_match(_rows(), "在吗", generic_ok=True)
    assert ex2["pool"] == "generic" and len(ex2["candidates"]) == 1
    ex3 = pm.explain_match(_rows(), "在吗", generic_ok=False)
    assert ex3["pool"] == "none" and ex3["candidates"] == []


def test_caption_for_i18n_fallback():
    row = {"caption": "默认", "caption_i18n": {"en": "hello"}}
    assert pm.caption_for(row, "en") == "hello"
    assert pm.caption_for(row, "ja") == "默认"  # 无 ja → 回落 caption
    assert pm.caption_for({}, "en", fallback="fb") == "fb"


def test_pick_media_via_store():
    s = _store()
    s.add("lin", "photo", "/1", "/static/1.jpg", triggers=["跳舞"])
    s.add("mia", "photo", "/2", "/static/2.jpg", triggers=["跳舞"])
    got = pm.pick_media(s, "lin", "给我跳舞看看", rng=random.Random(1))
    assert got and got["persona_id"] == "lin"
    assert pm.pick_media(s, "lin", "无关", generic_ok=False) is None
    assert pm.pick_media(None, "lin", "跳舞") is None
