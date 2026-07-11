"""P5: RPA 手动/回落发送队列「入队即唤醒」跨平台不变量。

LINE 与 WhatsApp service 的 ``enqueue_send`` 必须在入队后 ``_trigger_evt.set()``，
让阻塞在自适应轮询间隔上的 ``_loop`` 立即醒来 pop 队列——否则手动/回落发送要空等
一个 interval（最长数十秒）才投递。此前 WA 侧漏了这一步（LINE 有），本测试锁死两端对称。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest


def _cm(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(config={}, config_path=str(tmp_path / "config.yaml"))


# ── LINE ──────────────────────────────────────────────────────────────────────

def test_line_enqueue_send_wakes_runner(tmp_path: Path):
    from src.integrations.line_rpa.service import LineRpaService

    svc = LineRpaService(
        config_manager=_cm(tmp_path), skill_manager=None,
        line_rpa_cfg={"account_id": "line_test"},
    )
    assert not svc._trigger_evt.is_set()  # 初始未触发
    item_id = svc.enqueue_send(chat_key="line_rpa:Alice", peer_name="Alice", text="hi")
    assert isinstance(item_id, int) and item_id > 0
    assert svc._trigger_evt.is_set()  # 入队后立即唤醒


# ── WhatsApp（本轮修复对象） ────────────────────────────────────────────────────

def test_wa_enqueue_send_wakes_runner(tmp_path: Path):
    from src.integrations.whatsapp_rpa.service import WhatsAppRpaService

    svc = WhatsAppRpaService(
        config_manager=_cm(tmp_path), skill_manager=None,
        wa_cfg={"account_id": "wa_test"},
    )
    assert not svc._trigger_evt.is_set()
    item_id = svc.enqueue_send(chat_key="wa:Bob", peer_name="Bob", text="yo")
    assert isinstance(item_id, int) and item_id > 0
    assert svc._trigger_evt.is_set()  # 修复点：WA 入队也必须唤醒


def test_wa_enqueue_send_persists_item(tmp_path: Path):
    """唤醒之外，入队本身仍正确落库（回归护栏）。"""
    from src.integrations.whatsapp_rpa.service import WhatsAppRpaService

    svc = WhatsAppRpaService(
        config_manager=_cm(tmp_path), skill_manager=None,
        wa_cfg={"account_id": "wa_test"},
    )
    item_id = svc.enqueue_send(chat_key="wa:Bob", peer_name="Bob", text="yo")
    items = svc.list_send_queue(include_done=True)
    assert any(it["id"] == item_id and it["text"] == "yo" for it in items)
