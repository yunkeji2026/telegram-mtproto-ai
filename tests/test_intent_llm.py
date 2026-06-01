"""intent_llm + ChatAssistantService LLM 升级测试（Phase C1）。"""

import pytest

from src.ai.intent_llm import parse_json_lenient, llm_score
from src.ai.chat_assistant_service import ChatAssistantService
from src.inbox.store import InboxStore


def test_parse_json_lenient_plain():
    assert parse_json_lenient('{"intent":"提问"}') == {"intent": "提问"}


def test_parse_json_lenient_strips_code_fence():
    raw = '```json\n{"intent":"物流","risk_level":"low"}\n```'
    out = parse_json_lenient(raw)
    assert out["intent"] == "物流"


def test_parse_json_lenient_extracts_from_noise():
    raw = '好的，分析结果是 {"intent":"投诉"} 仅供参考'
    assert parse_json_lenient(raw)["intent"] == "投诉"


def test_parse_json_lenient_invalid_returns_none():
    assert parse_json_lenient("not json at all") is None
    assert parse_json_lenient("") is None


class _AIJson:
    model = "test-model"

    async def chat(self, prompt, overrides=None):
        return '{"intent":"物流","emotion":"平稳","risk_level":"low","summary":"问物流","order_no":"A123"}'


class _AIBadJson:
    async def chat(self, prompt, overrides=None):
        return "completely not json"


class _AIBoom:
    async def chat(self, prompt, overrides=None):
        raise RuntimeError("api down")


@pytest.mark.asyncio
async def test_llm_score_parses_normalized():
    out = await llm_score(_AIJson(), "where is my order", [], {})
    assert out["intent"] == "物流"
    assert out["order_no"] == "A123"


@pytest.mark.asyncio
async def test_llm_score_bad_json_returns_none():
    assert await llm_score(_AIBadJson(), "hi", [], {}) is None


@pytest.mark.asyncio
async def test_llm_score_exception_returns_none():
    assert await llm_score(_AIBoom(), "hi", [], {}) is None


@pytest.mark.asyncio
async def test_analyze_rule_only_when_use_llm_false():
    svc = ChatAssistantService(ai_client=_AIJson(), use_llm=False)
    res = await svc.analyze(text="where is my order")
    # use_llm=False → 不调 LLM，走规则版（intent 不会是 LLM 的"物流"）
    assert res.intent != "物流"


@pytest.mark.asyncio
async def test_analyze_llm_upgrades_intent():
    svc = ChatAssistantService(ai_client=_AIJson(), use_llm=True)
    res = await svc.analyze(text="where is my order")
    assert res.intent == "物流"
    assert getattr(res, "order_no", "") == "A123"


@pytest.mark.asyncio
async def test_analyze_llm_failure_falls_back_to_rule():
    svc = ChatAssistantService(ai_client=_AIBoom(), use_llm=True)
    res = await svc.analyze(text="hello")
    # LLM 炸了也不抛错，返回规则版
    assert res.intent == "打招呼"


@pytest.mark.asyncio
async def test_risk_only_goes_up_never_down():
    # 规则命中 money 硬底线 → high；LLM 说 low 也不能降
    class _AILowRisk:
        async def chat(self, prompt, overrides=None):
            return '{"intent":"闲聊","risk_level":"low"}'

    svc = ChatAssistantService(ai_client=_AILowRisk(), use_llm=True)
    res = await svc.analyze(text="请帮我转账到银行卡")
    assert res.risk_level == "high"
    assert "money" in res.risk_reasons


@pytest.mark.asyncio
async def test_analyze_saves_to_analysis_store(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    svc = ChatAssistantService(ai_client=_AIJson(), use_llm=True, analysis_store=store)
    await svc.analyze(text="where is my order", chat={"conversation_id": "line:a:c1"})
    latest = store.latest_analysis("line:a:c1")
    assert latest is not None
    assert latest["analyzer"] == "llm"
    assert latest["intent"] == "物流"
    store.close()


@pytest.mark.asyncio
async def test_analyze_backward_compatible_no_kwargs():
    # 不传 use_llm/analysis_store，与改造前一致
    svc = ChatAssistantService()
    res = await svc.analyze(text="hi", messages=[{"text": "hi"}], chat={"language": "en"})
    assert res.intent == "打招呼"
    assert len(res.suggestions) == 3
