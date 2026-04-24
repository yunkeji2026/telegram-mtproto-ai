"""greeting_lexicon：多语种寒暄与业务句排除。"""
from src.utils.greeting_lexicon import (
    is_greeting_message,
    is_standalone_zai_query,
    merge_greeting_substrings,
)


def test_ha_lou_and_multilingual():
    assert is_greeting_message("哈喽") is True
    assert is_greeting_message("嗨喽呀") is True
    assert is_greeting_message("hola") is True
    assert is_greeting_message("bonjour") is True
    assert is_greeting_message("good morning") is True
    assert is_greeting_message("assalamualaikum") is True


def test_not_greeting_when_business():
    assert is_greeting_message("你好 EP 通道") is False
    assert is_greeting_message("hola order 123") is False
    assert is_greeting_message("hi 查单") is False


def test_merge_greeting_substrings():
    m = merge_greeting_substrings(["自定义招呼"])
    assert "自定义招呼" in m
    assert any("good morning" in x.lower() for x in m)
    assert any(x == "hi" for x in m)


def test_standalone_zai_only():
    assert is_standalone_zai_query("在") is True
    assert is_standalone_zai_query("在。") is True
    assert is_standalone_zai_query("在？") is True
    assert is_standalone_zai_query("在吗") is False
    assert is_standalone_zai_query("现在") is False
    assert is_standalone_zai_query("正在付款") is False
    assert is_standalone_zai_query("在线吗") is False
