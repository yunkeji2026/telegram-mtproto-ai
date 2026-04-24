"""kb_registry：同一路径返回同一 KnowledgeBaseStore 实例。"""

import asyncio

import yaml

import pytest

from src.utils.config_manager import ConfigManager
from src.utils.kb_registry import get_kb_store


@pytest.fixture
def tmp_config(tmp_path):
    cfg = {
        "telegram": {"api_id": "1", "api_hash": "x", "phone_number": "+1"},
        "ai": {"api_key": "k"},
        "skills": {"enabled": [], "cooldown": {}},
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(cfg, allow_unicode=True), encoding="utf-8")
    cm = ConfigManager(str(tmp_path / "config.yaml"))
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(cm.load())
    finally:
        loop.close()
    return cm


def test_singleton_same_resolved_path(tmp_config):
    """先创建库再按 require_exists 取，应为同一实例。"""
    a = get_kb_store(tmp_config, require_exists=False)
    b = get_kb_store(tmp_config, require_exists=True)
    assert a is not None and a is b


def test_none_when_missing_and_require_exists(tmp_config):
    assert get_kb_store(tmp_config, require_exists=True) is None


def test_create_db_when_require_false(tmp_config, tmp_path):
    assert not (tmp_path / "knowledge_base.db").exists()
    kb = get_kb_store(tmp_config, require_exists=False)
    assert kb is not None
    assert (tmp_path / "knowledge_base.db").exists()
