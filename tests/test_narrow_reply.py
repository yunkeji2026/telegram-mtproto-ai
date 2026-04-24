"""narrow_reply：仅允许客服在线 / 通道·限额·成功率 相关意图。"""
import asyncio

import pytest
import yaml
from pathlib import Path

from src.skills.skill_manager import SkillManager
from src.utils.config_manager import ConfigManager


def _cfg_mgr_with_narrow(tmp_path: Path, narrow: dict) -> ConfigManager:
    cfg = {
        "telegram": {"api_id": "1", "api_hash": "x", "phone_number": "+1"},
        "ai": {"api_key": "k"},
        "skills": {
            "enabled": ["channel_info", "greeting", "order_query"],
            "cooldown": {
                "global": 0,
                "per_user": 0,
                "per_content": 0,
                "per_chat_user": 0,
            },
        },
        "intent": {
            "keywords": {
                "channel_info": ["通道", "成功率", "限额"],
                "greeting": ["你好", "hi"],
                "order_query": ["订单", "单号"],
            },
            "patterns": {},
        },
        "reply": {},
        "context_store": {"ttl_days": 30},
        "narrow_reply": narrow,
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(cfg, allow_unicode=True), encoding="utf-8")
    (tmp_path / "templates.yaml").write_text(
        yaml.dump({"greeting": ["x"]}, allow_unicode=True), encoding="utf-8"
    )
    (tmp_path / "exchange_rates.yaml").write_text(
        yaml.dump({"channels": {}}, allow_unicode=True), encoding="utf-8"
    )
    cm = ConfigManager(str(tmp_path / "config.yaml"))
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(cm.load())
    finally:
        loop.close()
    return cm


@pytest.fixture
def sm_narrow_on(tmp_path):
    cm = _cfg_mgr_with_narrow(
        tmp_path,
        {
            "enabled": True,
            "allowed_intents": ["greeting", "channel_info", "status_check"],
            "cs_online_substrings": ["在吗", "客服"],
            "greeting_substrings": ["你好", "hi"],
            "channel_topic_substrings": ["通道", "成功率", "限额", "交易", "跑单", "正常"],
            "deny_substrings": ["订单号", "查单", "单号"],
            "inherit_followup_seconds": 120,
        },
    )
    sm = object.__new__(SkillManager)
    sm.config = cm
    sm._narrow_reply_cfg = dict(cm.config.get("narrow_reply") or {})
    return sm


def test_narrow_allows_cs_online_and_channel_topic(sm_narrow_on):
    sm = sm_narrow_on
    uc = {"last_message_time": 0}
    assert sm._narrow_reply_allows("客服在吗", "greeting", "", uc) is True
    assert sm._narrow_reply_allows("今天通道稳定吗", "channel_info", "", uc) is True
    assert sm._narrow_reply_allows("成功率多少", "channel_info", "", uc) is True
    # 截图类话术：意图已为 channel_info 时须命中 channel_topic 子串才放行
    assert sm._narrow_reply_allows("现在可以交易吗", "channel_info", "", uc) is True
    assert sm._narrow_reply_allows("现在可以跑单吗？", "channel_info", "", uc) is True
    assert sm._narrow_reply_allows("交易正常吗？", "channel_info", "", uc) is True


def test_narrow_allows_simple_greeting(sm_narrow_on):
    sm = sm_narrow_on
    uc = {"last_message_time": 0}
    assert sm._narrow_reply_allows("你好啊", "greeting", "", uc) is True
    assert sm._narrow_reply_greeting_allows("在", {}) is True
    assert sm._narrow_reply_greeting_allows("在？", {}) is True


def test_narrow_blocks_order_intent(sm_narrow_on):
    sm = sm_narrow_on
    uc = {"last_message_time": 0}
    assert sm._narrow_reply_allows("查订单 123", "order_query", "", uc) is False


def test_narrow_blocks_deny_substring(sm_narrow_on):
    sm = sm_narrow_on
    uc = {"last_message_time": 0}
    assert sm._narrow_reply_allows("单号 888 通道", "channel_info", "", uc) is False


def test_narrow_disabled_allows_all_intents(tmp_path):
    cm = _cfg_mgr_with_narrow(tmp_path, {"enabled": False})
    sm = object.__new__(SkillManager)
    sm.config = cm
    sm._narrow_reply_cfg = dict(cm.config.get("narrow_reply") or {})
    uc = {"last_message_time": 0}
    assert sm._narrow_reply_allows("随便聊", "order_query", "", uc) is True
