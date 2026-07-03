"""深度人设 store 门禁（临时库，CRUD + append-only + 上限 + 去重）。"""
import pytest

from src.companion.deep_persona_store import DeepPersonaStore


@pytest.fixture()
def store(tmp_path):
    return DeepPersonaStore(str(tmp_path / "deep.db"))


def test_relationship_profile_roundtrip(store):
    assert store.get_relationship_profile("c1") == ""
    store.set_relationship_profile("c1", "关于TA：养猫")
    assert store.get_relationship_profile("c1") == "关于TA：养猫"
    store.set_relationship_profile("c1", "更新后的画像")
    assert store.get_relationship_profile("c1") == "更新后的画像"


def test_inside_jokes_union_dedup(store):
    store.add_inside_jokes("c1", ["撸串", "梗A"])
    store.add_inside_jokes("c1", ["梗A", "梗B"])  # 梗A 去重
    js = store.get_inside_jokes("c1")
    assert js.count("梗A") == 1
    assert set(js) == {"撸串", "梗A", "梗B"}


def test_open_loops_add_get_resolve_dedup(store):
    store.add_open_loop("c1", "换工作", salience=0.9, ts=1000.0)
    store.add_open_loop("c1", "换工作", salience=0.9, ts=1000.0)  # 去重
    loops = store.get_open_loops("c1")
    assert len(loops) == 1 and loops[0]["topic"] == "换工作"
    store.resolve_open_loop("c1", "换工作")
    assert store.get_open_loops("c1") == []


def test_open_loops_cap(store):
    for i in range(30):
        store.add_open_loop("c1", f"话题{i}", ts=float(i))
    assert len(store.get_open_loops("c1")) <= 20


def test_experiential_add_get_rank_and_dedup(store):
    store.add_experiential("c1", "露营那次", emotion="开心", salience=0.5)
    store.add_experiential("c1", "露营那次", emotion="开心", salience=0.5)  # 去重
    store.add_experiential("c1", "Max跑丢", emotion="焦虑", salience=0.9)
    ev = store.get_experiential("c1")
    assert len(ev) == 2
    assert ev[0]["what"] == "Max跑丢"  # salience 高在前


def test_experiential_embedding_roundtrip(store):
    store.add_experiential("c1", "露营那次", emotion="开心", salience=0.8,
                           emb=[0.1, 0.2, 0.3])
    ev = store.get_experiential("c1")
    assert len(ev) == 1
    assert ev[0]["emb"] == [0.1, 0.2, 0.3]


def test_experiential_no_embedding_ok(store):
    store.add_experiential("c1", "没向量的事", salience=0.5)
    ev = store.get_experiential("c1")
    assert ev[0]["emb"] == []


def test_blank_ids_safe(store):
    store.set_relationship_profile("", "x")
    assert store.get_relationship_profile("") == ""
    store.add_inside_jokes("", ["x"])
    assert store.get_inside_jokes("") == []
