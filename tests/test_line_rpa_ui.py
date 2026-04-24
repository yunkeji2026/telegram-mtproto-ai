"""line_rpa UI 解析单测（无设备）。"""

from src.integrations.line_rpa import ui_hierarchy as ui


_MINIMAL_XML = b"""<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node bounds="[0,0][1080,2400]" />
  <node class="android.widget.TextView" text="CHATS" bounds="[0,0][200,80]" />
  <node class="android.widget.TextView" text="Hello peer" resource-id="jp.naver.line.android:id/chat_row"
         bounds="[40,1800][600,1880]" />
  <node class="android.widget.TextView" text="Me reply" resource-id="jp.naver.line.android:id/chat_row"
         bounds="[500,2000][1040,2080]" />
  <node class="android.widget.EditText" text="" bounds="[80,2200][900,2340]" />
</hierarchy>
"""


def test_pick_last_peer_text_left():
    text, dbg = ui.pick_last_peer_text(_MINIMAL_XML, left_ratio=0.45)
    assert text == "Hello peer"
    assert "left_bubble" in dbg or "bottom=" in dbg


def test_find_edittext():
    xy = ui.find_edittext_bottom_center(_MINIMAL_XML)
    assert xy is not None
    assert xy[1] > 2000
