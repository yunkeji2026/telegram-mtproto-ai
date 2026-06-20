"""L2c-1：译后文档临时令牌存储单测。

覆盖：put→take 往返 + 一次性消费（take 后即删）+ TTL 过期 + 条目数上限逐出 +
总字节上限逐出 + 不存在/过期 token 返回 None + 单例。
"""
import time

from src.web.translated_file_store import (
    TranslatedFileStore,
    get_translated_file_store,
)


def test_put_take_roundtrip():
    s = TranslatedFileStore()
    tok = s.put(b"hello", "a.docx", "application/x")
    e = s.take(tok)
    assert e is not None
    assert e.data == b"hello" and e.filename == "a.docx" and e.content_type == "application/x"


def test_take_is_one_time():
    s = TranslatedFileStore()
    tok = s.put(b"x", "a.docx", "ct")
    assert s.take(tok) is not None
    assert s.take(tok) is None  # 第二次取不到（已删）


def test_unknown_token_none():
    s = TranslatedFileStore()
    assert s.take("nope") is None
    assert s.take("") is None


def test_ttl_expiry():
    s = TranslatedFileStore(ttl=0.05)
    tok = s.put(b"x", "a.docx", "ct")
    time.sleep(0.08)
    assert s.take(tok) is None  # 已过期


def test_max_entries_evicts_oldest():
    s = TranslatedFileStore(max_entries=2)
    t1 = s.put(b"1", "1.docx", "ct")
    time.sleep(0.01)
    t2 = s.put(b"2", "2.docx", "ct")
    time.sleep(0.01)
    t3 = s.put(b"3", "3.docx", "ct")  # 触发逐出最早的 t1
    assert s.take(t1) is None
    assert s.take(t2) is not None and s.take(t3) is not None


def test_max_total_bytes_evicts():
    s = TranslatedFileStore(max_total_bytes=10)
    t1 = s.put(b"a" * 6, "1.docx", "ct")
    time.sleep(0.01)
    t2 = s.put(b"b" * 6, "2.docx", "ct")  # 6+6>10 → 逐出 t1
    assert s.take(t1) is None
    assert s.take(t2) is not None


def test_count_excludes_expired():
    s = TranslatedFileStore(ttl=0.05)
    s.put(b"x", "a.docx", "ct")
    assert s.count() == 1
    time.sleep(0.08)
    assert s.count() == 0


def test_singleton_identity():
    assert get_translated_file_store() is get_translated_file_store()
