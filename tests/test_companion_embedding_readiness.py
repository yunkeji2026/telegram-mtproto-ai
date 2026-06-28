"""陪护记忆深度「嵌入源就绪度」检查 + 能力看板自洽体检 footgun 拦截。"""

import pytest

from src.companion.embedding_readiness import (
    check_embedding_readiness,
    embedding_source_configured,
    probe_embedding,
)
from src.companion.capability_advisor import consistency_issues


# ── embedding_source_configured：纯 config 判定 ──────────────────────────────
def test_configured_openai_compatible():
    cfg = {"ai": {"embedding_base_url": "http://127.0.0.1:11434",
                  "embedding_model": "nomic-embed-text"}}
    assert embedding_source_configured(cfg) is True


def test_not_configured_when_base_url_empty():
    cfg = {"ai": {"embedding_base_url": "", "embedding_model": "nomic-embed-text"}}
    assert embedding_source_configured(cfg) is False


def test_not_configured_when_model_off():
    cfg = {"ai": {"embedding_base_url": "http://x", "embedding_model": "none"}}
    assert embedding_source_configured(cfg) is False


def test_configured_gemini_native_needs_key():
    assert embedding_source_configured(
        {"ai": {"provider": "gemini", "embedding_model": "gemini-embedding-001",
                "api_key": "k"}}) is True
    assert embedding_source_configured(
        {"ai": {"provider": "gemini", "embedding_model": "gemini-embedding-001"}}) is False


def test_empty_config_is_not_configured():
    assert embedding_source_configured(None) is False
    assert embedding_source_configured({}) is False


# ── check_embedding_readiness：开关 × 源 的四象限 ────────────────────────────
def test_enabled_but_unconfigured_is_error_footgun():
    cfg = {"memory": {"vector": {"enabled": True}}, "ai": {}}
    r = check_embedding_readiness(cfg)
    assert r["vector_enabled"] is True
    assert r["configured"] is False
    assert r["ready"] is False
    assert r["severity"] == "error"
    assert r["fix"]


def test_enabled_and_configured_is_ready():
    cfg = {"memory": {"vector": {"enabled": True}},
           "ai": {"embedding_base_url": "http://x", "embedding_model": "m"}}
    r = check_embedding_readiness(cfg)
    assert r["ready"] is True
    assert r["severity"] == "ok"


def test_configured_but_disabled_hints_to_enable():
    cfg = {"memory": {"vector": {"enabled": False}},
           "ai": {"embedding_base_url": "http://x", "embedding_model": "m"}}
    r = check_embedding_readiness(cfg)
    assert r["ready"] is False
    assert r["severity"] == "ok"
    assert "vector.enabled" in r["fix"]


def test_disabled_and_unconfigured_is_quiet():
    r = check_embedding_readiness({"memory": {}, "ai": {}})
    assert r["ready"] is False
    assert r["severity"] == "ok"


# ── consistency_issues：footgun 进自洽体检 ──────────────────────────────────
def _caps(vector_on: bool):
    return [{"key": "memory_vector_recall", "label": "记忆向量召回",
             "enabled": vector_on, "stage": "active"}]


def test_consistency_flags_enabled_without_embed_source():
    issues = consistency_issues(_caps(True), embed_ready=False)
    assert any(i["severity"] == "error" and "memory_vector_recall" in i["keys"]
               for i in issues)


def test_consistency_silent_when_embed_ready():
    assert consistency_issues(_caps(True), embed_ready=True) == []


def test_consistency_silent_when_vector_off():
    assert consistency_issues(_caps(False), embed_ready=False) == []


def test_consistency_embed_ready_none_is_backward_compatible():
    # 未传 embed_ready（旧调用方）→ 不应凭空报 embedding error
    assert consistency_issues(_caps(True)) == []


# ── probe_embedding：在线探针，绝不抛 ───────────────────────────────────────
@pytest.mark.asyncio
async def test_probe_handles_missing_client():
    r = await probe_embedding(None)
    assert r["ok"] is False
    assert r["dim"] == 0


@pytest.mark.asyncio
async def test_probe_ok_on_nonempty_vector():
    class _Client:
        async def embed(self, text):
            return [0.1, 0.2, 0.3]

    r = await probe_embedding(_Client())
    assert r["ok"] is True
    assert r["dim"] == 3


@pytest.mark.asyncio
async def test_probe_reports_empty_vector():
    class _Client:
        async def embed(self, text):
            return []

    r = await probe_embedding(_Client())
    assert r["ok"] is False
    assert "空向量" in r["error"]


@pytest.mark.asyncio
async def test_probe_catches_exception():
    class _Client:
        async def embed(self, text):
            raise RuntimeError("boom")

    r = await probe_embedding(_Client())
    assert r["ok"] is False
    assert "boom" in r["error"]
