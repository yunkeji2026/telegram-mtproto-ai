"""P52 — Copilot LLM 润色层单元测试。"""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.inbox.copilot_polisher import (
    _build_prompt,
    _parse_polish_response,
    apply_polish_results,
    get_polish_config,
    polish_suggestions,
    should_polish,
)


class TestPolishConfig:
    def test_default_disabled(self):
        assert get_polish_config(None)["enabled"] is False

    def test_reads_nested_config(self):
        cm = MagicMock()
        cm.config = {
            "ai": {
                "copilot_polish": {
                    "enabled": True,
                    "max_suggestions": 3,
                    "timeout_sec": 5,
                },
            },
        }
        cfg = get_polish_config(cm)
        assert cfg["enabled"] is True
        assert cfg["max_suggestions"] == 3


class TestShouldPolish:
    def test_disabled(self):
        assert should_polish(
            polish_requested=True, partial_text="", cfg={"enabled": False},
        ) is False

    def test_prefill_empty_ok(self):
        assert should_polish(
            polish_requested=True, partial_text="", cfg={"enabled": True},
        ) is True

    def test_typing_skipped(self):
        assert should_polish(
            polish_requested=True, partial_text="你好", cfg={"enabled": True},
        ) is False


class TestPromptAndParse:
    def test_build_prompt_includes_downgrade_hint(self):
        p = _build_prompt(
            [{"text": "草稿", "rationale": "共情"}],
            context={
                "stage": "warming",
                "stage_label": "试探/升温",
                "trigger": "churn",
                "recent_downgrade": True,
            },
            last_customer_msg="好累啊",
        )
        assert "降级" in p
        assert "好累" in p

    def test_parse_json_array(self):
        raw = json.dumps([{"index": 0, "text": "润色后的话"}])
        out = _parse_polish_response(raw, 1)
        assert out[0]["text"] == "润色后的话"

    def test_parse_markdown_block(self):
        raw = '```json\n[{"index":1,"text":"第二条"}]\n```'
        out = _parse_polish_response(raw, 2)
        assert out[0]["text"] == "第二条"

    def test_apply_polish_marks_source(self):
        orig = [{"text": "原话", "source": "empathy", "source_label": "共情"}]
        merged = apply_polish_results(orig, [{"index": "0", "text": "新话"}], [0])
        assert merged[0]["text"] == "新话"
        assert merged[0]["polished"] is True
        assert merged[0]["source"] == "copilot_polish"
        assert merged[0]["original_text"] == "原话"


class TestPolishSuggestions:
    @pytest.mark.asyncio
    async def test_skips_without_ai_client(self):
        r = await polish_suggestions(
            None,
            [{"text": "你好", "source": "empathy"}],
            context={"stage": "initial"},
        )
        assert r["polished"] is False

    @pytest.mark.asyncio
    async def test_skips_workflow_chain(self):
        ai = AsyncMock()
        r = await polish_suggestions(
            ai,
            [{"text": "工作链话术", "source": "workflow_chain"}],
            context={"stage": "warming"},
            cfg={"enabled": True, "max_suggestions": 2},
        )
        assert r["polished"] is False
        ai.chat.assert_not_called()

    @pytest.mark.asyncio
    async def test_polish_success(self):
        ai = AsyncMock()
        ai.chat = AsyncMock(return_value='[{"index":0,"text":"听起来你真的很累，愿意跟我说说吗？"}]')
        suggestions = [
            {"text": "我能理解你的感受，能多跟我说说吗？", "source": "empathy", "rationale": "共情"},
            {"text": "工作链", "source": "workflow_chain"},
        ]
        r = await polish_suggestions(
            ai,
            suggestions,
            context={"stage": "warming", "stage_label": "试探/升温", "trigger": "open"},
            last_customer_msg="好累",
            cfg={"enabled": True, "max_suggestions": 2, "timeout_sec": 5},
        )
        assert r["polished"] is True
        assert "累" in r["suggestions"][0]["text"]
        assert r["suggestions"][0]["polished"] is True

    @pytest.mark.asyncio
    async def test_timeout_fallback(self):
        async def slow(*_a, **_k):
            await asyncio.sleep(2)
            return "[]"

        ai = MagicMock()
        ai.chat = slow
        r = await polish_suggestions(
            ai,
            [{"text": "原话", "source": "empathy"}],
            context={"stage": "initial"},
            cfg={"enabled": True, "timeout_sec": 0.05, "max_suggestions": 1},
        )
        assert r["polished"] is False
        assert r["polish_error"] == "timeout"
        assert r["suggestions"][0]["text"] == "原话"
