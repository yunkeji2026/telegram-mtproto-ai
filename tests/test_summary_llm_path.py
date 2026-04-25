"""Phase 2 — `_summarize_history_with_fallback` 行为单测。

不实例化完整 SkillManager（init 太重），用 __new__ 绕过 init，手动注入依赖。
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.skills.skill_manager import SkillManager


def _make_sm(*, ai_client=None, summarize_with_llm: bool = True) -> SkillManager:
    sm = SkillManager.__new__(SkillManager)
    sm.config = MagicMock()
    sm.config.config = {"ai": {"summarize_with_llm": summarize_with_llm}}
    sm.ai_client = ai_client
    # logger 是 LoggerMixin 提供的 property，不必/不能直接 set
    return sm


_OLD_MSGS = [
    {"role": "user", "content": "我想查订单 ORDER12345"},
    {"role": "assistant", "content": "已为您查询，订单状态正常"},
    {"role": "user", "content": "能帮我看下额度吗"},
    {"role": "assistant", "content": "您当前额度 50000，可用 30000"},
]


@pytest.mark.asyncio
async def test_uses_llm_when_available_and_enabled():
    ai = MagicMock()
    ai.summarize_conversation = AsyncMock(return_value="LLM 生成的连贯摘要：客户问了订单和额度。")
    sm = _make_sm(ai_client=ai, summarize_with_llm=True)

    result = await sm._summarize_history_with_fallback(_OLD_MSGS)

    ai.summarize_conversation.assert_awaited_once()
    assert "LLM 生成" in result


@pytest.mark.asyncio
async def test_falls_back_to_rule_based_when_llm_raises():
    ai = MagicMock()
    ai.summarize_conversation = AsyncMock(side_effect=RuntimeError("LLM down"))
    sm = _make_sm(ai_client=ai, summarize_with_llm=True)

    result = await sm._summarize_history_with_fallback(_OLD_MSGS)

    # rule-based 输出含「提及:」或「结论」前缀
    assert ("提及" in result) or ("结论" in result)


@pytest.mark.asyncio
async def test_falls_back_to_rule_based_when_llm_returns_empty():
    ai = MagicMock()
    ai.summarize_conversation = AsyncMock(return_value="   ")
    sm = _make_sm(ai_client=ai, summarize_with_llm=True)

    result = await sm._summarize_history_with_fallback(_OLD_MSGS)

    # rule-based 输出（"提及" / "结论" / 空消息提示）
    assert isinstance(result, str) and len(result) > 0
    assert "LLM 生成" not in result


@pytest.mark.asyncio
async def test_uses_rule_based_when_config_disables_llm():
    ai = MagicMock()
    ai.summarize_conversation = AsyncMock(return_value="不应被调用")
    sm = _make_sm(ai_client=ai, summarize_with_llm=False)

    result = await sm._summarize_history_with_fallback(_OLD_MSGS)

    ai.summarize_conversation.assert_not_called()
    assert "不应被调用" not in result


@pytest.mark.asyncio
async def test_uses_rule_based_when_no_ai_client():
    sm = _make_sm(ai_client=None, summarize_with_llm=True)
    result = await sm._summarize_history_with_fallback(_OLD_MSGS)
    assert isinstance(result, str) and len(result) > 0


@pytest.mark.asyncio
async def test_truncates_to_300_chars():
    ai = MagicMock()
    ai.summarize_conversation = AsyncMock(return_value="A" * 1000)
    sm = _make_sm(ai_client=ai, summarize_with_llm=True)

    result = await sm._summarize_history_with_fallback(_OLD_MSGS)

    assert len(result) <= 300
