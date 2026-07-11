"""P29: LINE 手动/回落发送队列 UI 投递（``_handle_queued_send`` + ``run_send_queue_deliveries``）。

思路：只伪造「设备 I/O」层（Navigator + _pace_and_send），保留 runner 的真实编排逻辑
与真实 ``find_chat_row_by_name`` 定位——验证「列表定位→点进→发送→回写→回列表」全链路，
以及成功/未找到/打开失败/发送失败/空文本各分支的终态落库与副作用。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import pytest

from src.integrations.line_rpa.runner import LineRpaRunner
from src.integrations.line_rpa.state_store import LineRpaStateStore


# ── 伪聊天列表 XML（与 test_line_rpa_pending_queue 同风格，供真实 find_chat_row_by_name 消费） ──

def _list_xml(names: List[str], *, with_search: bool = False) -> bytes:
    nodes = []
    if with_search:
        # 顶部搜索栏（center=(540,130)），供 find_search_entry 命中
        nodes.append(
            '<node resource-id="jp.naver.line.android:id/search_bar" '
            'class="android.widget.EditText" bounds="[0,80][1080,180]"/>'
        )
    for i, name in enumerate(names):
        top = 300 + i * 200
        bot = top + 80
        nodes.append(
            f'<node index="{i}" text="{name}" '
            f'resource-id="jp.naver.line.android:id/chat_name" '
            f'class="android.widget.TextView" bounds="[60,{top}][500,{bot}]"/>'
        )
    inner = "\n".join(nodes)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<hierarchy rotation="0">'
        f'<node class="android.widget.FrameLayout" bounds="[0,0][1080,2340]">{inner}</node>'
        "</hierarchy>"
    ).encode("utf-8")


_SEARCH_XY = (540, 130)


_ROOM_XML = b"<hierarchy><node class='android.widget.EditText'/></hierarchy>"


def _nav_res(ok: bool, *, state: str = "chat_list", reason: str = "",
             xml: Optional[bytes] = None) -> SimpleNamespace:
    return SimpleNamespace(ok=ok, state=state, reason=reason, attempts=1, xml=xml)


class _FakeNav:
    """伪 Navigator：_dump 回真实列表 XML（让 find_chat_row_by_name 真跑），其余动作记账。

    支持搜索兜底模拟：``with_search`` 令列表 XML 含搜索栏；点击搜索栏坐标后进入「搜索态」，
    此后 _dump 返回 ``search_results`` 名单（模拟搜索结果页）。
    """

    def __init__(
        self, list_names: List[str], *, open_ok: bool = True,
        with_search: bool = False, search_results: Optional[List[str]] = None,
    ) -> None:
        self._list_names = list_names
        self._open_ok = open_ok
        self._with_search = with_search
        self._search_results = search_results
        self._searched = False
        self.opened: List[Any] = []
        self.backs = 0
        self.swipes = 0
        self.goto_calls = 0
        self.taps: List[Tuple[int, int]] = []
        self.press_backs = 0

    async def goto_chat_list(self, *, max_steps: int = 4) -> SimpleNamespace:
        self.goto_calls += 1
        return _nav_res(True, state="chat_list")

    async def _dump(self) -> Tuple[Optional[bytes], str]:
        if self._searched and self._search_results is not None:
            return _list_xml(self._search_results), "ok"
        return _list_xml(self._list_names, with_search=self._with_search), "ok"

    async def swipe_chat_list_down(self) -> bool:
        self.swipes += 1
        return True

    async def tap(self, x: int, y: int) -> None:
        self.taps.append((int(x), int(y)))
        if (int(x), int(y)) == _SEARCH_XY:
            self._searched = True

    async def press_back(self) -> None:
        self.press_backs += 1
        self._searched = False

    async def open_unread_chat(self, row: Any) -> SimpleNamespace:
        self.opened.append(row)
        if self._open_ok:
            return _nav_res(True, state="chat_room", xml=_ROOM_XML)
        return _nav_res(False, state="other_line", reason="open_boom")

    async def back_to_chat_list(self) -> SimpleNamespace:
        self.backs += 1
        return _nav_res(True, state="chat_list")


class _FakeHooks:
    def __init__(self) -> None:
        self.outbound: List[Dict[str, Any]] = []

    def on_message(self, **kw: Any) -> None:
        self.outbound.append(kw)

    def on_line_first_text(self, **kw: Any) -> None:  # pragma: no cover - 入站不在本测试
        pass


@pytest.fixture
def runner(tmp_path: Path):
    store = LineRpaStateStore(tmp_path / "line.db", max_runs_kept=100)
    cm = SimpleNamespace(config={}, config_path=str(tmp_path / "config.yaml"))
    r = LineRpaRunner(
        config_manager=cm, skill_manager=None,
        line_rpa_cfg={"account_id": "acc1"}, state_store=store,
    )
    r._serial = "emulator-fake"  # 跳过 _resolve_serial
    return r, store


def _wire(runner_obj, nav: _FakeNav, *, send_result: Optional[Dict[str, Any]] = None):
    """把 runner 的设备 I/O 换成伪实现；记录 _pace_and_send 调用。"""
    sent: List[Tuple[Any, str]] = []

    async def _fake_pace(xml: Any, text: str) -> Dict[str, Any]:
        sent.append((xml, text))
        return send_result if send_result is not None else {"ok": True}

    runner_obj._build_navigator = lambda: nav  # type: ignore[method-assign]
    runner_obj._pace_and_send = _fake_pace      # type: ignore[method-assign]
    hooks = _FakeHooks()
    runner_obj.set_contact_hooks(hooks)
    return sent, hooks


def test_send_queue_delivers_marks_sent_and_writes_back(runner):
    r, store = runner
    nav = _FakeNav(["Alice", "Bob"])
    sent, hooks = _wire(r, nav)
    item_id = store.enqueue_send(chat_key="line_rpa:Alice", peer_name="Alice", text="hi there")

    res = asyncio.run(r.run_send_queue_deliveries(max_deliver=3))

    assert res["delivered"] == 1 and res["failed"] == 0
    assert store.get_send_queue_item(item_id)["status"] == "sent"
    # 真发生了发送（文本正确）
    assert sent == [(_ROOM_XML, "hi there")]
    # 打开的是 Alice 那一行（find_chat_row_by_name 真跑）
    assert nav.opened and nav.opened[0].name == "Alice"
    # outbound 回写 ContactHooks（与 pending 投递同口径）
    assert len(hooks.outbound) == 1
    ob = hooks.outbound[0]
    assert ob["direction"] == "out" and ob["external_id"] == "line_rpa:Alice"
    assert ob["channel"] == "line" and ob["account_id"] == "acc1"
    # per-chat 状态写了 last_reply
    st = store.get_chat_state("line_rpa:Alice") or {}
    assert (st.get("last_reply") or "") == "hi there"
    # 收尾回到列表
    assert nav.backs >= 1


def test_send_queue_contact_not_found_marks_failed(runner):
    r, store = runner
    nav = _FakeNav(["Bob", "Charlie"])  # 无 Alice
    sent, hooks = _wire(r, nav)
    item_id = store.enqueue_send(chat_key="k", peer_name="Alice", text="hi")

    res = asyncio.run(r.run_send_queue_deliveries(max_deliver=3))

    assert res["delivered"] == 0 and res["failed"] == 1
    item = store.get_send_queue_item(item_id)
    assert item["status"] == "failed" and item["error"] == "chat_not_found"
    assert sent == []               # 没找到人 → 不发送
    assert nav.swipes == 1          # 试过 1 次滚动重试
    assert hooks.outbound == []     # 不回写


def test_send_queue_open_fail_marks_failed_and_backs(runner):
    r, store = runner
    nav = _FakeNav(["Alice"], open_ok=False)
    sent, hooks = _wire(r, nav)
    item_id = store.enqueue_send(chat_key="k", peer_name="Alice", text="hi")

    res = asyncio.run(r.run_send_queue_deliveries(max_deliver=3))

    assert res["failed"] == 1
    item = store.get_send_queue_item(item_id)
    assert item["status"] == "failed" and item["error"].startswith("open_fail")
    assert sent == []
    assert nav.backs >= 1           # 打开失败也要回列表，避免卡在中间态
    assert hooks.outbound == []


def test_send_queue_send_failure_marks_failed_no_writeback(runner):
    r, store = runner
    nav = _FakeNav(["Alice"])
    sent, hooks = _wire(r, nav, send_result={"ok": False, "error": "ime_broadcast_failed"})
    item_id = store.enqueue_send(chat_key="k", peer_name="Alice", text="hi")

    res = asyncio.run(r.run_send_queue_deliveries(max_deliver=3))

    assert res["failed"] == 1
    item = store.get_send_queue_item(item_id)
    assert item["status"] == "failed"
    assert item["error"].startswith("send_failed:ime_broadcast_failed")
    assert sent == [(_ROOM_XML, "hi")]  # 尝试发了，但失败
    assert hooks.outbound == []          # 发送失败 → 不回写（不留幽灵已发）
    assert nav.backs >= 1


def test_send_queue_empty_text_fails_without_navigation(runner):
    r, store = runner
    nav = _FakeNav(["Alice"])
    sent, hooks = _wire(r, nav)
    # 直接构造空文本 item 喂给 _handle_queued_send（enqueue 会拒空，故绕过队列）
    res = asyncio.run(r._handle_queued_send({"chat_key": "k", "peer_name": "Alice", "text": "  "}))

    assert res["ok"] is False and res["error"] == "empty_text"
    assert nav.goto_calls == 0      # 空文本不进导航
    assert sent == []


def test_send_queue_no_adb_device_fails(runner):
    r, store = runner
    r._serial = None
    r._resolve_serial = lambda: None  # type: ignore[method-assign]
    nav = _FakeNav(["Alice"])
    _wire(r, nav)
    res = asyncio.run(r._handle_queued_send({"chat_key": "k", "peer_name": "Alice", "text": "hi"}))
    assert res["ok"] is False and res["error"] == "no_adb_device"


# ── P7：内置搜索兜底（列表滚动扫不到 → LINE 搜索定位） ─────────────────────────

def _stub_type_ok(r):
    async def _t(name):
        return True
    r._type_search_query = _t  # type: ignore[method-assign]


def test_send_queue_search_fallback_finds_and_sends(runner):
    r, store = runner
    # 列表只有 Bob（+搜索栏），目标 Zoe 不在列表；搜索结果含 Zoe
    nav = _FakeNav(["Bob"], with_search=True, search_results=["Zoe"])
    sent, hooks = _wire(r, nav)
    _stub_type_ok(r)
    item_id = store.enqueue_send(chat_key="line_rpa:Zoe", peer_name="Zoe", text="hello")

    res = asyncio.run(r.run_send_queue_deliveries(max_deliver=1))

    assert res["delivered"] == 1
    assert store.get_send_queue_item(item_id)["status"] == "sent"
    assert nav.taps and nav.taps[0] == _SEARCH_XY   # 点了搜索入口
    assert nav.opened and nav.opened[0].name == "Zoe"  # 打开搜索结果里的 Zoe
    assert sent == [(_ROOM_XML, "hello")]
    assert len(hooks.outbound) == 1


def test_send_queue_search_fallback_not_found_backs_out(runner):
    r, store = runner
    # 搜索也搜不到 Zoe（结果仍是 Bob）→ chat_not_found，且退出搜索态
    nav = _FakeNav(["Bob"], with_search=True, search_results=["Bob"])
    sent, hooks = _wire(r, nav)
    _stub_type_ok(r)
    item_id = store.enqueue_send(chat_key="k", peer_name="Zoe", text="hi")

    res = asyncio.run(r.run_send_queue_deliveries(max_deliver=1))

    assert res["failed"] == 1
    assert store.get_send_queue_item(item_id)["error"] == "chat_not_found"
    assert nav.taps[0] == _SEARCH_XY    # 尝试了搜索
    assert nav.press_backs >= 1         # 搜不到 → 退出搜索模式
    assert sent == []


def test_send_queue_no_search_entry_gives_up(runner):
    r, store = runner
    # 列表无搜索栏 → 不进入搜索，直接 chat_not_found
    nav = _FakeNav(["Bob"], with_search=False, search_results=["Zoe"])
    sent, hooks = _wire(r, nav)
    _stub_type_ok(r)
    item_id = store.enqueue_send(chat_key="k", peer_name="Zoe", text="hi")

    res = asyncio.run(r.run_send_queue_deliveries(max_deliver=1))

    assert res["failed"] == 1
    assert store.get_send_queue_item(item_id)["error"] == "chat_not_found"
    assert nav.taps == []   # 无搜索入口 → 没点任何东西
    assert sent == []


def test_type_search_query_ascii_uses_input_text(runner, monkeypatch):
    """ASCII 名字走 input_text_ascii（真实 _type_search_query，仅 monkeypatch adb）。"""
    r, _ = runner
    from src.integrations.line_rpa import adb_helpers as adb
    calls = {}
    monkeypatch.setattr(adb, "input_text_ascii",
                        lambda serial, text: calls.setdefault("t", text))
    ok = asyncio.run(r._type_search_query("Zoe"))
    assert ok is True and calls["t"] == "Zoe"
