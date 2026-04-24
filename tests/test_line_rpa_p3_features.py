"""P3 阶段：对 pick_last_peer_bubbles、notification_check、滚动防抖 的单元测试。"""

from __future__ import annotations

from src.integrations.line_rpa import notification_check as nc
from src.integrations.line_rpa import ui_hierarchy as ui


def _bubble_xml(bubbles):
    """bubbles = [(left, top, right, bottom, text)] 生成 uiautomator XML."""
    nodes = []
    for i, (l, t, r, b, txt) in enumerate(bubbles):
        nodes.append(
            f'<node index="{i}" text="{txt}" resource-id="jp.naver.line.android:id/chat_ui_message_text" '
            f'class="android.widget.TextView" bounds="[{l},{t}][{r},{b}]"/>'
        )
    inner = "\n".join(nodes)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<hierarchy rotation="0">'
        f'<node class="android.widget.FrameLayout" bounds="[0,0][1080,2340]">{inner}</node>'
        "</hierarchy>"
    ).encode("utf-8")


def test_pick_last_peer_bubbles_merges_three_consecutive():
    # 对方连发 3 段，彼此相邻 bottom_y 差 ~100px，都在屏幕左半区
    xml = _bubble_xml([
        (60, 1000, 400, 1100, "第一段"),
        (60, 1150, 400, 1250, "第二段"),
        (60, 1300, 400, 1400, "第三段"),
        # 自己的气泡在右侧 —— 不应被纳入
        (700, 1450, 1020, 1550, "自己回复"),
    ])
    bubbles, dbg = ui.pick_last_peer_bubbles(xml)
    assert bubbles == ["第一段", "第二段", "第三段"], (bubbles, dbg)
    assert "bubbles=3" in dbg


def test_pick_last_peer_bubbles_stops_on_large_gap():
    # 加一个右侧占位（自己的气泡），保证 screen_width 正确估算
    xml = _bubble_xml([
        (60, 200, 400, 300, "更早的一段"),  # 与下一个差 900+
        (60, 1200, 400, 1300, "第二段"),
        (60, 1350, 400, 1450, "第三段"),
        (700, 1500, 1020, 1600, "自己"),
    ])
    bubbles, dbg = ui.pick_last_peer_bubbles(xml, max_gap_px=220)
    # 第一段与第二段的 bottom 差 > 220，应该被截断
    assert bubbles == ["第二段", "第三段"], (bubbles, dbg)


def test_pick_last_peer_bubbles_stops_on_self_reply_interleaved():
    # 自己的回复穿插在对方气泡之间 —— 由于自己的气泡在 cx>540，不会进候选；
    # 但它"物理上"占据了 bottom_y，左侧候选间隔会被拉大，从而自然断开
    xml = _bubble_xml([
        (60, 1000, 400, 1100, "对方1"),
        (700, 1150, 1020, 1250, "自己"),
        (60, 1400, 400, 1500, "对方2"),  # 与对方1 差 400 > 默认 220
    ])
    bubbles, _ = ui.pick_last_peer_bubbles(xml)
    assert bubbles == ["对方2"], bubbles


def test_pick_last_peer_bubbles_empty_xml():
    bubbles, dbg = ui.pick_last_peer_bubbles(b"")
    assert bubbles == []
    assert "xml_parse_error" in dbg or dbg == "no_text_nodes"


# ── notification_check ────────────────────────────────────

SAMPLE_DUMPSYS = """
  Notification List:
    NotificationRecord(0x1 pkg=jp.naver.line.android user=UserHandle{0})
      mGroupKey=a
      android.title=Alice
      android.text=你好呀
      android.subText=Lion Club
      mExtras=...
    NotificationRecord(0x2 pkg=jp.naver.line.android user=UserHandle{0})
      android.title=Bob
      android.bigText=今天你去哪里了?\\n我等了你好久\\n快回我啊
    NotificationRecord(0x3 pkg=com.android.chrome user=UserHandle{0})
      android.title=Chrome Tab
      android.text=unrelated
"""


def test_parse_notifications_extracts_only_line():
    snap = nc.parse_notifications(SAMPLE_DUMPSYS)
    assert snap.ok is True
    titles = snap.titles()
    assert "Alice" in titles
    assert "Bob" in titles
    assert "Chrome Tab" not in titles
    assert snap.total() == 2


def test_parse_notifications_empty():
    snap = nc.parse_notifications("")
    assert snap.ok is False
    assert snap.total() == 0


def test_parse_notifications_no_line_records():
    snap = nc.parse_notifications("""
    NotificationRecord(0xa pkg=com.foo.bar user=UserHandle{0})
      android.title=Foo
      android.text=Bar
    """)
    assert snap.ok is True
    assert snap.total() == 0


def test_health_verdict_possibly_missed():
    assert nc.health_verdict(main_unread=0, notif_count=3) == "possibly_missed"


def test_health_verdict_ok_when_both_zero():
    assert nc.health_verdict(main_unread=0, notif_count=0) == "ok"


def test_health_verdict_covered_with_room():
    assert nc.health_verdict(main_unread=5, notif_count=2) == "covered_with_room"


def test_health_verdict_inconsistent():
    assert nc.health_verdict(main_unread=1, notif_count=3) == "inconsistent"


def test_health_verdict_unknown_on_negative():
    assert nc.health_verdict(main_unread=-1, notif_count=0) == "unknown"
