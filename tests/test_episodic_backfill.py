"""SkillManager.episodic_backfill_embeddings 与 store fetch 筛选。"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from src.skills.skill_manager import SkillManager
from src.utils.config_manager import ConfigManager


async def _make_cm(tmp_path: Path, memory: dict) -> ConfigManager:
    cfg = {
        "telegram": {"api_id": "1", "api_hash": "x", "phone_number": "+1"},
        "ai": {"api_key": "k"},
        "skills": {"enabled": []},
        "intent": {"keywords": {}, "patterns": {}},
        "reply": {},
        "context_store": {"ttl_days": 30},
        "memory": memory,
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(cfg, allow_unicode=True), encoding="utf-8")
    (tmp_path / "templates.yaml").write_text("greeting: hi\n", encoding="utf-8")
    (tmp_path / "exchange_rates.yaml").write_text("channels: {}\n", encoding="utf-8")
    cm = ConfigManager(str(tmp_path / "config.yaml"))
    await cm.load()
    return cm


@pytest.mark.asyncio
async def test_episodic_backfill_batch_updates(tmp_path):
    db = tmp_path / "epi_backfill.db"
    cm = await _make_cm(
        tmp_path,
        {
            "enabled": True,
            "db_path": str(db),
            "vector": {"enabled": True},
        },
    )
    ai = MagicMock()
    vec16 = [0.02] * 16

    async def _ewf(texts):
        return [vec16[:] for _ in texts]

    ai.embed_with_fallback = AsyncMock(side_effect=_ewf)
    sm = SkillManager(cm, ai)

    assert sm._episodic_store is not None
    sm._episodic_store.add_fact("u1", "第一条需要向量的事实内容")
    sm._episodic_store.add_fact("u1", "第二条需要向量的事实内容二")
    out = await sm.episodic_backfill_embeddings(limit=10)
    assert out["ok"] is True
    assert out["processed"] == 2
    assert out["updated"] == 2
    assert ai.embed_with_fallback.await_count == 1
    args = ai.embed_with_fallback.call_args[0][0]
    assert len(args) == 2


@pytest.mark.asyncio
async def test_episodic_backfill_vector_disabled(tmp_path):
    cm = await _make_cm(
        tmp_path,
        {
            "enabled": True,
            "db_path": str(tmp_path / "e.db"),
            "vector": {"enabled": False},
        },
    )
    ai = MagicMock()
    ai.embed_with_fallback = AsyncMock()
    sm = SkillManager(cm, ai)
    out = await sm.episodic_backfill_embeddings()
    assert out == {"ok": False, "error": "vector_disabled"}
    ai.embed_with_fallback.assert_not_called()


@pytest.mark.asyncio
async def test_episodic_backfill_respects_memory_key_prefix(tmp_path):
    db = tmp_path / "epi_pref.db"
    cm = await _make_cm(
        tmp_path,
        {
            "enabled": True,
            "db_path": str(db),
            "vector": {"enabled": True},
        },
    )
    ai = MagicMock()
    ai.embed_with_fallback = AsyncMock(
        return_value=[[0.03] * 16]
    )
    sm = SkillManager(cm, ai)
    sm._episodic_store.add_fact("-100_1", "fact A for group minus 100")
    sm._episodic_store.add_fact("-200_2", "fact B other group")
    out = await sm.episodic_backfill_embeddings(limit=10, memory_key_prefix="-100")
    assert out["ok"] is True
    assert out["processed"] == 1
    assert out["updated"] == 1
    ai.embed_with_fallback.assert_called_once()
    texts_arg = ai.embed_with_fallback.call_args[0][0]
    assert len(texts_arg) == 1
    assert "minus 100" in texts_arg[0]


@pytest.mark.asyncio
async def test_episodic_backfill_daily_budget_blocks(tmp_path):
    import src.skills.skill_manager as smod

    smod._EPISODIC_BACKFILL_BUDGET_DAY = None
    smod._EPISODIC_BACKFILL_BUDGET_USED = 0
    db = tmp_path / "bud.db"
    cm = await _make_cm(
        tmp_path,
        {
            "enabled": True,
            "db_path": str(db),
            "vector": {
                "enabled": True,
                "daily_embed_budget": {"enabled": True, "max_calls": 0},
            },
        },
    )
    ai = MagicMock()
    ai.embed_with_fallback = AsyncMock()
    sm = SkillManager(cm, ai)
    sm._episodic_store.add_fact("u1", "some fact content here")
    out = await sm.episodic_backfill_embeddings(limit=5)
    assert out["ok"] is False
    assert out["error"] == "daily_embed_budget_exceeded"
    ai.embed_with_fallback.assert_not_called()
