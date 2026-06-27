"""统一草稿引擎 SkillManager.generate_inbox_draft 单测（彻底对齐 Phase 2）。

锁定契约：
  - 走人设产线生成回复，复用情景记忆读取（memory_hit 埋点）
  - 相似度重试：与上条回复高度重复 → 抬温度重生一次（retry_applied 埋点）
  - 空回复 → 返回 None 且记 empty
  - 规则栈埋点经 MetricsStore.get_inbox_draft_metrics 暴露
并单测 MetricsStore 的 inbox_draft 计数/窗口/命中率快照。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from src.monitoring import metrics_store as _ms
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
    (tmp_path / "config.yaml").write_text(
        yaml.dump(cfg, allow_unicode=True), encoding="utf-8"
    )
    (tmp_path / "templates.yaml").write_text("greeting: hi\n", encoding="utf-8")
    (tmp_path / "exchange_rates.yaml").write_text("channels: {}\n", encoding="utf-8")
    cm = ConfigManager(str(tmp_path / "config.yaml"))
    await cm.load()
    return cm


def _reset_metrics():
    _ms.MetricsStore._instance = None
    return _ms.get_metrics_store()


# ── MetricsStore.inbox_draft 维度 ────────────────────────────────

def test_metrics_inbox_draft_counts_and_rates():
    m = _reset_metrics()
    for _ in range(4):
        m.record_inbox_draft_event("generated")
    m.record_inbox_draft_event("memory_hit")
    m.record_inbox_draft_event("memory_hit")
    m.record_inbox_draft_event("retry_applied")
    snap = m.get_inbox_draft_metrics()
    assert snap["total"]["generated"] == 4
    assert snap["total"]["memory_hit"] == 2
    assert snap["window"]["generated"] == 4
    # 命中率基于累计 generated
    assert snap["rates_vs_generated"]["memory_hit"] == 0.5
    assert snap["rates_vs_generated"]["retry_applied"] == 0.25


def test_metrics_inbox_draft_ignores_empty_and_zero():
    m = _reset_metrics()
    m.record_inbox_draft_event("")
    m.record_inbox_draft_event("generated", count=0)
    assert m.get_inbox_draft_metrics()["total"] == {}


# ── generate_inbox_draft ────────────────────────────────────────

@pytest.mark.asyncio
async def test_generate_inbox_draft_basic_and_memory_hit(tmp_path):
    _reset_metrics()
    cm = await _make_cm(
        tmp_path,
        {
            "enabled": True,
            "db_path": str(tmp_path / "draft_mem.db"),
            "vector": {"enabled": False},
            "extract": {"enabled": False},  # 避免 fire-and-forget 抽取任务噪声
        },
    )
    ai = MagicMock()
    ai.generate_reply_with_intent = AsyncMock(return_value="你好呀，Jun～")
    sm = SkillManager(cm, ai)

    # 预置该联系人的长期事实 → 注入应命中（memory_hit）
    key = sm._episodic_storage_key("u1", "", "telegram")
    assert key
    sm._episodic_store.add_fact(key, "用户的名字叫 Jun", source="user_stated")

    out = await sm.generate_inbox_draft(
        text="还记得我叫什么吗",
        chat_key="u1",
        platform="telegram",
        history=[{"role": "user", "content": "还记得我叫什么吗"}],
    )
    assert out is not None
    assert out["reply"] == "你好呀，Jun～"
    ai.generate_reply_with_intent.assert_awaited()
    # 注入的记忆文本进入了传给 AI 的 user_context
    _ctx = ai.generate_reply_with_intent.await_args.kwargs["user_context"]
    assert "Jun" in (_ctx.get("_episodic_memory_text") or "")

    snap = _ms.get_metrics_store().get_inbox_draft_metrics()
    assert snap["total"].get("generated") == 1
    assert snap["total"].get("memory_hit") == 1


@pytest.mark.asyncio
async def test_generate_inbox_draft_similarity_retry(tmp_path):
    _reset_metrics()
    cm = await _make_cm(
        tmp_path,
        {
            "enabled": True,
            "db_path": str(tmp_path / "retry_mem.db"),
            "vector": {"enabled": False},
            "extract": {"enabled": False},
        },
    )
    ai = MagicMock()
    # 第一次返回与上条几乎一致 → 触发重试；第二次返回不同 → 采纳
    ai.generate_reply_with_intent = AsyncMock(
        side_effect=["在的在的，亲在的哦", "刚去倒了杯水，怎么啦～"]
    )
    sm = SkillManager(cm, ai)

    # 预置上条回复，制造高相似度
    uc = sm._get_user_context("u2")
    uc["last_reply"] = "在的在的，亲在的哦"
    sm._context_store.mark_dirty("u2")

    out = await sm.generate_inbox_draft(
        text="在吗",
        chat_key="u2",
        platform="telegram",
        history=[{"role": "user", "content": "在吗"}],
    )
    assert out is not None
    assert out["reply"] == "刚去倒了杯水，怎么啦～"
    assert ai.generate_reply_with_intent.await_count == 2
    snap = _ms.get_metrics_store().get_inbox_draft_metrics()
    assert snap["total"].get("retry_applied") == 1


@pytest.mark.asyncio
async def test_generate_inbox_draft_empty_returns_none(tmp_path):
    _reset_metrics()
    cm = await _make_cm(
        tmp_path,
        {"enabled": True, "db_path": str(tmp_path / "e.db"),
         "vector": {"enabled": False}, "extract": {"enabled": False}},
    )
    ai = MagicMock()
    ai.generate_reply_with_intent = AsyncMock(return_value="   ")
    sm = SkillManager(cm, ai)
    out = await sm.generate_inbox_draft(
        text="测试", chat_key="u3", platform="telegram", history=[],
    )
    assert out is None
    snap = _ms.get_metrics_store().get_inbox_draft_metrics()
    assert snap["total"].get("empty") == 1
    assert "generated" not in snap["total"]
