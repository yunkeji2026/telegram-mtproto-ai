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
async def test_fast_path_low_risk_skips_slow_think_and_records_latency(tmp_path):
    _reset_metrics()
    cm = await _make_cm(
        tmp_path,
        {"enabled": True, "db_path": str(tmp_path / "fp.db"),
         "vector": {"enabled": False}, "extract": {"enabled": False}},
    )
    ai = MagicMock()
    ai.generate_reply_with_intent = AsyncMock(return_value="嗯嗯好的～")
    ai.slow_think_outline = AsyncMock(return_value="计划要点")
    sm = SkillManager(cm, ai)
    # 强制开启慢思考且命中本轮 intent → 验证「快路确实跳过了它」
    _text = "在吗"
    _intent = sm._recognize_intent(_text)
    sm._memory_cfg["slow_think"] = {
        "enabled": True, "intents": [_intent], "min_message_chars": 1,
        "stage1_max_tokens": 50,
    }

    out = await sm.generate_inbox_draft(
        text=_text, chat_key="uf", platform="telegram",
        history=[{"role": "user", "content": _text}], risk_level="low",
    )
    assert out is not None
    ai.slow_think_outline.assert_not_awaited()  # 快路跳过慢思考
    snap = _ms.get_metrics_store().get_inbox_draft_metrics()
    assert snap["total"].get("fast_path") == 1
    assert "slow_think" not in snap["total"]
    assert snap["latency"]["count"] == 1  # 延迟已记录


@pytest.mark.asyncio
async def test_high_risk_runs_slow_think(tmp_path):
    _reset_metrics()
    cm = await _make_cm(
        tmp_path,
        {"enabled": True, "db_path": str(tmp_path / "hr.db"),
         "vector": {"enabled": False}, "extract": {"enabled": False}},
    )
    ai = MagicMock()
    ai.generate_reply_with_intent = AsyncMock(return_value="我帮您核实一下退款")
    ai.slow_think_outline = AsyncMock(return_value="先共情→核实→给方案")
    sm = SkillManager(cm, ai)
    _text = "我要投诉并退款"
    _intent = sm._recognize_intent(_text)
    sm._memory_cfg["slow_think"] = {
        "enabled": True, "intents": [_intent], "min_message_chars": 1,
        "stage1_max_tokens": 50,
    }

    out = await sm.generate_inbox_draft(
        text=_text, chat_key="uh", platform="telegram",
        history=[{"role": "user", "content": _text}], risk_level="high",
    )
    assert out is not None
    ai.slow_think_outline.assert_awaited()  # 高风险吃满全栈
    snap = _ms.get_metrics_store().get_inbox_draft_metrics()
    assert snap["total"].get("slow_think") == 1
    assert "fast_path" not in snap["total"]


@pytest.mark.asyncio
async def test_long_history_summarized_and_cached(tmp_path):
    _reset_metrics()
    cm = await _make_cm(
        tmp_path,
        {"enabled": True, "db_path": str(tmp_path / "sum.db"),
         "vector": {"enabled": False}, "extract": {"enabled": False}},
    )
    ai = MagicMock()
    ai.generate_reply_with_intent = AsyncMock(return_value="收到～")
    ai.summarize_conversation = AsyncMock(return_value="早期摘要：客户叫Jun，偏好夜聊")
    sm = SkillManager(cm, ai)

    # 22 条历史（>16 阈值）→ 触发摘要，最近 10 条逐字保留
    hist = []
    for i in range(11):
        hist.append({"role": "user", "content": f"用户消息{i}"})
        hist.append({"role": "assistant", "content": f"客服回复{i}"})

    out = await sm.generate_inbox_draft(
        text="新的一句", chat_key="ulong", platform="telegram", history=hist,
    )
    assert out is not None
    _ctx = ai.generate_reply_with_intent.await_args.kwargs["user_context"]
    assert _ctx.get("_conversation_summary") == "早期摘要：客户叫Jun，偏好夜聊"
    assert len(_ctx.get("_conversation_history") or []) == 10  # 仅最近 10 条逐字
    assert ai.summarize_conversation.await_count == 1
    snap = _ms.get_metrics_store().get_inbox_draft_metrics()
    assert snap["total"].get("history_summarized") == 1

    # 第二轮：历史只多 2 条（<6）→ 复用缓存摘要，不再调 LLM
    hist2 = hist + [
        {"role": "user", "content": "又一句"},
        {"role": "assistant", "content": "嗯嗯"},
    ]
    out2 = await sm.generate_inbox_draft(
        text="再一句", chat_key="ulong", platform="telegram", history=hist2,
    )
    assert out2 is not None
    assert ai.summarize_conversation.await_count == 1  # 缓存命中，未重算


@pytest.mark.asyncio
async def test_e2e_bare_key_migration_then_draft_recall(tmp_path):
    """端到端联调：旧裸 key 记忆 → 迁移 → 真实草稿引擎能召回（命题闭环）。

    复现「换 canonical key 后旧记忆失联」的线上场景：原生产线把"名字"写在裸 key，
    新收件箱引擎按 canonical(`telegram:uid`) 读取 → 迁移前读不到；apply 迁移后，
    同一条 generate_inbox_draft 应命中记忆（memory_hit），验证整链贯通。
    """
    _reset_metrics()
    cm = await _make_cm(
        tmp_path,
        {"enabled": True, "db_path": str(tmp_path / "e2e_mem.db"),
         "vector": {"enabled": False}, "extract": {"enabled": False}},
    )
    ai = MagicMock()
    ai.generate_reply_with_intent = AsyncMock(return_value="你叫 Jun 呀～")
    sm = SkillManager(cm, ai)

    uid = "8005863121"
    # 旧原生 bot 产线：记忆写在裸 key（无 platform 前缀）
    sm._episodic_store.add_fact(uid, "用户的名字叫 Jun", source="user_stated")
    # 引擎读取用的 canonical key 与裸 key 不同 → 迁移前 canonical 下为空
    canon = sm._episodic_storage_key(uid, "", "telegram")
    assert canon == f"telegram:{uid}" and canon != uid
    assert sm._episodic_store.count(canon) == 0
    assert sm._episodic_store.count(uid) == 1

    # 迁移落地（与 CLI 同一函数）
    from src.utils.episodic_key_migration import apply_canonical_migration
    rep = apply_canonical_migration(sm._episodic_store, "telegram")
    assert rep["moved_rows"] >= 1
    assert sm._episodic_store.count(canon) == 1
    assert sm._episodic_store.count(uid) == 0

    # 端到端：同一句问名字，迁移后应命中记忆并把"Jun"喂进 user_context
    out = await sm.generate_inbox_draft(
        text="还记得我叫什么吗", chat_key=uid, platform="telegram",
        history=[{"role": "user", "content": "还记得我叫什么吗"}],
    )
    assert out is not None
    ctx = ai.generate_reply_with_intent.await_args.kwargs["user_context"]
    assert "Jun" in (ctx.get("_episodic_memory_text") or "")
    snap = _ms.get_metrics_store().get_inbox_draft_metrics()
    assert snap["total"].get("generated") == 1
    assert snap["total"].get("memory_hit") == 1


@pytest.mark.asyncio
async def test_episodic_key_health_and_migration_passthrough(tmp_path):
    """SkillManager 的 key 健康探针 + plan/apply 迁移 passthrough（后台一键修复用）。"""
    cm = await _make_cm(
        tmp_path,
        {"enabled": True, "db_path": str(tmp_path / "kh.db"),
         "vector": {"enabled": False}, "extract": {"enabled": False}},
    )
    ai = MagicMock()
    ai.generate_reply_with_intent = AsyncMock(return_value="hi")
    sm = SkillManager(cm, ai)

    sm._episodic_store.add_fact("123", "裸 key 事实")
    sm._episodic_store.add_fact("telegram:999", "canonical 事实")

    h = sm.episodic_key_health()
    assert h["enabled"] is True and h["bare_keys"] == 1

    plan = sm.episodic_plan_key_migration("telegram")
    assert plan["enabled"] is True and plan["candidates"] == 1
    # dry-run 不改库
    assert sm.episodic_key_health()["bare_keys"] == 1

    rep = sm.episodic_apply_key_migration("telegram")
    assert rep["enabled"] is True and rep["moved_rows"] == 1
    assert sm.episodic_key_health()["bare_keys"] == 0


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
