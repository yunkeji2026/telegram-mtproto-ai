"""P2-2 / P2-4 / P2-5 / P2-6 新能力单元测试（不触 ADB）。

- chat_list_scanner.red_ratio_in_box / _is_red_pixel
- chat_list_scanner.parse_unread_rows + 红点兜底
- ui_hierarchy.detect_group_chat / detect_mentioned
- group_policy.evaluate
- failure_shots.FailureShotsConfig / save_failure_shot
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from src.integrations.line_rpa import chat_list_scanner as cls_mod
from src.integrations.line_rpa import group_policy
from src.integrations.line_rpa import ui_hierarchy as ui
from src.integrations.line_rpa.chat_list_scanner import parse_unread_rows, red_ratio_in_box
from src.integrations.line_rpa.failure_shots import (
    FailureShotsConfig,
    save_failure_shot,
)


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


def _node_text(text: str, bounds: str, *, rid: str = "", pkg: str = PKG,
               cdesc: str = "") -> str:
    return (
        f"    <node class='android.widget.TextView' text='{text}' "
        f"bounds='{bounds}' package='{pkg}' "
        f"resource-id='{rid}' content-desc='{cdesc}'/>"
    )


# ──────────────────────────────────────────────────────────
# P2-2：红点兜底 —— 像素级
# ──────────────────────────────────────────────────────────


def _make_solid_png(color=(247, 0, 66), size=(60, 60)) -> bytes:
    from PIL import Image
    im = Image.new("RGB", size, color)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def test_red_ratio_full_red_box():
    png = _make_solid_png()
    r = red_ratio_in_box(png, (0, 0, 60, 60))
    assert r > 0.9


def test_red_ratio_no_red_box():
    png = _make_solid_png(color=(10, 10, 10))
    r = red_ratio_in_box(png, (0, 0, 60, 60))
    assert r < 0.05


def test_red_ratio_out_of_bounds_does_not_crash():
    png = _make_solid_png()
    # 负坐标、越界坐标都应被夹回有效范围
    r = red_ratio_in_box(png, (-50, -50, 200, 200))
    assert 0.0 <= r <= 1.0


def test_parse_unread_rows_red_dot_fallback_picks_up_row_without_digit():
    """姓名行没有数字徽章但右侧有红点 → 应被红点兜底识别。"""
    xml = _hier(PKG, "\n".join([
        _node_text("Alice", "[200,320][600,400]",
                   rid=f"{PKG}:id/chatlist_row_name"),
    ]))
    # 构造一张"行右侧有红块"的假截图：整图 1080x2340 黑色，行右区红色
    from PIL import Image
    im = Image.new("RGB", (1080, 2340), (10, 10, 10))
    # 行 y 中心约 360，行右侧条带大概 = right 20% 范围 ≈ [824, 1080]
    for x in range(880, 1060):
        for y in range(320, 400):
            im.putpixel((x, y), (247, 0, 66))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    png = buf.getvalue()

    rd_cfg = {"enabled": True, "min_red_ratio": 0.04, "right_strip_ratio": 0.25}
    rows, dbg = parse_unread_rows(xml, png_bytes=png, red_dot_cfg=rd_cfg)
    assert len(rows) == 1, dbg
    assert rows[0].name == "Alice"
    assert rows[0].source == "red_dot"
    assert rows[0].unread_count == 1


def test_parse_unread_rows_red_dot_fallback_disabled_by_default():
    xml = _hier(PKG, _node_text(
        "Alice", "[200,320][600,400]", rid=f"{PKG}:id/chatlist_row_name"))
    rows, _ = parse_unread_rows(xml)  # 未提供 png / cfg
    assert rows == []


# ──────────────────────────────────────────────────────────
# P2-4：detect_group_chat / detect_mentioned
# ──────────────────────────────────────────────────────────


def test_detect_group_chat_by_count_suffix():
    xml = _hier(PKG, _node_text(
        "产品内部群 (12)", "[120,60][900,160]",
        rid=f"{PKG}:id/header_title"))
    is_grp, dbg = ui.detect_group_chat(xml)
    assert is_grp, dbg


def test_detect_group_chat_one_on_one_returns_false():
    xml = _hier(PKG, _node_text(
        "小王", "[120,60][900,160]",
        rid=f"{PKG}:id/header_title"))
    is_grp, _ = ui.detect_group_chat(xml)
    assert not is_grp


def test_detect_mentioned_by_peer_text_at_prefix():
    xml = _hier(PKG, "")
    found, dbg = ui.detect_mentioned(
        xml, peer_text="@客服小助手 帮我看一下这个订单", self_names=["客服小助手"],
    )
    assert found, dbg


def test_detect_mentioned_no_self_names_config():
    xml = _hier(PKG, "")
    found, dbg = ui.detect_mentioned(
        xml, peer_text="@客服小助手 你好", self_names=[],
    )
    assert not found
    assert "no_self_names" in dbg or "self_names_empty" in dbg


def test_detect_mentioned_by_rid_mention_node():
    xml = _hier(PKG, "\n".join([
        _node_text("@客服", "[100,1600][400,1680]",
                   rid=f"{PKG}:id/chat_mention_span"),
    ]))
    found, dbg = ui.detect_mentioned(
        xml, peer_text="无关纯文本", self_names=["客服"],
    )
    assert found, dbg


# ──────────────────────────────────────────────────────────
# P2-4：group_policy.evaluate
# ──────────────────────────────────────────────────────────


def _chat_room_xml(topbar: str, peer_text_node: str = "") -> bytes:
    nodes = [
        _node_text(topbar, "[120,60][900,160]", rid=f"{PKG}:id/header_title"),
    ]
    if peer_text_node:
        nodes.append(_node_text(
            peer_text_node, "[100,1600][600,1700]",
            rid=f"{PKG}:id/message_text",
        ))
    return _hier(PKG, "\n".join(nodes))


def test_group_policy_one_on_one_always_replies():
    xml = _chat_room_xml("张三", "你好")
    v = group_policy.evaluate(
        xml=xml, peer_text="你好", line_pkg=PKG,
        self_names=["我"], group_reply_policy="mention_only",
        default_style_hint="", mentioned_style_hint="",
    )
    assert v.is_group is False
    assert v.should_reply is True


def test_group_policy_mention_only_skips_non_mention_in_group():
    xml = _chat_room_xml("群聊 (5)", "你好")
    v = group_policy.evaluate(
        xml=xml, peer_text="你好", line_pkg=PKG,
        self_names=["客服"], group_reply_policy="mention_only",
        default_style_hint="", mentioned_style_hint="",
    )
    assert v.is_group is True
    assert v.mentioned is False
    assert v.should_reply is False
    assert v.skip_step == "group_not_mentioned"


def test_group_policy_mention_only_replies_when_mentioned():
    xml = _chat_room_xml("群聊 (5)")
    v = group_policy.evaluate(
        xml=xml, peer_text="@客服 帮忙", line_pkg=PKG,
        self_names=["客服"], group_reply_policy="mention_only",
        default_style_hint="normal", mentioned_style_hint="urgent",
    )
    assert v.is_group and v.mentioned
    assert v.should_reply
    assert v.style_hint == "urgent"  # 提权替换


def test_group_policy_never_always_skips_in_group():
    xml = _chat_room_xml("群聊 (3)")
    v = group_policy.evaluate(
        xml=xml, peer_text="@客服", line_pkg=PKG,
        self_names=["客服"], group_reply_policy="never",
        default_style_hint="", mentioned_style_hint="",
    )
    assert v.should_reply is False
    assert v.skip_step == "group_policy_never"


def test_group_policy_unknown_value_defaults_to_all():
    xml = _chat_room_xml("群聊 (3)")
    v = group_policy.evaluate(
        xml=xml, peer_text="hello", line_pkg=PKG,
        self_names=[], group_reply_policy="weirdvalue",
        default_style_hint="", mentioned_style_hint="",
    )
    assert v.should_reply is True


# ──────────────────────────────────────────────────────────
# P2-5 / P2-6：failure_shots
# ──────────────────────────────────────────────────────────


def test_failure_shots_config_defaults():
    c = FailureShotsConfig.from_dict(None)
    assert c.enabled is False
    assert "open_fail" in c.on_steps


def test_failure_shots_config_custom_steps():
    c = FailureShotsConfig.from_dict({
        "enabled": True, "dir": "/tmp/x", "max_files": 50,
        "on_steps": ["open_fail", "custom_x"],
    })
    assert c.enabled and c.dir == "/tmp/x" and c.max_files == 50
    assert c.on_steps == ["open_fail", "custom_x"]


def test_save_failure_shot_disabled_returns_none(tmp_path: Path):
    cfg = FailureShotsConfig(enabled=False, dir=str(tmp_path))
    res = save_failure_shot(cfg=cfg, step="open_fail", chat_key="X", png=b"\x89PNG\r\n")
    assert res is None


def test_save_failure_shot_step_not_in_list(tmp_path: Path):
    cfg = FailureShotsConfig(
        enabled=True, dir=str(tmp_path), on_steps=["open_fail"],
    )
    res = save_failure_shot(cfg=cfg, step="other_step", chat_key="X", png=b"\x89PNG")
    assert res is None


def test_save_failure_shot_writes_file(tmp_path: Path):
    cfg = FailureShotsConfig(
        enabled=True, dir=str(tmp_path), max_files=5,
        on_steps=["open_fail"],
    )
    fname = save_failure_shot(
        cfg=cfg, step="open_fail", chat_key="Alice/测试",
        png=b"\x89PNG\r\n\x1a\nfake",
    )
    assert fname is not None
    assert fname.endswith(".png")
    assert (tmp_path / fname).is_file()
    # 文件名不得包含目录分隔符
    assert "/" not in fname and "\\" not in fname


def test_save_failure_shot_fifo_cleans_old(tmp_path: Path):
    cfg = FailureShotsConfig(
        enabled=True, dir=str(tmp_path), max_files=3,
        on_steps=["open_fail"],
    )
    import time
    for i in range(6):
        save_failure_shot(
            cfg=cfg, step="open_fail", chat_key=f"c{i}",
            png=b"\x89PNG\r\n" + bytes([i]),
        )
        time.sleep(0.01)  # 保证 mtime 递增
    remaining = list(tmp_path.glob("*.png"))
    assert len(remaining) <= 3


def test_save_failure_shot_no_png_returns_none(tmp_path: Path):
    cfg = FailureShotsConfig(
        enabled=True, dir=str(tmp_path), on_steps=["open_fail"],
    )
    assert save_failure_shot(
        cfg=cfg, step="open_fail", chat_key="X", png=None,
    ) is None
    assert save_failure_shot(
        cfg=cfg, step="open_fail", chat_key="X", png=b"",
    ) is None
