"""``recent_verify_cache`` 模块的单测 + 集成行为。"""
from __future__ import annotations

import time

import pytest

from src.integrations.messenger_rpa import recent_verify_cache as rvc


# ── 基本读写 ──────────────────────────────────────────────

def setup_function(_):
    rvc._reset()


def test_write_then_read_within_ttl():
    rvc.mark_verified("dev1", "Victor Zan")
    assert rvc.is_recently_verified("dev1", "Victor Zan", ttl_sec=60.0) is True


def test_read_returns_false_for_unknown():
    assert rvc.is_recently_verified("dev1", "Victor Zan") is False


def test_expires_past_ttl(monkeypatch):
    fake_ts = [time.time() - 1000.0]
    real_time = time.time
    monkeypatch.setattr(rvc.time, "time", lambda: fake_ts[0])
    rvc.mark_verified("dev1", "X")
    monkeypatch.setattr(rvc.time, "time", real_time)
    assert rvc.is_recently_verified("dev1", "X", ttl_sec=60.0) is False


def test_short_ttl_excludes_old_entry():
    rvc.mark_verified("dev1", "X")
    # TTL = 0 → 任何条目立刻过期
    assert rvc.is_recently_verified("dev1", "X", ttl_sec=0.0) is False


def test_normalization_strip_and_casefold():
    rvc.mark_verified("dev1", "  Victor Zan  ")
    assert rvc.is_recently_verified("dev1", "victor zan") is True
    assert rvc.is_recently_verified("dev1", "VICTOR ZAN") is True


def test_normalization_unicode_direction_marks():
    """方向控制字符（LRE/PDF）必须等同——thread_actions 也是这么处理的。"""
    rvc.mark_verified("dev1", "‪Victor Zan‬")
    assert rvc.is_recently_verified("dev1", "Victor Zan") is True


def test_isolation_per_serial():
    rvc.mark_verified("dev1", "X")
    assert rvc.is_recently_verified("dev2", "X") is False


def test_isolation_per_peer():
    rvc.mark_verified("dev1", "Alice")
    assert rvc.is_recently_verified("dev1", "Bob") is False


def test_send_succeeded_refreshes_ts(monkeypatch):
    fake_ts = [1000.0]
    monkeypatch.setattr(rvc.time, "time", lambda: fake_ts[0])
    rvc.mark_verified("dev1", "X")
    fake_ts[0] = 1080.0   # 80 秒后
    # 60s TTL 已过，应失效
    assert rvc.is_recently_verified("dev1", "X", ttl_sec=60.0) is False
    # 心跳续期
    rvc.send_succeeded("dev1", "X")
    # cache 现在应在
    assert rvc.is_recently_verified("dev1", "X", ttl_sec=60.0) is True


def test_invalidate_specific_peer():
    rvc.mark_verified("dev1", "Alice")
    rvc.mark_verified("dev1", "Bob")
    rvc.invalidate("dev1", "Alice")
    assert rvc.is_recently_verified("dev1", "Alice") is False
    assert rvc.is_recently_verified("dev1", "Bob") is True


def test_invalidate_whole_serial():
    rvc.mark_verified("dev1", "Alice")
    rvc.mark_verified("dev1", "Bob")
    rvc.mark_verified("dev2", "Alice")
    rvc.invalidate("dev1")
    assert rvc.is_recently_verified("dev1", "Alice") is False
    assert rvc.is_recently_verified("dev1", "Bob") is False
    assert rvc.is_recently_verified("dev2", "Alice") is True   # 别的 serial 不受影响


def test_empty_inputs_are_noop():
    rvc.mark_verified("", "Alice")
    rvc.mark_verified("dev1", "")
    rvc.mark_verified("dev1", "   ")
    assert len(rvc._peek_cache()) == 0


# ── 集成：thread_actions.verify_thread_title 走 cache ──────

THREAD_XML_VICTOR = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node class="android.widget.Button" content-desc="Victor Zan, 对话详情" bounds="[112,76][424,172]"/>
</hierarchy>
"""


def test_verify_thread_title_uses_recent_cache(monkeypatch):
    """cache 命中时不调 dump_view_tree（确认真的省了开销）。"""
    from src.integrations.messenger_rpa import thread_actions as ta

    # 先正常 verify 一次写入 cache
    monkeypatch.setattr(ta, "dump_view_tree", lambda s, **kw: THREAD_XML_VICTOR)
    r1 = ta.verify_thread_title("dev1", "Victor Zan")
    assert r1.ok is True
    assert r1.reason == "exact"

    # 第二次 verify：cache 应命中，根本不调 dump
    dump_calls = {"n": 0}

    def _no_call(*a, **kw):
        dump_calls["n"] += 1
        return THREAD_XML_VICTOR

    monkeypatch.setattr(ta, "dump_view_tree", _no_call)
    r2 = ta.verify_thread_title("dev1", "Victor Zan")
    assert r2.ok is True
    assert r2.reason == "recent_cache_hit"
    assert dump_calls["n"] == 0


def test_verify_thread_title_skips_cache_when_disabled(monkeypatch):
    """use_recent_cache=False 时即使 cache 有也不读——tap-then-verify 路径用。"""
    from src.integrations.messenger_rpa import thread_actions as ta

    rvc.mark_verified("dev1", "Victor Zan")  # cache 有

    monkeypatch.setattr(ta, "dump_view_tree", lambda s, **kw: THREAD_XML_VICTOR)
    r = ta.verify_thread_title("dev1", "Victor Zan", use_recent_cache=False)
    # 必须真测，reason='exact' 而非 cache_hit
    assert r.ok is True
    assert r.reason == "exact"


def test_verify_thread_title_mismatch_does_not_pollute_cache(monkeypatch):
    """verify 失败（mismatch）不应该写 cache——下次 pre_inbox 不能因此被骗。"""
    from src.integrations.messenger_rpa import thread_actions as ta

    XML_WRONG = (
        "<hierarchy><node class='android.widget.Button' "
        "content-desc='Someone Else, 对话详情' "
        "bounds='[112,76][424,172]'/></hierarchy>"
    )
    monkeypatch.setattr(ta, "dump_view_tree", lambda s, **kw: XML_WRONG)
    r = ta.verify_thread_title("dev1", "Victor Zan")
    assert r.ok is False
    # cache 不应有 Victor Zan 条目
    assert rvc.is_recently_verified("dev1", "Victor Zan") is False


def test_short_ttl_makes_cache_useless(monkeypatch):
    from src.integrations.messenger_rpa import thread_actions as ta

    monkeypatch.setattr(ta, "dump_view_tree", lambda s, **kw: THREAD_XML_VICTOR)
    ta.verify_thread_title("dev1", "Victor Zan")   # 写 cache
    # 下次用 ttl=0 查 → cache 没用
    r = ta.verify_thread_title("dev1", "Victor Zan", recent_cache_ttl_sec=0.0)
    # 真跑 dump 路径
    assert r.reason == "exact"
