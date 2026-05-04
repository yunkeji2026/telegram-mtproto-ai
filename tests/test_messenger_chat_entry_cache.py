"""``MessengerRpaRunner`` 的 chat_entry_cache 单测。

只测缓存读写逻辑——不需要全副 runner 依赖（config_manager / skill_manager /
state_store 等）。用一个最小 stub 把 _cfg 和 _chat_entry_cache 注入到一个
裸对象上，然后调 runner 的方法。
"""
from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any, Dict


from src.integrations.messenger_rpa.runner import MessengerRpaRunner


def _make_runner_with_cfg(cfg: Dict[str, Any]) -> MessengerRpaRunner:
    """绕开 __init__ 直接造一个最小 runner（仅含被测需要的属性）。"""
    r = MessengerRpaRunner.__new__(MessengerRpaRunner)
    r._cfg = dict(cfg)
    r._chat_entry_cache = {}
    return r


def test_record_and_read_back():
    r = _make_runner_with_cfg({})
    r._record_chat_entry("dev1", "Victor Zan", 360, 600, source="search:xml:row0")
    got = r._cached_chat_entry("dev1", "Victor Zan")
    assert got is not None
    x, y, ts, src = got
    assert (x, y) == (360, 600)
    assert src == "search:xml:row0"
    assert time.time() - ts < 1.0


def test_cache_isolated_per_serial_and_name():
    r = _make_runner_with_cfg({})
    r._record_chat_entry("dev1", "A", 10, 20, source="s")
    r._record_chat_entry("dev2", "A", 30, 40, source="s")
    r._record_chat_entry("dev1", "B", 50, 60, source="s")

    assert r._cached_chat_entry("dev1", "A")[:2] == (10, 20)
    assert r._cached_chat_entry("dev2", "A")[:2] == (30, 40)
    assert r._cached_chat_entry("dev1", "B")[:2] == (50, 60)
    assert r._cached_chat_entry("devX", "A") is None


def test_cache_disabled_via_config():
    r = _make_runner_with_cfg({"send_to_chat_entry_cache": False})
    r._record_chat_entry("dev1", "X", 1, 2, source="s")
    # 配置关时即使强写也不应该被读出
    assert r._cached_chat_entry("dev1", "X") is None


def test_cache_expires_past_ttl(monkeypatch):
    r = _make_runner_with_cfg({"chat_entry_cache_ttl_sec": 60})
    # 用 monkeypatch 把 record 时的 time 倒回 100 秒前
    fake_time = [time.time() - 100.0]
    real_time = time.time

    monkeypatch.setattr(
        "src.integrations.messenger_rpa.runner.time.time",
        lambda: fake_time[0],
    )
    r._record_chat_entry("dev1", "Z", 1, 1, source="s")

    # 时间回到现在
    monkeypatch.setattr(
        "src.integrations.messenger_rpa.runner.time.time",
        real_time,
    )
    # TTL 60s 已过 → 应被过期清掉
    assert r._cached_chat_entry("dev1", "Z") is None
    # 过期项也应从字典里清掉（防内存泄漏）
    assert ("dev1", "Z") not in r._chat_entry_cache


def test_cache_empty_chat_name_is_noop():
    r = _make_runner_with_cfg({})
    r._record_chat_entry("dev1", "", 1, 1, source="s")
    r._record_chat_entry("dev1", "   ", 1, 1, source="s")
    assert len(r._chat_entry_cache) == 0
    assert r._cached_chat_entry("dev1", "") is None


def test_invalidate_removes_entry():
    r = _make_runner_with_cfg({})
    r._record_chat_entry("dev1", "Q", 7, 8, source="s")
    assert r._cached_chat_entry("dev1", "Q") is not None
    r._invalidate_chat_entry("dev1", "Q")
    assert r._cached_chat_entry("dev1", "Q") is None
    # invalidate 不存在的 key 不报错
    r._invalidate_chat_entry("dev1", "NoSuch")


def test_chat_name_normalized_via_strip():
    r = _make_runner_with_cfg({})
    r._record_chat_entry("dev1", "  Victor Zan  ", 100, 200, source="s")
    # strip 后应该能查到（同一逻辑名）
    got = r._cached_chat_entry("dev1", "Victor Zan")
    assert got is not None
    assert got[:2] == (100, 200)


# ── _tap_chat_row 返回签名（P1 重构）────────────────────────

def test_tap_chat_row_returns_tuple_xy_source(monkeypatch):
    """P1 重构：_tap_chat_row 必须返回 (x, y, source)，否则 send_to_chat_name
    的 cache 写入 unpack 会爆。"""
    import src.integrations.messenger_rpa.runner as runner_mod
    from src.integrations.messenger_rpa.runner import UnreadChat

    r = _make_runner_with_cfg({
        "use_ui_hierarchy_tap": False,   # 跳过 UI XML 路径
        "auto_calibrate": False,           # 跳过校准路径
    })
    r._screen_wh_cache = {}
    r._calib_cache = {}

    captured = {}

    def _fake_tap(serial, x, y):
        captured["xy"] = (x, y)

    monkeypatch.setattr(runner_mod.adb, "input_tap", _fake_tap)

    chat = UnreadChat(
        name="Victor Zan", preview="hi", time="now",
        row_index=2, y_percent=0.3, quality_hint="vision",
        score=80.0, skip_inbox_tap=False,
    )
    out = r._tap_chat_row("dev1", (720, 1600), chat)
    assert isinstance(out, tuple) and len(out) == 3
    x, y, src = out
    assert isinstance(x, int) and isinstance(y, int)
    assert isinstance(src, str) and src   # 非空
    # 真的发了 tap
    assert captured["xy"] == (x, y)
    # stories-aware：row_index=2 → adjusted_row=1
    assert "stories_aware" in src
