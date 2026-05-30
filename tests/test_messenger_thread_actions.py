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
from src.integrations.messenger_rpa import recent_verify_cache as _rvc


@pytest.fixture(autouse=True)
def _reset_recent_verify_cache():
    """每个 test 前后清 recent_verify_cache，避免互相污染。"""
    _rvc._reset()
    yield
    _rvc._reset()


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
    # Genuine Messenger XML (has com.facebook.orca) but no thread title → not_in_thread
    monkeypatch.setattr(
        ta, "dump_view_tree", lambda s, **kw:
        "<hierarchy><node package='com.facebook.orca' "
        "class='android.widget.FrameLayout'/></hierarchy>",
    )
    r = ta.verify_thread_title("abc", "Jane Doe")
    assert r.ok is False
    assert r.reason == "not_in_thread"


def test_verify_thread_title_xml_garbage_no_vision(monkeypatch):
    # Non-Messenger XML (notification shade, no com.facebook.orca) + no vision_cfg
    monkeypatch.setattr(
        ta, "dump_view_tree", lambda s, **kw:
        "<hierarchy><node class='android.widget.FrameLayout'/></hierarchy>",
    )
    r = ta.verify_thread_title("abc", "Jane Doe")
    assert r.ok is False
    assert r.reason == "not_in_thread_xml_garbage"


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


# ── verify_thread_title vision fallback (dump 死掉时的兜底) ──

def test_verify_thread_title_vision_fallback_exact(monkeypatch):
    """dump 永久失败 + 提供 vision_cfg → 走 vision，命中 exact。"""
    monkeypatch.setattr(ta, "dump_view_tree", lambda s, **kw: None)

    from src.integrations.messenger_rpa import thread_title_vision as ttv

    monkeypatch.setattr(
        ttv, "read_thread_title_via_vision",
        lambda s, vc, gv=None, **kw: ttv.VisionTitleResult(
            title="Victor Zan", debug="ok",
        ),
    )
    r = ta.verify_thread_title(
        "abc", "Victor Zan", vision_cfg={"provider": "zhipu", "api_key": "x"},
    )
    assert r.ok is True
    assert r.actual == "Victor Zan"
    assert r.reason == "exact_via_vision"


def test_verify_thread_title_vision_fallback_substr(monkeypatch):
    """vision 把 'Active 1h ago' 拼进来也不影响——substr 命中即可。"""
    monkeypatch.setattr(ta, "dump_view_tree", lambda s, **kw: None)

    from src.integrations.messenger_rpa import thread_title_vision as ttv

    monkeypatch.setattr(
        ttv, "read_thread_title_via_vision",
        lambda s, vc, gv=None, **kw: ttv.VisionTitleResult(
            title="Jane Doe Active 1 hour ago", debug="ok",
        ),
    )
    r = ta.verify_thread_title(
        "abc", "Jane Doe", vision_cfg={"provider": "zhipu", "api_key": "x"},
    )
    assert r.ok is True
    assert r.reason == "substr_via_vision"


def test_verify_thread_title_vision_fallback_mismatch(monkeypatch):
    """vision 读出错的人名 → mismatch_via_vision，仍要拦截发送。"""
    monkeypatch.setattr(ta, "dump_view_tree", lambda s, **kw: None)

    from src.integrations.messenger_rpa import thread_title_vision as ttv

    monkeypatch.setattr(
        ttv, "read_thread_title_via_vision",
        lambda s, vc, gv=None, **kw: ttv.VisionTitleResult(
            title="Someone Else", debug="ok",
        ),
    )
    r = ta.verify_thread_title(
        "abc", "Victor Zan", vision_cfg={"provider": "zhipu", "api_key": "x"},
    )
    assert r.ok is False
    assert r.reason == "mismatch_via_vision"
    assert r.actual == "Someone Else"


def test_verify_thread_title_vision_returns_none(monkeypatch):
    """dump 死 + vision 也读不出 → 标 dump_failed_vision_xxx，仍然不发。"""
    monkeypatch.setattr(ta, "dump_view_tree", lambda s, **kw: None)

    from src.integrations.messenger_rpa import thread_title_vision as ttv

    monkeypatch.setattr(
        ttv, "read_thread_title_via_vision",
        lambda s, vc, gv=None, **kw: ttv.VisionTitleResult(
            title=None, debug="vision_empty",
        ),
    )
    r = ta.verify_thread_title(
        "abc", "Victor Zan", vision_cfg={"provider": "zhipu", "api_key": "x"},
    )
    assert r.ok is False
    assert r.reason.startswith("dump_failed_vision_")


def test_verify_thread_title_no_vision_cfg_keeps_legacy_behavior(monkeypatch):
    """没传 vision_cfg → 老逻辑，dump 失败直接 dump_failed。"""
    monkeypatch.setattr(ta, "dump_view_tree", lambda s, **kw: None)
    r = ta.verify_thread_title("abc", "Victor Zan")
    assert r.ok is False
    assert r.reason == "dump_failed"


def test_verify_thread_title_xml_path_unaffected_by_vision_cfg(monkeypatch):
    """dump 正常时不应进 vision 路径——reason 仍是 'exact'，无 _via_vision 后缀。"""
    ta._reset_dump_capability_cache()
    monkeypatch.setattr(
        ta, "dump_view_tree", lambda s, **kw: THREAD_XML_KEYBOARD_OPEN,
    )
    # vision_cfg 即使配置了，也不该被调用（dump 已成功）
    called: list = []

    from src.integrations.messenger_rpa import thread_title_vision as ttv

    def _should_not_call(*args, **kwargs):
        called.append(("read_thread_title_via_vision", args, kwargs))
        return ttv.VisionTitleResult(title="WRONG", debug="should_not_run")

    monkeypatch.setattr(
        ttv, "read_thread_title_via_vision", _should_not_call,
    )
    r = ta.verify_thread_title(
        "abc", "Jane Doe",
        vision_cfg={"provider": "zhipu", "api_key": "x"},
    )
    assert r.ok is True
    assert r.reason == "exact"   # 不是 exact_via_vision
    assert called == []          # 未被调


def test_verify_thread_title_dump_dead_skips_retries(monkeypatch):
    """dump 连续失败 ≥2 次 → 后续直接走 vision，跳重试 sleep 浪费。

    模拟真实 dump_view_tree 行为：失败时调 _record_dump_fail，跨调用累计。
    """
    ta._reset_dump_capability_cache()
    dump_calls = {"n": 0}

    def _dump_always_dies(s, **kw):
        # 模拟真实 dump_view_tree：先看 dead 缓存
        if ta._dump_is_dead(s):
            return None
        dump_calls["n"] += 1
        ta._record_dump_fail(s)
        return None

    monkeypatch.setattr(ta, "dump_view_tree", _dump_always_dies)

    # 让 sleep 不真睡，让测试快
    monkeypatch.setattr(ta.time, "sleep", lambda *_a, **_kw: None)

    from src.integrations.messenger_rpa import thread_title_vision as ttv

    monkeypatch.setattr(
        ttv, "read_thread_title_via_vision",
        lambda s, vc, gv=None, **kw: ttv.VisionTitleResult(
            title="Jane Doe", debug="ok",
        ),
    )

    cfg = {"provider": "zhipu", "api_key": "x"}

    # 第 1 次：dump 第 1 次失败（fail count=1），第 2 次失败（=2 → dead），
    # 之后命中 dead 缓存立即返 None（也调 dump 但快速 short-circuit）
    r1 = ta.verify_thread_title("device-A", "Jane Doe", vision_cfg=cfg)
    assert r1.ok is True
    # 第 1 次实际进 dump 体内的次数 = 2（第 2 次后已 dead；第 3 次重试在
    # verify 内提前 break 不再调）
    assert dump_calls["n"] == 2

    # 第 2 次：dead cache 已命中，0 次进入 dump 体
    r2 = ta.verify_thread_title("device-A", "Jane Doe", vision_cfg=cfg)
    assert r2.ok is True
    assert dump_calls["n"] == 2   # 未增加


def test_verify_thread_title_dump_recovery_resets_counter(monkeypatch):
    """dump 暂坏一次后立即恢复 → 不应被标 dead。"""
    ta._reset_dump_capability_cache()
    state = {"calls": 0}

    def _flaky_dump(s, **kw):
        if ta._dump_is_dead(s):
            return None
        state["calls"] += 1
        if state["calls"] == 1:
            ta._record_dump_fail(s)
            return None
        ta._record_dump_ok(s)
        return THREAD_XML_KEYBOARD_OPEN

    monkeypatch.setattr(ta, "dump_view_tree", _flaky_dump)
    monkeypatch.setattr(ta.time, "sleep", lambda *_a, **_kw: None)

    cfg = {"provider": "zhipu", "api_key": "x"}
    r1 = ta.verify_thread_title("device-B", "Jane Doe", vision_cfg=cfg)
    assert r1.ok is True
    assert r1.reason == "exact"
    assert ta._dump_is_dead("device-B") is False


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


# ── inject_and_verify P3：bug fix + vision fallback ───────

def test_inject_and_verify_dump_failed_no_double_inject_when_no_vision(monkeypatch):
    """★ 历史 bug：dump 失败时原 continue 触发 retry，导致 inject_text 被
    调用 2-3 次重复注入文字，发出 'hihi'/'hihihi'。

    现修复：dump 失败 → 立即返 ok=True reason='no_verify_dump_failed'，
    inject_text 只调一次。
    """
    monkeypatch.setattr(ta, "dump_view_tree", lambda s, **kw: None)

    inject_calls = {"n": 0}

    def fake_inject(serial, text, **kw):
        inject_calls["n"] += 1
        return SimpleNamespace(ok=True, path="clipboard_paste", error="")

    import src.integrations.messenger_rpa.text_input as ti_mod
    monkeypatch.setattr(ti_mod, "inject_text", fake_inject)

    r = asyncio.run(
        ta.inject_and_verify(
            "abc", "hi",
            settle_sec=0.01,
            max_retries=2,   # 历史 bug 这里会让 inject 跑 3 次
        ),
    )
    assert r.ok is True
    assert r.reason == "no_verify_dump_failed"
    assert inject_calls["n"] == 1, (
        f"双重注入 bug 复发：inject_text 被调用 {inject_calls['n']} 次"
    )


def test_inject_and_verify_vision_fallback_exact(monkeypatch):
    """dump 死 + vision_cfg 提供 → 截屏 vision 读输入框，命中 exact_via_vision。"""
    monkeypatch.setattr(ta, "dump_view_tree", lambda s, **kw: None)

    inject_calls = {"n": 0}

    def fake_inject(serial, text, **kw):
        inject_calls["n"] += 1
        return SimpleNamespace(ok=True, path="adb_keyboard", error="")

    import src.integrations.messenger_rpa.text_input as ti_mod
    monkeypatch.setattr(ti_mod, "inject_text", fake_inject)

    from src.integrations.messenger_rpa import input_text_vision as itv

    monkeypatch.setattr(
        itv, "read_input_text_via_vision",
        lambda s, vc, gv=None, **kw: itv.VisionInputTextResult(
            text="hello world", debug="ok",
        ),
    )

    r = asyncio.run(
        ta.inject_and_verify(
            "abc", "hello world",
            settle_sec=0.01, max_retries=0,
            vision_cfg={"provider": "zhipu", "api_key": "x"},
        ),
    )
    assert r.ok is True
    assert "via_vision" in r.reason
    assert "exact" in r.reason
    assert r.actual_text == "hello world"
    assert inject_calls["n"] == 1


def test_inject_and_verify_vision_fallback_mismatch_no_retry(monkeypatch):
    """vision 读出"不一样"的字 → mismatch_via_vision，**不重试**（避免双发）。

    重要 invariant：vision 路径下 mismatch 也要安全——重试会 inject 2 次。
    实际上修复后的逻辑：vision 读到非空 actual → 走清空+重试路径（保留旧
    语义）。这个测试验证"vision 读到 None"时永不重试。
    """
    monkeypatch.setattr(ta, "dump_view_tree", lambda s, **kw: None)

    inject_calls = {"n": 0}

    def fake_inject(serial, text, **kw):
        inject_calls["n"] += 1
        return SimpleNamespace(ok=True, path="adb_keyboard", error="")

    import src.integrations.messenger_rpa.text_input as ti_mod
    monkeypatch.setattr(ti_mod, "inject_text", fake_inject)

    from src.integrations.messenger_rpa import input_text_vision as itv

    # vision 返 None → 立即兜底放行，**绝不重试**
    monkeypatch.setattr(
        itv, "read_input_text_via_vision",
        lambda s, vc, gv=None, **kw: itv.VisionInputTextResult(
            text=None, debug="vision_empty",
        ),
    )

    r = asyncio.run(
        ta.inject_and_verify(
            "abc", "hi",
            settle_sec=0.01, max_retries=2,
            vision_cfg={"provider": "zhipu", "api_key": "x"},
        ),
    )
    assert r.ok is True
    assert "no_verify_vision_" in r.reason
    assert inject_calls["n"] == 1   # 永不重试


def test_inject_and_verify_vision_prefix_ok(monkeypatch):
    """vision 读到 expected 的前缀（IME 末尾吃掉几个字符）→ 容差内 ok。"""
    monkeypatch.setattr(ta, "dump_view_tree", lambda s, **kw: None)

    def fake_inject(serial, text, **kw):
        return SimpleNamespace(ok=True, path="adb_keyboard", error="")

    import src.integrations.messenger_rpa.text_input as ti_mod
    monkeypatch.setattr(ti_mod, "inject_text", fake_inject)

    from src.integrations.messenger_rpa import input_text_vision as itv

    monkeypatch.setattr(
        itv, "read_input_text_via_vision",
        lambda s, vc, gv=None, **kw: itv.VisionInputTextResult(
            text="hello worl", debug="ok",   # expected 末尾吃了 1 个字
        ),
    )

    r = asyncio.run(
        ta.inject_and_verify(
            "abc", "hello world",
            settle_sec=0.01, max_retries=0,
            tolerate_truncation_chars=2,
            vision_cfg={"provider": "zhipu", "api_key": "x"},
        ),
    )
    assert r.ok is True
    assert "prefix_ok_delta=1" in r.reason
    assert "via_vision" in r.reason


def test_inject_and_verify_xml_path_unaffected_by_vision_cfg(monkeypatch):
    """dump 正常 → 走 XML，reason 没有 _via_vision 后缀（确认健康设备零额外开销）。"""
    monkeypatch.setattr(
        ta, "dump_view_tree", lambda s, **kw: THREAD_XML_KEYBOARD_OPEN,
    )

    def fake_inject(serial, text, **kw):
        return SimpleNamespace(ok=True, path="clipboard_paste", error="")

    import src.integrations.messenger_rpa.text_input as ti_mod
    monkeypatch.setattr(ti_mod, "inject_text", fake_inject)

    from src.integrations.messenger_rpa import input_text_vision as itv
    called = []

    monkeypatch.setattr(
        itv, "read_input_text_via_vision",
        lambda *a, **kw: called.append("vision_called") or
        itv.VisionInputTextResult(text="WRONG", debug="ko"),
    )

    r = asyncio.run(
        ta.inject_and_verify(
            "abc", "hello world",
            settle_sec=0.01, max_retries=0,
            vision_cfg={"provider": "zhipu", "api_key": "x"},
        ),
    )
    assert r.ok is True
    assert r.reason == "exact"   # 不是 exact_via_vision
    assert called == []          # vision 未被调


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
