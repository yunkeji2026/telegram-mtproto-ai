"""
process_message 路径：通道指标咨询跳过 KB 时注入 _channel_metrics_live_only，且不实例化 KB 检索。
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from src.skills.skill_manager import SkillManager
from src.utils.config_manager import ConfigManager
from src.utils.kb_store import KnowledgeBaseStore


def _write_min_config(tmp_path: Path) -> ConfigManager:
    cfg = {
        "telegram": {"api_id": "1", "api_hash": "x", "phone_number": "+1"},
        "ai": {"api_key": "k"},
        "skills": {
            "enabled": ["channel_info"],
            "cooldown": {
                "global": 0,
                "per_user": 0,
                "per_content": 0,
                "per_chat_user": 0,
            },
        },
        "intent": {
            "keywords": {
                "channel_info": ["费率", "限额", "通道", "成功率", "手续费"],
                "price_check": ["价格", "多少钱"],
            },
            "patterns": {},
        },
        "reply": {},
        "context_store": {"ttl_days": 30},
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(cfg, allow_unicode=True), encoding="utf-8")
    (tmp_path / "templates.yaml").write_text(
        yaml.dump({"greeting": ["hi"]}, allow_unicode=True), encoding="utf-8"
    )
    (tmp_path / "exchange_rates.yaml").write_text(
        yaml.dump({"channels": {}}, allow_unicode=True), encoding="utf-8"
    )
    cm = ConfigManager(str(tmp_path / "config.yaml"))
    # 勿用 asyncio.run：会关闭默认循环，导致同会话内 conftest 等测试报错
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(cm.load())
    finally:
        loop.close()
    return cm


@pytest.fixture
def skill_manager_with_channel_info(tmp_path):
    """真实 SkillManager + 异步 mock 的 channel_info，避免调用真实 AI。"""
    cm = _write_min_config(tmp_path)
    KnowledgeBaseStore(tmp_path / "knowledge_base.db")

    ai = MagicMock()
    ai.embed = AsyncMock(return_value=[[0.01] * 16])

    # Register payment hook so domain-specific KB skip logic is active
    from src.hooks.registry import HookRegistry
    from domains.payment.hooks import PaymentDomainHook
    HookRegistry.reset()
    HookRegistry.get_instance().register(PaymentDomainHook(config=cm), "payment")

    sm = SkillManager(cm, ai)
    ch = AsyncMock()
    ch.execute = AsyncMock(return_value="ok")
    sm.skills["channel_info"] = ch

    yield sm
    HookRegistry.reset()


@pytest.mark.asyncio
async def test_skip_kb_sets_live_only_and_does_not_run_bm25_search(
    skill_manager_with_channel_info, tmp_path
):
    """跳过 KB 时仍可能因 F1 低分修复等实例化 KnowledgeBaseStore，但主流程不应调用 search。"""
    sm = skill_manager_with_channel_info
    with patch.object(KnowledgeBaseStore, "search") as mock_search:
        mock_search.return_value = {"entries": [], "search_mode": "bm25"}
        out = await sm.process_message(
            "今天手续费多少",
            "user_kb_skip",
            {"_trigger_path": "mention", "chat_id": 1},
        )
    assert out == "ok"
    ctx = sm._get_user_context("user_kb_skip")
    assert ctx.get("_channel_metrics_live_only") is True
    assert mock_search.call_count == 0


@pytest.mark.asyncio
async def test_quota_query_runs_kb_search_not_live_only_flag(
    skill_manager_with_channel_info, tmp_path
):
    sm = skill_manager_with_channel_info
    search_calls = []

    def _track_search(self, *args, **kwargs):
        search_calls.append((args, kwargs))
        return {"entries": [], "search_mode": "bm25"}

    with patch.object(KnowledgeBaseStore, "search", _track_search):
        out = await sm.process_message(
            "限额多少",
            "user_quota",
            {"_trigger_path": "mention", "chat_id": 2},
        )
    assert out == "ok"
    assert search_calls, "应进入 KB 检索分支并调用 search"
    ctx = sm._get_user_context("user_quota")
    assert ctx.get("_channel_metrics_live_only") is not True
