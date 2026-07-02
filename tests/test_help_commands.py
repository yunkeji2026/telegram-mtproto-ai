"""help_commands 注册表 + help 页路由契约。"""
from src.web.help_commands import get_help_sections, section_is_pro_only


def test_get_help_sections_stable_keys():
    secs = get_help_sections()
    assert len(secs) >= 1
    keys = [s["key"] for s in secs]
    assert len(keys) == len(set(keys)), "section keys must be unique"
    for s in secs:
        assert s.get("tier")
        assert s.get("title_key")
        for c in s["commands"]:
            assert c.get("cmd", "").startswith("/")
            assert c.get("desc_key")


def test_section_is_pro_only_tier_enum():
    assert section_is_pro_only("admin") is True
    assert section_is_pro_only("ai") is True
    assert section_is_pro_only("basic") is False
    assert section_is_pro_only("channel") is False
