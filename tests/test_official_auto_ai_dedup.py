"""Phase A：官方通道 auto_ai 让位（System Z 去重）单测。

核心：会话设为 🚀全自动(auto_ai) 且编排器拥有该账号时，官方 webhook 应**早退让位**给
统一收件箱 autosend（System Z，与 Telegram 同一条人设+语言+风控产线），避免与 webhook
自答双发；任一不满足 → 不让位（维持原 SkillManager 自答，零回归）。
"""
import pytest

import src.integrations.protocol_bridge as pb
from src.integrations.shared.official_inbound import (
    inbox_will_autosend, process_official_inbound,
)


class _FakeStore:
    def __init__(self, mode_by_cid):
        self._m = dict(mode_by_cid)

    def get_automation_mode(self, cid):
        return self._m.get(cid, "")


class _FakeOrch:
    def __init__(self, owned):
        self._owned = set(owned)

    def owns(self, platform, account_id):
        return (str(platform).lower(), str(account_id)) in self._owned


@pytest.fixture(autouse=True)
def _reset_store_getter():
    yield
    pb.register_inbox_store_getter(None)


def _wire(monkeypatch, *, mode, owned):
    # conv_id = platform:account_id:chat_key（与 normalizer.conv_id 一致）
    cid = "whatsapp:PNID:wa:user:123"
    pb.register_inbox_store_getter(lambda: _FakeStore({cid: mode}))
    monkeypatch.setattr(
        "src.integrations.account_orchestrator.get_orchestrator",
        lambda *a, **k: _FakeOrch(owned),
    )


# ── inbox_will_autosend 判定矩阵 ─────────────────────────────────────────────

def test_no_store_getter_returns_false(monkeypatch):
    pb.register_inbox_store_getter(None)
    assert inbox_will_autosend("whatsapp", "PNID", "wa:user:123") is False


def test_not_auto_ai_returns_false(monkeypatch):
    _wire(monkeypatch, mode="review", owned=[("whatsapp", "PNID")])
    assert inbox_will_autosend("whatsapp", "PNID", "wa:user:123") is False


def test_auto_ai_but_orch_not_own_returns_false(monkeypatch):
    _wire(monkeypatch, mode="auto_ai", owned=[])  # 编排器不拥有 → System Z 发不出
    assert inbox_will_autosend("whatsapp", "PNID", "wa:user:123") is False


def test_auto_ai_and_owned_returns_true(monkeypatch):
    _wire(monkeypatch, mode="auto_ai", owned=[("whatsapp", "PNID")])
    assert inbox_will_autosend("whatsapp", "PNID", "wa:user:123") is True


# ── process_official_inbound 让位：返回 True（已托管）且不进 pipeline 自答 ─────

async def test_process_inbound_defers_to_system_z(monkeypatch):
    _wire(monkeypatch, mode="auto_ai", owned=[("whatsapp", "PNID")])
    mirrored = []
    monkeypatch.setattr(
        "src.integrations.shared.inbox_mirror.mirror_to_inbox",
        lambda *a, **k: mirrored.append((a, k)) or True,
    )
    called_pipeline = {"n": 0}

    async def _boom(*a, **k):
        called_pipeline["n"] += 1

    monkeypatch.setattr(pb, "maybe_auto_reply", _boom)

    handed = await process_official_inbound(
        platform="whatsapp", account_id="PNID", chat_key="wa:user:123",
        text="hi", use_pipeline=True)
    assert handed is True            # 调用方据此跳过自答
    assert mirrored                  # 入站仍镜像进收件箱（System Z 据此触发）
    assert called_pipeline["n"] == 0  # 未走 maybe_auto_reply（让位 System Z，不双发）


async def test_process_inbound_self_reply_path_when_not_auto_ai(monkeypatch):
    _wire(monkeypatch, mode="review", owned=[("whatsapp", "PNID")])
    monkeypatch.setattr(
        "src.integrations.shared.inbox_mirror.mirror_to_inbox",
        lambda *a, **k: True,
    )
    handed = await process_official_inbound(
        platform="whatsapp", account_id="PNID", chat_key="wa:user:123",
        text="hi", use_pipeline=False)
    assert handed is False  # 非 auto_ai + 无管道 → 交调用方自答（零回归）
