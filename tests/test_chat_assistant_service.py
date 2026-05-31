import pytest

from src.ai.chat_assistant_service import ChatAssistantService


@pytest.mark.asyncio
async def test_chat_assistant_detects_greeting_and_suggestions():
    svc = ChatAssistantService()
    rv = await svc.analyze(text="hi", messages=[{"text": "hi"}], chat={"language": "en"})
    data = rv.to_dict()
    assert data["intent"] == "打招呼"
    assert data["risk_level"] == "low"
    assert len(data["suggestions"]) == 3
    assert "客服" not in data["suggestions"][0]["text"]


@pytest.mark.asyncio
async def test_chat_assistant_high_risk_requires_review():
    svc = ChatAssistantService()
    rv = await svc.analyze(text="send me your bank password", messages=[], chat={})
    data = rv.to_dict()
    assert data["risk_level"] == "high"
    assert "privacy" in data["risk_reasons"]
    assert data["suggestions"][0]["risk_level"] == "high"

