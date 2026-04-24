"""HandoffCompliance 单元测试。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.skills.handoff_compliance import HandoffComplianceChecker


CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "handoff_compliance.yaml"


@pytest.fixture
def checker():
    return HandoffComplianceChecker(config_path=CONFIG_PATH)


class TestBasic:
    def test_clean_text_allowed(self, checker):
        r = checker.check("加我 LINE 嘛 alice123，告诉我 m7ra2k 我认你")
        assert r.allowed
        assert r.blocked_hits == []
        assert r.reason == "ok"

    def test_empty_blocked(self, checker):
        r = checker.check("")
        assert r.allowed is False
        assert r.length_issue == "too_short"


class TestBlockedKeywords:
    def test_blocked_word_rejects(self, checker):
        r = checker.check("加我 LINE 然后来打款给我嘛")
        assert r.allowed is False
        assert "打款" in r.blocked_hits
        assert r.reason == "blocked_keyword_hit"

    def test_multiple_blocked(self, checker):
        r = checker.check("付款下单一起来")
        assert r.allowed is False
        assert set(r.blocked_hits) >= {"付款", "下单"}


class TestWarn:
    def test_warn_still_allowed(self, checker):
        r = checker.check("加我 LINE abc 说 xyz 吧 客服这边不方便")
        # 命中 warn "客服" 但 blocked 没触发 → 允许 + 打标
        assert r.allowed
        assert "客服" in r.warn_hits
        assert r.reason == "passed_with_warnings"


class TestLength:
    def test_too_short(self, checker):
        r = checker.check("加我")
        assert r.allowed is False
        assert r.length_issue == "too_short"

    def test_too_long(self, checker):
        r = checker.check("a" * 300)
        assert r.allowed is False
        assert r.length_issue == "too_long"


class TestCustomConfig:
    def test_explicit_kwlist(self):
        c = HandoffComplianceChecker(
            blocked_keywords=["zzz"], min_length=3, max_length=100)
        assert c.check("hello world").allowed is True
        assert c.check("has zzz in it").allowed is False

    def test_case_insensitive(self):
        c = HandoffComplianceChecker(
            blocked_keywords=["BadWord"], min_length=3, max_length=100)
        assert c.check("this has BADWORD here").allowed is False
        assert c.check("badword lowercase").allowed is False
