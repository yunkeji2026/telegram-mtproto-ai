"""L2c-2：文档翻译待处理作业暂存单测。

覆盖：create→take 往返 + 一次性消费 + TTL 过期 + 条目上限逐出 + 未知 token None + 单例。
"""
import time

from src.web.document_job_store import DocumentJobStore, get_document_job_store


def test_create_take_roundtrip():
    s = DocumentJobStore()
    tok = s.create({"kind": "docx", "data": b"x"})
    p = s.take(tok)
    assert p is not None and p["kind"] == "docx" and p["data"] == b"x"


def test_take_one_time():
    s = DocumentJobStore()
    tok = s.create({"a": 1})
    assert s.take(tok) is not None
    assert s.take(tok) is None


def test_unknown_token_none():
    s = DocumentJobStore()
    assert s.take("nope") is None
    assert s.take("") is None


def test_ttl_expiry():
    s = DocumentJobStore(ttl=0.05)
    tok = s.create({"a": 1})
    time.sleep(0.08)
    assert s.take(tok) is None


def test_max_jobs_evicts_oldest():
    s = DocumentJobStore(max_jobs=2)
    t1 = s.create({"n": 1}); time.sleep(0.01)
    t2 = s.create({"n": 2}); time.sleep(0.01)
    t3 = s.create({"n": 3})
    assert s.take(t1) is None
    assert s.take(t2) is not None and s.take(t3) is not None


def test_singleton_identity():
    assert get_document_job_store() is get_document_job_store()
