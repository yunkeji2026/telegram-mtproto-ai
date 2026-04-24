"""messenger_rpa.thread_actions 的异步单测（不依赖真机）。

通过 monkeypatch `dump_view_tree` 和 `inject_text` 把外部副作用剥掉，
只测"给 view tree X 时 我们如何判断"。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Optional

import pytest

from src.integrations.messenger_rpa import thread_actions as ta


THREAD_XML_KEYBOARD_OPEN = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node class="android.widget.Button" content-desc="返回" bounds="[8,76][104,172]"/>
  <node class="android.widget.Button" content-desc="Jane Doe, 对话详情" bounds="[112,76][424,172]"/>
  <node class="android.widget.EditText" text="hello world" content-desc="输入消息" bounds="[80,894][568,978]"/>
  <node class="android.widget.Button" content-desc="发送" bounds="[640,876][720,996]"/>
</hierarchy>
"""

THREAD_XML_KEYBOARD_CLOSED_EMPTY = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node class="android.widget.Button" content-desc="Jane Doe, 对话详情" bounds="[112,76][424,172]"/>
  <node class="android.widget.EditText" text="发消息" content-desc="输入消息" bounds="[320,1404][560,1438]"/>
</hierarchy>
"""

THREAD_XML_WRONG_PEER = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node class="android.widget.Button" content-desc="Someone Else, 对话详情" bounds="[112,76][424,172]"/>
</hierarchy>
"""

THREAD_XML_VICTOR_SHORT_TITLE = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node class="android.widget.Button" content-desc="Victor, 对话详情" bounds="[112,76][424,172]"/>
</hierarchy>
"""

THREAD_XML_BUBBLE_AFTER_SEND = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node class="android.widget.Button" content-desc="Jane Doe, 对话详情" bounds="[112,76][424,172]"/>
  <node class="android.view.ViewGroup" text="hello world" bounds="[400,800][700,860]"/>
  <node class="android.widget.ImageView" content-desc="Jane Doe已读" bounds="[648,868][680,900]"/>
  <node class="android.widget.EditText" text="发消息" content-desc="输入消息" bounds="[320,1404][560,1438]"/>
</hierarchy>
"""


# ── verify_thread_title (U1) ──────────────────────────────

def test_verify_thread_title_exact(monkeypatch):
    monkeypatch.setattr(
        ta, "dump_view_tree", lambda s, **kw: THREAD_XML_KEYBOARD_OPEN,
    )
    r = ta.verify_thread_title("abc", "Jane Doe")
    assert r.ok is True
    assert r.actual == "Jane Doe"
    assert r.reason == "exact"


def test_verify_thread_title_substr(monkeypatch):
    monkeypatch.setattr(
        ta, "dump_view_tree", lambda s, **kw: THREAD_XML_KEYBOARD_OPEN,
    )
    # 用户配置的 peer 是 "Jane"，顶栏是 "Jane Doe" → substr 命中
    r = ta.verify_thread_title("abc", "Jane")
    assert r.ok is True
    assert r.reason == "substr"


def test_verify_thread_title_mismatch(monkeypatch):
    monkeypatch.setattr(
        ta, "dump_view_tree", lambda s, **kw: THREAD_XML_WRONG_PEER,
    )
    r = ta.verify_thread_title("abc", "Jane Doe")
    assert r.ok is False


def test_verify_thread_title_rejects_short_title_for_long_expected(monkeypatch):
    """顶栏 Victor 不得视为 Victor Zan（互发点错人后 U1 须能拦）。"""
    monkeypatch.setattr(
        ta, "dump_view_tree", lambda s, **kw: THREAD_XML_VICTOR_SHORT_TITLE,
    )
    r = ta.verify_thread_title("abc", "Victor Zan")
    assert r.ok is False
    assert r.reason == "mismatch"
    assert r.actual == "Victor"


def test_verify_thread_title_dump_failed(monkeypatch):
    monkeypatch.setattr(ta, "dump_view_tree", lambda s, **kw: None)
    r = ta.verify_thread_title("abc", "Jane Doe")
    assert r.ok is False
    assert r.reason == "dump_failed"


def test_verify_thread_title_not_in_thread(monkeypatch):
    monkeypatch.setattr(
        ta, "dump_view_tree", lambda s, **kw:
        "<hierarchy><node class='android.widget.FrameLayout'/></hierarchy>",
    )
    r = ta.verify_thread_title("abc", "Jane Doe")
    assert r.ok is False
    assert r.reason == "not_in_thread"


def test_verify_thread_title_ignores_unicode_direction(monkeypatch):
    xml = (
        "<hierarchy><node class='android.widget.Button' "
        "content-desc='\u202aJane Doe\u202c, 对话详情' "
        "bounds='[112,76][424,172]'/></hierarchy>"
    )
    monkeypatch.setattr(ta, "dump_view_tree", lambda s, **kw: xml)
    r = ta.verify_thread_title("abc", "Jane Doe")
    assert r.ok is True


def test_verify_thread_title_case_insensitive(monkeypatch):
    monkeypatch.setattr(
        ta, "dump_view_tree", lambda s, **kw: THREAD_XML_KEYBOARD_OPEN,
    )
    r = ta.verify_thread_title("abc", "jane doe")
    assert r.ok is True


# ── wait_keyboard_open (U2) ───────────────────────────────

def test_wait_keyboard_open_success(monkeypatch):
    monkeypatch.setattr(
        ta, "dump_view_tree", lambda s, **kw: THREAD_XML_KEYBOARD_OPEN,
    )
    r = asyncio.run(
        ta.wait_keyboard_open("abc", timeout_sec=0.5, poll_interval_sec=0.05),
    )
    assert r.ok is True
    assert r.input_box is not None
    assert r.input_box.keyboard_open is True


def test_wait_keyboard_open_timeout(monkeypatch):
    monkeypatch.setattr(
        ta, "dump_view_tree", lambda s, **kw: THREAD_XML_KEYBOARD_CLOSED_EMPTY,
    )
    r = asyncio.run(
        ta.wait_keyboard_open("abc", timeout_sec=0.3, poll_interval_sec=0.05),
    )
    assert r.ok is False
    assert r.reason == "timeout"
    assert r.tries >= 1


# ── inject_and_verify (U2) ────────────────────────────────

def test_inject_and_verify_exact_match(monkeypatch):
    monkeypatch.setattr(
        ta, "dump_view_tree", lambda s, **kw: THREAD_XML_KEYBOARD_OPEN,
    )

    def fake_inject(serial, text, **kw):
        return SimpleNamespace(ok=True, path="clipboard_paste", error="")

    import src.integrations.messenger_rpa.text_input as ti_mod
    monkeypatch.setattr(ti_mod, "inject_text", fake_inject)

    r = asyncio.run(
        ta.inject_and_verify(
            "abc", "hello world",
            settle_sec=0.01, max_retries=0,
        ),
    )
    assert r.ok is True
    assert r.reason == "exact"
    assert r.actual_text == "hello world"


def test_inject_and_verify_mismatch_retries(monkeypatch):
    # 第 1 次 dump 返回 "bad"，第 2 次返回正确 —— 模拟 IME 慢
    responses = iter([
        THREAD_XML_KEYBOARD_CLOSED_EMPTY,  # 1st: EditText still hint
        THREAD_XML_KEYBOARD_OPEN,          # 2nd: good
    ])
    monkeypatch.setattr(
        ta, "dump_view_tree", lambda s, **kw: next(responses),
    )

    def fake_inject(serial, text, **kw):
        return SimpleNamespace(ok=True, path="clipboard_paste", error="")

    import src.integrations.messenger_rpa.text_input as ti_mod
    monkeypatch.setattr(ti_mod, "inject_text", fake_inject)

    # monkey out the DEL loop so it doesn't hit adb
    monkeypatch.setattr(
        ta.adb, "run_adb",
        lambda *a, **kw: SimpleNamespace(stdout="", stderr="", returncode=0),
    )

    r = asyncio.run(
        ta.inject_and_verify(
            "abc", "hello world",
            settle_sec=0.01, max_retries=1,
        ),
    )
    assert r.ok is True
    assert r.tries == 2


def test_inject_and_verify_inject_fails_immediately(monkeypatch):
    def fake_inject(serial, text, **kw):
        return SimpleNamespace(
            ok=False, path="rejected", error="no_unicode_path",
        )

    import src.integrations.messenger_rpa.text_input as ti_mod
    monkeypatch.setattr(ti_mod, "inject_text", fake_inject)
    monkeypatch.setattr(
        ta, "dump_view_tree", lambda s, **kw: THREAD_XML_KEYBOARD_OPEN,
    )

    r = asyncio.run(
        ta.inject_and_verify("abc", "hi", settle_sec=0.01, max_retries=0),
    )
    assert r.ok is False
    assert "inject_failed" in r.reason


# ── tap_send_when_ready (U2) ──────────────────────────────

def test_tap_send_when_ready_precise(monkeypatch):
    monkeypatch.setattr(
        ta, "dump_view_tree", lambda s, **kw: THREAD_XML_KEYBOARD_OPEN,
    )
    calls = []
    monkeypatch.setattr(
        ta.adb, "run_adb",
        lambda args, **kw: (
            calls.append(args) or
            SimpleNamespace(stdout="", stderr="", returncode=0)
        ),
    )
    r = ta.tap_send_when_ready("abc")
    assert r.ok is True
    assert r.reason == "precise"
    # SEND bbox [640,876][720,996] → cx=680 cy=936
    assert r.tapped_x == 680 and r.tapped_y == 936
    assert any("input tap 680 936" in " ".join(a) for a in calls)


def test_tap_send_when_ready_falls_back_when_no_keyboard(monkeypatch):
    monkeypatch.setattr(
        ta, "dump_view_tree", lambda s, **kw: THREAD_XML_KEYBOARD_CLOSED_EMPTY,
    )
    monkeypatch.setattr(
        ta.adb, "run_adb",
        lambda args, **kw: SimpleNamespace(
            stdout="", stderr="", returncode=0,
        ),
    )
    r = ta.tap_send_when_ready("abc", fallback_xy=(671, 940))
    assert r.ok is True
    assert r.reason.startswith("fallback")


def test_tap_send_when_ready_no_fallback_fails(monkeypatch):
    monkeypatch.setattr(
        ta, "dump_view_tree", lambda s, **kw: THREAD_XML_KEYBOARD_CLOSED_EMPTY,
    )
    r = ta.tap_send_when_ready("abc", fallback_xy=None)
    assert r.ok is False


# ── assert_sent (U4) ──────────────────────────────────────

def test_assert_sent_seen_by_peer(monkeypatch):
    monkeypatch.setattr(
        ta, "dump_view_tree", lambda s, **kw: THREAD_XML_BUBBLE_AFTER_SEND,
    )
    r = asyncio.run(
        ta.assert_sent(
            "abc", "hello world", wait_sec=0.01,
        ),
    )
    assert r.ok is True
    assert r.reason == "seen_by_peer"
    assert r.seen_by == "Jane Doe"


def test_assert_sent_input_cleared(monkeypatch):
    xml_cleared = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy>
  <node class='android.widget.Button' content-desc='Jane Doe, 对话详情' bounds='[112,76][424,172]'/>
  <node class='android.widget.EditText' text='发消息' content-desc='输入消息' bounds='[320,1404][560,1438]'/>
</hierarchy>
"""
    monkeypatch.setattr(ta, "dump_view_tree", lambda s, **kw: xml_cleared)
    r = asyncio.run(
        ta.assert_sent("abc", "hello world", wait_sec=0.01),
    )
    # 输入框空了但无已读和气泡信号 —— 视为 "大概率已发" (reason=input_cleared)
    assert r.ok is True
    assert r.reason == "input_cleared"


def test_assert_sent_no_signal(monkeypatch):
    # EditText 还有残留 "hello world"，也没 seen_by，也没 bubble
    monkeypatch.setattr(
        ta, "dump_view_tree", lambda s, **kw: THREAD_XML_KEYBOARD_OPEN,
    )
    r = asyncio.run(
        ta.assert_sent("abc", "different text", wait_sec=0.01),
    )
    assert r.ok is False
    assert r.reason == "no_signal"


def test_peer_names_match_bidi_strip() -> None:
    assert ta.peer_names_match("\u202aJohn\u202c", "John") is True


def test_peer_names_match_substr_toggle() -> None:
    assert ta.peer_names_match("Jane D", "Jane Doe", allow_substr=True) is True
    assert ta.peer_names_match("Jane D", "Jane Doe", allow_substr=False) is False


def test_peer_names_match_inbox_pick_rejects_short_row() -> None:
    """行名 Victor 不得命中目标 Victor Zan（旧 peer_names_match 会误判）。"""
    assert ta.peer_names_match("Victor", "Victor Zan") is True
    assert ta.peer_names_match_inbox_pick("Victor", "Victor Zan") is False


def test_peer_names_match_inbox_pick_truncated_row_ok() -> None:
    assert ta.peer_names_match_inbox_pick("Jane Do", "Jane Doe") is True


def test_peer_names_match_inbox_pick_target_in_longer_row() -> None:
    assert ta.peer_names_match_inbox_pick("Victor Zan · Active", "Victor Zan") is True
