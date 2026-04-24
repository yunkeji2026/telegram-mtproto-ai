"""P2-7：Navigator 端到端伪 ADB 集成测试。

思路：
  - 搭一个带状态机的 FakeDevice（OTHER_APP / CHAT_LIST / CHAT_ROOM 之间切换）
  - monkeypatch adb_helpers 里会被 Navigator 调到的函数，使其只改变 FakeDevice 的状态
  - Navigator 真实运行 goto_chat_list / scan / open / back_to_chat_list，验证端到端路径
  - 覆盖 P2-1 的"循环式重扫"场景也能在 runner._run_once_multi 风格下得到预期结果
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import pytest

from src.integrations.line_rpa import adb_helpers as adb
from src.integrations.line_rpa import screen_state as ss
from src.integrations.line_rpa.chat_list_scanner import UnreadRow
from src.integrations.line_rpa.navigator import Navigator


# ───── 伪 XML 构造（跟 test_line_rpa_navigation 保持一致风格） ─────

PKG = "jp.naver.line.android"


def _hier(pkg: str, nodes: str) -> bytes:
    return (
        "<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>\n"
        "<hierarchy rotation='0'>\n"
        f"  <node index='0' bounds='[0,0][1080,2340]' package='{pkg}' "
        "class='android.widget.FrameLayout'>\n"
        f"{nodes}\n"
        "  </node>\n"
        "</hierarchy>\n"
    ).encode("utf-8")


def _xml_other_app() -> bytes:
    return _hier(
        "com.some.other",
        (
            "    <node class='android.widget.TextView' text='Home' "
            "bounds='[0,0][200,200]' package='com.some.other' "
            "resource-id='' content-desc=''/>"
        ),
    )


def _xml_chat_list(unread: List[Tuple[str, int, int]]) -> bytes:
    # 固定给两个 chatlist rid 容器节点，确保 detect_screen_state 识别为 CHAT_LIST（即使没有未读）
    parts = [
        f"    <node class='androidx.recyclerview.widget.RecyclerView' text='' "
        f"bounds='[0,200][1080,2200]' package='{PKG}' "
        f"resource-id='{PKG}:id/recycler_chatlist' content-desc=''/>",
        f"    <node class='android.view.ViewGroup' text='' "
        f"bounds='[0,200][1080,400]' package='{PKG}' "
        f"resource-id='{PKG}:id/chat_row' content-desc=''/>",
    ]
    for name, count, y in unread:
        parts.append(
            f"    <node class='android.widget.TextView' text='{name}' "
            f"bounds='[200,{y + 20}][700,{y + 100}]' package='{PKG}' "
            f"resource-id='{PKG}:id/chatlist_row_name' content-desc=''/>"
        )
        parts.append(
            f"    <node class='android.widget.TextView' text='{count}' "
            f"bounds='[950,{y + 30}][1020,{y + 90}]' package='{PKG}' "
            f"resource-id='{PKG}:id/chatlist_row_unread_count' content-desc=''/>"
        )
    return _hier(PKG, "\n".join(parts))


def _xml_chat_room(peer_name: str, last_peer_text: str = "你好呀") -> bytes:
    parts = [
        f"    <node class='android.widget.TextView' text='{peer_name}' "
        f"bounds='[120,60][600,160]' package='{PKG}' "
        f"resource-id='{PKG}:id/header_title' content-desc=''/>",
        f"    <node class='android.widget.TextView' text='{last_peer_text}' "
        f"bounds='[100,1600][600,1700]' package='{PKG}' "
        f"resource-id='{PKG}:id/message_text' content-desc=''/>",
        f"    <node class='android.widget.EditText' text='' "
        f"bounds='[40,2100][800,2220]' package='{PKG}' "
        f"resource-id='{PKG}:id/message_edit' content-desc=''/>",
        f"    <node class='android.widget.ImageView' text='' "
        f"bounds='[820,2100][1020,2220]' package='{PKG}' "
        f"resource-id='{PKG}:id/chat_send' content-desc='Send'/>",
        "    <node class='android.widget.ImageView' text='' "
        f"bounds='[0,60][100,160]' package='{PKG}' "
        "resource-id='' content-desc='Navigate up'/>",
    ]
    return _hier(PKG, "\n".join(parts))


# ───── 伪设备状态机 ─────

@dataclass
class FakeDevice:
    """最小化状态机。

    transitions:
      OTHER_APP -launch_line()-> CHAT_LIST
      CHAT_LIST -tap(row)-> CHAT_ROOM(row_name)
      CHAT_ROOM -BACK(keyevent 4)-> CHAT_LIST（同时消耗掉该人的 unread）
    """

    state: str = "other_app"
    unread: List[Tuple[str, int, int]] = field(default_factory=list)
    current_peer: Optional[str] = None
    taps: List[Tuple[int, int]] = field(default_factory=list)
    back_count: int = 0
    launches: int = 0

    def xml(self) -> bytes:
        if self.state == "other_app":
            return _xml_other_app()
        if self.state == "chat_list":
            return _xml_chat_list(self.unread)
        if self.state == "chat_room":
            return _xml_chat_room(self.current_peer or "Unknown")
        raise AssertionError(f"unknown state: {self.state}")

    def do_launch_line(self) -> None:
        self.launches += 1
        self.state = "chat_list"

    def do_tap(self, x: int, y: int) -> None:
        self.taps.append((x, y))
        if self.state == "chat_list":
            # 找 y 对应的 unread 行
            for name, count, row_y in list(self.unread):
                top = row_y + 20
                bot = row_y + 100
                if top - 20 <= y <= bot + 20:
                    self.current_peer = name
                    self.state = "chat_room"
                    return

    def do_back(self) -> None:
        self.back_count += 1
        if self.state == "chat_room":
            # 回 list；顺手把这条未读清掉（模拟"回复后对方已读 / 被标记已读"）
            peer = self.current_peer
            if peer:
                self.unread = [u for u in self.unread if u[0] != peer]
            self.current_peer = None
            self.state = "chat_list"


@pytest.fixture
def fake(monkeypatch):
    dev = FakeDevice()

    def _input_tap(serial, x, y):
        dev.do_tap(int(x), int(y))
        return adb.AdbResult("", "", 0)

    def _input_keyevent(serial, code):
        if str(code) == "4":
            dev.do_back()
        return adb.AdbResult("", "", 0)

    def _input_swipe(serial, x1, y1, x2, y2, duration_ms=380):
        return adb.AdbResult("", "", 0)

    def _ensure_line_foreground(serial, pkg, splash):
        dev.do_launch_line()
        return adb.AdbResult("", "", 0)

    def _screen_size(serial):
        return (1080, 2340)

    monkeypatch.setattr(adb, "input_tap", _input_tap)
    monkeypatch.setattr(adb, "input_keyevent", _input_keyevent)
    monkeypatch.setattr(adb, "input_swipe", _input_swipe)
    monkeypatch.setattr(adb, "ensure_line_foreground", _ensure_line_foreground)
    monkeypatch.setattr(adb, "screen_size", _screen_size)
    return dev


def _make_navigator(dev: FakeDevice) -> Navigator:
    async def dump_func() -> Tuple[Optional[bytes], str]:
        return dev.xml(), "ok"

    return Navigator(
        serial="emulator-fake",
        line_pkg=PKG,
        splash_activity=f"{PKG}/.activity.SplashActivity",
        dump_func=dump_func,
        after_tap_sleep_sec=0.0,
        after_launch_sleep_sec=0.0,
    )


# ───── 测试：goto_chat_list 路径覆盖 ─────


def test_goto_chat_list_from_other_app(fake):
    fake.state = "other_app"
    nav = _make_navigator(fake)
    res = asyncio.run(nav.goto_chat_list(max_steps=4))
    assert res.ok, res.reason
    assert res.state == ss.CHAT_LIST
    assert fake.launches >= 1


def test_goto_chat_list_from_chat_room(fake):
    fake.state = "chat_room"
    fake.current_peer = "Alice"
    fake.unread = [("Alice", 2, 300)]
    nav = _make_navigator(fake)
    res = asyncio.run(nav.goto_chat_list(max_steps=4))
    assert res.ok, res.reason
    assert res.state == ss.CHAT_LIST
    assert fake.back_count >= 1


def test_goto_chat_list_already_there(fake):
    fake.state = "chat_list"
    fake.unread = [("Alice", 1, 300)]
    nav = _make_navigator(fake)
    res = asyncio.run(nav.goto_chat_list(max_steps=4))
    assert res.ok and res.state == ss.CHAT_LIST
    assert fake.launches == 0
    assert fake.back_count == 0


# ───── 测试：scan → open → back 全链路 ─────


def test_open_unread_and_back(fake):
    fake.state = "chat_list"
    fake.unread = [("Alice", 2, 300), ("Bob", 1, 600)]
    nav = _make_navigator(fake)

    rows, dbg, _ = asyncio.run(nav.scan_unread_rows())
    assert len(rows) == 2, dbg
    assert rows[0].name == "Alice"

    op = asyncio.run(nav.open_unread_chat(rows[0]))
    assert op.ok, op.reason
    assert op.state == ss.CHAT_ROOM
    assert fake.current_peer == "Alice"

    back = asyncio.run(nav.back_to_chat_list())
    assert back.ok, back.reason
    assert back.state == ss.CHAT_LIST
    # Alice 已被消耗
    assert [u[0] for u in fake.unread] == ["Bob"]


def test_full_loop_handles_all_unread(fake):
    """模拟 runner._run_once_multi 的循环：每轮 scan → 处理首条 → back，直到 scan 空。

    这是 P2-1 的"每轮重扫"机制的端到端体现：即便 LINE 重排，tap 始终基于新 scan 得到的坐标。
    """
    fake.state = "chat_list"
    fake.unread = [("U1", 1, 300), ("U2", 1, 500), ("U3", 1, 700)]
    nav = _make_navigator(fake)

    handled: List[str] = []

    async def _loop():
        for _ in range(5):  # 防无限
            rows, _dbg, _xml = await nav.scan_unread_rows()
            if not rows:
                break
            top = rows[0]
            op = await nav.open_unread_chat(top)
            assert op.ok
            handled.append(top.name)
            back = await nav.back_to_chat_list()
            assert back.ok

    asyncio.run(_loop())
    assert handled == ["U1", "U2", "U3"]
    assert fake.unread == []


# ───── 测试：swipe_chat_list_down 触发 ADB input_swipe ─────


def test_swipe_chat_list_down_calls_adb(fake, monkeypatch):
    fake.state = "chat_list"
    calls: List[Tuple[int, int, int, int, int]] = []

    def _input_swipe(serial, x1, y1, x2, y2, duration_ms=380):
        calls.append((int(x1), int(y1), int(x2), int(y2), int(duration_ms)))
        return adb.AdbResult("", "", 0)

    monkeypatch.setattr(adb, "input_swipe", _input_swipe)
    nav = _make_navigator(fake)
    ok = asyncio.run(nav.swipe_chat_list_down())
    assert ok
    assert len(calls) == 1
    x1, y1, x2, y2, dur = calls[0]
    assert x1 == x2  # 纯竖向
    assert y1 > y2   # 向上拖（露下方）
    assert dur >= 80


# ───── 测试：scan_unread_rows 在非列表状态下的保护返回 ─────


def test_scan_refuses_when_not_on_chat_list(fake):
    fake.state = "chat_room"
    fake.current_peer = "Zoe"
    fake.unread = []
    nav = _make_navigator(fake)
    rows, dbg, _ = asyncio.run(nav.scan_unread_rows())
    assert rows == []
    assert "not_chat_list" in dbg


# ───── 测试：UnreadRow（只是确保 dataclass 字段对齐） ─────


def test_unread_row_to_dict_has_source():
    row = UnreadRow(
        name="X", unread_count=1, tap_x=500, tap_y=400,
        bounds=(0, 300, 1000, 500),
        badge_bounds=(900, 330, 980, 400),
        name_rid="", badge_rid="",
    )
    d = row.to_dict()
    assert "source" in d
    assert d["source"] == "digit"
