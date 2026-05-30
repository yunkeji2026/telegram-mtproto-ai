"""S5: Tests for CrossPlatformIdentity module."""
import tempfile
from pathlib import Path
import pytest
from src.utils.cross_platform_identity import CrossPlatformIdentity


@pytest.fixture
def cpi(tmp_path):
    return CrossPlatformIdentity(tmp_path / "bot.db")


class TestResolve:
    def test_new_uid_gets_default_canonical(self, cpi):
        c = cpi.resolve("telegram", "12345")
        assert c == "telegram:12345"

    def test_same_uid_returns_same_canonical(self, cpi):
        c1 = cpi.resolve("line_rpa", "Uabc")
        c2 = cpi.resolve("line_rpa", "Uabc")
        assert c1 == c2

    def test_different_platforms_different_defaults(self, cpi):
        ct = cpi.resolve("telegram", "999")
        cl = cpi.resolve("line_rpa", "999")
        assert ct != cl

    def test_empty_platform_returns_uid(self, cpi):
        assert cpi.resolve("", "abc") == "abc"

    def test_empty_uid_returns_empty(self, cpi):
        assert cpi.resolve("telegram", "") == ""


class TestLink:
    def test_link_shares_canonical(self, cpi):
        c = cpi.link("telegram", "111", "line_rpa", "Uaaa")
        assert c == "telegram:111"
        assert cpi.resolve("line_rpa", "Uaaa") == "telegram:111"

    def test_linked_uid_is_persistent(self, cpi):
        cpi.link("telegram", "222", "messenger_rpa", "fb_222")
        assert cpi.resolve("messenger_rpa", "fb_222") == "telegram:222"

    def test_link_returns_canonical_of_a(self, cpi):
        # Pre-create a with custom canonical by linking it first
        cpi.link("telegram", "A", "line_rpa", "B")
        # Now link B to whatsapp C — canon should remain telegram:A
        c = cpi.link("telegram", "A", "whatsapp_rpa", "C")
        assert c == "telegram:A"
        assert cpi.resolve("whatsapp_rpa", "C") == "telegram:A"


class TestUnlink:
    def test_unlink_restores_own_canonical(self, cpi):
        cpi.link("telegram", "333", "line_rpa", "Ubbb")
        assert cpi.resolve("line_rpa", "Ubbb") == "telegram:333"
        new_c = cpi.unlink("line_rpa", "Ubbb")
        assert new_c == "line_rpa:Ubbb"
        assert cpi.resolve("line_rpa", "Ubbb") == "line_rpa:Ubbb"

    def test_unlink_leaves_other_party_intact(self, cpi):
        cpi.link("telegram", "444", "line_rpa", "Uccc")
        cpi.unlink("line_rpa", "Uccc")
        assert cpi.resolve("telegram", "444") == "telegram:444"


class TestListAndGetByCanonical:
    def test_list_returns_all_rows(self, cpi):
        cpi.resolve("telegram", "X1")
        cpi.resolve("line_rpa", "X2")
        rows = cpi.list_all()
        platforms = [r[0] for r in rows]
        assert "telegram" in platforms
        assert "line_rpa" in platforms

    def test_get_by_canonical_finds_linked(self, cpi):
        cpi.link("telegram", "T1", "line_rpa", "L1")
        pairs = cpi.get_by_canonical("telegram:T1")
        platforms = [p[0] for p in pairs]
        assert "telegram" in platforms
        assert "line_rpa" in platforms
