"""bootstrap_contacts_subsystem 单元测试。"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.contacts import bootstrap_contacts_subsystem


REPO_CONFIG = Path(__file__).resolve().parent.parent / "config"


class _FakeConfig:
    def __init__(self, cfg: dict):
        self.config = cfg


@pytest.fixture
def cfg_dir(tmp_path):
    # 复制必要的 yaml 到 tmp 目录（HandoffRenderer / Compliance 需要）
    d = tmp_path / "config"
    d.mkdir()
    shutil.copy(REPO_CONFIG / "handoff_scripts.yaml", d / "handoff_scripts.yaml")
    shutil.copy(REPO_CONFIG / "handoff_compliance.yaml", d / "handoff_compliance.yaml")
    yield d


class TestFeatureFlag:
    def test_disabled_returns_none(self, cfg_dir):
        cfg = _FakeConfig({"contacts": {"enabled": False}})
        assert bootstrap_contacts_subsystem(cfg, cfg_dir) is None

    def test_missing_section_returns_none(self, cfg_dir):
        cfg = _FakeConfig({})
        assert bootstrap_contacts_subsystem(cfg, cfg_dir) is None

    def test_none_config_returns_none(self, cfg_dir):
        assert bootstrap_contacts_subsystem(None, cfg_dir) is None


class TestEnabled:
    def test_minimal_enabled(self, cfg_dir):
        cfg = _FakeConfig({"contacts": {"enabled": True}})
        sub = bootstrap_contacts_subsystem(cfg, cfg_dir)
        assert sub is not None
        assert sub.store is not None
        assert sub.gateway is not None
        assert sub.hooks is not None
        # 可选服务全部就位（yaml 都能读）
        assert sub.renderer is not None
        assert sub.compliance is not None
        assert sub.limiter is not None
        assert sub.intimacy_engine is not None
        assert sub.readiness_scorer is not None
        assert sub.reactivation is not None
        sub.close()

    def test_db_created(self, cfg_dir):
        cfg = _FakeConfig({"contacts": {"enabled": True}})
        sub = bootstrap_contacts_subsystem(cfg, cfg_dir)
        assert sub is not None
        # contacts.db 默认建到 cfg_dir 下
        db_files = list(cfg_dir.glob("contacts.db*"))
        assert any(f.name.startswith("contacts.db") for f in db_files)
        sub.close()

    def test_config_values_applied(self, cfg_dir):
        cfg = _FakeConfig({"contacts": {
            "enabled": True,
            "daily_cap": 7,
            "global_cap": 50,
            "token_ttl_hours": 48,
            "readiness_threshold": 60,
            "line_ids_by_account": {"acc-A": "@custom_line"},
        }})
        sub = bootstrap_contacts_subsystem(cfg, cfg_dir)
        # limiter 的 cap
        assert sub.limiter._daily_cap == 7
        assert sub.limiter._global_cap == 50
        # token ttl
        assert sub.handoff_svc._ttl == 48 * 3600
        # readiness threshold
        assert sub.readiness_scorer._threshold == 60.0
        # line_id provider 正确
        assert sub.gateway._line_id_provider("acc-A") == "@custom_line"
        assert sub.gateway._line_id_provider("unknown_acc") == "@our_line"
        sub.close()


class TestDictStyleConfig:
    def test_plain_dict_works(self, cfg_dir):
        # 直接传 dict 也能用
        sub = bootstrap_contacts_subsystem(
            {"contacts": {"enabled": True}}, cfg_dir,
        )
        assert sub is not None
        sub.close()
