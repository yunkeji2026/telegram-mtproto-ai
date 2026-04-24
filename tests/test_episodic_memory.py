"""Tests for episodic memory store and forget/heuristic helpers."""

import tempfile
from pathlib import Path

import pytest

from src.utils.episodic_memory_store import (
    EpisodicMemoryStore,
    compute_memory_storage_key,
)
from src.utils.episodic_vector import cosine_similarity, vec_to_blob
from src.utils.memory_heuristic import extract_heuristic_facts, matches_forget_intent


@pytest.fixture
def mem_db():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "t.db"
        store = EpisodicMemoryStore(p)
        yield store
        store.close()


def test_add_dedupe(mem_db: EpisodicMemoryStore):
    uid = "u1"
    assert mem_db.add_fact(uid, "用户自称：小明") is not None
    assert mem_db.add_fact(uid, "用户自称：小明") is None
    assert mem_db.count(uid) == 1


def test_prune(mem_db: EpisodicMemoryStore):
    uid = "u2"
    for i in range(5):
        mem_db.add_fact(uid, f"fact {i} unique {i}")
    mem_db.prune_oldest(uid, 2)
    assert mem_db.count(uid) == 2


def test_heuristic_call_me():
    fs = extract_heuristic_facts("以后叫我阿强就行")
    assert any("阿强" in f for f in fs)


def test_forget_match():
    assert matches_forget_intent("忘掉吧", ["忘掉"]) is True
    assert matches_forget_intent("今天天气好", ["忘掉"]) is False


def test_compute_memory_storage_key():
    assert compute_memory_storage_key("user", "123", -10099) == "123"
    assert compute_memory_storage_key("chat_user", "123", 123) == "123"
    assert compute_memory_storage_key("chat_user", "456", -10099) == "-10099_456"


def test_cosine_and_fusion(mem_db: EpisodicMemoryStore):
    a = [1.0, 0.0, 0.0]
    b = [1.0, 0.0, 0.0]
    assert abs(cosine_similarity(a, b) - 1.0) < 1e-6
    uid = "vx"
    rid = mem_db.add_fact(uid, "咖啡燕麦拿铁", embedding_blob=vec_to_blob([0.9, 0.1, 0.0]))
    assert rid is not None
    out = mem_db.get_bullets_for_prompt(
        uid,
        3,
        500,
        query_text="拿铁",
        rerank_keywords=True,
        query_embedding=[0.95, 0.05, 0.0],
        use_vector_fusion=True,
        vector_weight=0.5,
        keyword_weight=0.5,
    )
    assert "拿铁" in out or "咖啡" in out


def test_rerank_prefers_overlap(mem_db: EpisodicMemoryStore):
    uid = "u9"
    mem_db.add_fact(uid, "用户喜欢喝燕麦拿铁")
    mem_db.add_fact(uid, "用户住在北京")
    mem_db.add_fact(uid, "用户讨厌下雨")
    out = mem_db.get_bullets_for_prompt(
        uid, 2, 500, query_text="你记得我喜欢喝什么吗", rerank_keywords=True
    )
    assert "燕麦" in out or "拿铁" in out


def test_fetch_rows_missing_embedding_and_prefix(mem_db: EpisodicMemoryStore):
    mem_db.add_fact("alpha_1", "无向量事实一")
    mem_db.add_fact("beta_2", "无向量事实二")
    r1 = mem_db.add_fact(
        "alpha_1",
        "已有向量",
        embedding_blob=vec_to_blob([0.1, 0.2, 0.3, 0.4]),
    )
    assert r1 is not None
    all_missing = mem_db.fetch_rows_missing_embedding(10)
    assert len(all_missing) == 2
    only_alpha = mem_db.fetch_rows_missing_embedding(10, memory_key_prefix="alpha")
    assert len(only_alpha) == 1
    assert only_alpha[0][1] == "alpha_1"
