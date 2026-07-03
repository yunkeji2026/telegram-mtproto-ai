"""F1 点火门禁：runtime 依赖注入(embedder/llm/flags) + 语义 sim 端到端透传。"""
import pytest

from datetime import datetime

import src.companion.deep_persona_runtime as rt
from src.companion.deep_persona import build_deep_persona_block


@pytest.fixture(autouse=True)
def _reset_rt():
    rt.reset()
    yield
    rt.reset()


def test_flags_off_by_default():
    rt.configure_from_config({"ai": {}, "companion": {"deep_persona": {"enabled": True}}})
    assert rt.semantic_recall_enabled() is False
    assert rt.llm_refine_enabled() is False


def test_flags_gated_by_master():
    # 子开关开但 master 关 → False
    rt.configure_from_config({"companion": {"deep_persona": {
        "enabled": False, "semantic_recall": True, "llm_refine": True}}})
    assert rt.semantic_recall_enabled() is False
    assert rt.llm_refine_enabled() is False
    # master + 子开关都开 → True
    rt.configure_from_config({"companion": {"deep_persona": {
        "enabled": True, "semantic_recall": True, "llm_refine": True}}})
    assert rt.semantic_recall_enabled() is True
    assert rt.llm_refine_enabled() is True


def test_embedder_none_when_unconfigured():
    rt.configure_from_config({"ai": {}})  # 无 embedding_base_url/model
    assert rt.get_embedder() is None


def test_inject_embedder_and_end_to_end_semantic_recall():
    # 注入假 embedder：把文本映射到简单向量
    _vecs = {"露营那次": [1.0, 0.0], "工作崩溃": [0.0, 1.0], "周末想露营": [1.0, 0.0]}
    rt.set_embedder(lambda t: _vecs.get(t))
    assert rt.get_embedder() is not None
    # 模拟 ai_client 侧构造 sim_fn 并端到端跑 block
    from src.companion.deep_persona import make_embedding_sim_fn
    events = [{"what": "露营那次", "salience": 0.3, "ts": "2026-07-02T10:00:00",
               "emb": [1.0, 0.0]},
              {"what": "工作崩溃", "salience": 0.5, "ts": "2026-07-02T10:00:00",
               "emb": [0.0, 1.0]}]
    emap = {e["what"]: e["emb"] for e in events}
    qv = rt.get_embedder()("周末想露营")
    sim = make_embedding_sim_fn(qv, emap)
    cfg = {"enabled": True, "experiential": True}
    out = build_deep_persona_block(
        {"id": "p"}, now=datetime(2026, 7, 3, 12, 0), cfg=cfg,
        deep_ctx={"experiential_events": events, "query_text": "周末想露营",
                  "experiential_sim_fn": sim})
    # 语义相关的"露营那次"应被选中并出现在块里
    assert "露营那次" in out


def test_inject_llm():
    rt.set_llm(lambda p: "refined")
    assert rt.get_llm()("x") == "refined"


def test_embedder_stats_starts_zero():
    st = rt.embedder_stats()
    assert st["calls"] == 0 and st["failures"] == 0 and st["avg_latency_ms"] == 0.0
