"""R7 写入期 embedding 普及：覆盖率跟随需求（vector 或 R5 近义去重任一开即落向量）。

聚焦 SkillManager._episodic_embeddings_needed 判定，以及 _episodic_patch_embedding /
episodic_backfill_embeddings 在"仅开 semantic_dedup"时也会嵌入（此前只看 vector.enabled）。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


def _sm(memory_cfg):
    from src.skills.skill_manager import SkillManager
    sm = SkillManager.__new__(SkillManager)
    sm._memory_cfg = memory_cfg
    sm.config = SimpleNamespace(config={"memory": memory_cfg})
    return sm


# ── 判定谓词 ────────────────────────────────────────────────────────────

def test_needed_false_when_all_off():
    sm = _sm({})
    assert sm._episodic_embeddings_needed() is False


def test_needed_true_with_vector():
    sm = _sm({"vector": {"enabled": True}})
    assert sm._episodic_embeddings_needed() is True


def test_needed_true_with_semantic_dedup_threshold():
    sm = _sm({"consolidation": {"semantic_dedup": 0.92}})
    assert sm._episodic_embeddings_needed() is True


def test_needed_true_with_semantic_dedup_bool():
    sm = _sm({"consolidation": {"semantic_dedup": True}})
    assert sm._episodic_embeddings_needed() is True


def test_needed_false_when_dedup_falsy():
    sm = _sm({"consolidation": {"semantic_dedup": 0}})
    assert sm._episodic_embeddings_needed() is False


# ── 写入期 patch：仅开 dedup 也会嵌入 ───────────────────────────────────

@pytest.mark.asyncio
async def test_patch_embedding_runs_under_dedup_only():
    sm = _sm({"consolidation": {"semantic_dedup": 0.9}})
    sm._episodic_store = MagicMock()
    sm.ai_client = MagicMock()
    sm.ai_client.embed = AsyncMock(return_value=[[0.1, 0.2, 0.3, 0.4]])
    await sm._episodic_patch_embedding(7, "用户喜欢猫")
    sm.ai_client.embed.assert_awaited_once()
    sm._episodic_store.update_embedding.assert_called_once()


@pytest.mark.asyncio
async def test_patch_embedding_skipped_when_nothing_needs_it():
    sm = _sm({})
    sm._episodic_store = MagicMock()
    sm.ai_client = MagicMock()
    sm.ai_client.embed = AsyncMock(return_value=[[0.1, 0.2]])
    await sm._episodic_patch_embedding(7, "用户喜欢猫")
    sm.ai_client.embed.assert_not_awaited()


# ── 补全：dedup-only 不再被判 vector_disabled ───────────────────────────

@pytest.mark.asyncio
async def test_backfill_allowed_under_dedup_only():
    sm = _sm({"consolidation": {"semantic_dedup": 0.9}})
    sm._episodic_store = MagicMock()
    sm._episodic_store.fetch_rows_missing_embedding.return_value = []
    sm.ai_client = MagicMock()
    out = await sm.episodic_backfill_embeddings(limit=5)
    # 不再因 vector 关而早退（rows 为空 → ok True processed 0）
    assert out.get("ok") is True
    assert out.get("error") != "vector_disabled"


@pytest.mark.asyncio
async def test_backfill_still_blocked_when_nothing_needs_it():
    sm = _sm({})
    sm._episodic_store = MagicMock()
    sm.ai_client = MagicMock()
    out = await sm.episodic_backfill_embeddings(limit=5)
    assert out == {"ok": False, "error": "vector_disabled"}
