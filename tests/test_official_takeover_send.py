"""Phase G4b：官方渠道坐席接管发送闭环单测。

坐席从统一收件箱「发送」→ send_via_adapters → orch.send → OfficialApiWorker.send。
关键：收件箱 chat_key 形如 ``wa:user:<num>`` / ``line:user:<uid>`` / ``fb:user:<psid>``，
worker 必须归一为裸收件人标识再喂官方 send 助手，否则发错人。
"""
import pytest

from src.integrations.official_api_worker import (
    OfficialApiWorker, dest_from_chat_key,
)


# ── chat_key 归一 ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("chat_key,expect", [
    ("wa:user:8613800138000", "8613800138000"),
    ("line:user:Uabc123", "Uabc123"),
    ("line:group:Gxyz", "Gxyz"),
    ("line:room:Rooom", "Rooom"),
    ("fb:user:1234567890", "1234567890"),
    ("Uabc123", "Uabc123"),            # 已是裸标识 → 幂等
    ("8613800138000", "8613800138000"),
    ("", ""),
])
def test_dest_from_chat_key(chat_key, expect):
    assert dest_from_chat_key(chat_key) == expect


# ── worker.send 用归一后的 dest 调官方助手 ───────────────────────────────────

async def test_wa_worker_send_normalizes_dest(monkeypatch):
    captured = {}
    async def _fake_wa(to, text, pnid, token, **k):
        captured.update(to=to, text=text, pnid=pnid)
        return {"ok": True, "data": {"messages": [{"id": "m1"}]}}
    import src.integrations.whatsapp_cloud as wac
    monkeypatch.setattr(wac, "wa_send_text", _fake_wa)

    acc = {"platform": "whatsapp", "account_id": "PNID",
           "meta": {"access_token": "T", "phone_number_id": "PNID"}}
    w = OfficialApiWorker(acc, {})
    await w.start()
    out = await w.send("wa:user:8613800138000", "你好")
    assert out["delivered"] is True and out["message_id"] == "m1"
    assert captured["to"] == "8613800138000"   # ★ 裸号码，不是 wa:user:...

async def test_line_worker_send_normalizes_dest(monkeypatch):
    captured = {}
    async def _fake_push(to, text, token, *, account_id="default"):
        captured.update(to=to, account_id=account_id)
        return True
    import src.integrations.line_webhook as lw
    monkeypatch.setattr(lw, "line_push", _fake_push)

    acc = {"platform": "line", "account_id": "official",
           "meta": {"channel_access_token": "T"}}
    w = OfficialApiWorker(acc, {})
    await w.start()
    out = await w.send("line:user:Uabc", "hi")
    assert out["delivered"] is True
    assert captured["to"] == "Uabc" and captured["account_id"] == "official"

async def test_fb_worker_send_normalizes_dest_and_passes_account(monkeypatch):
    captured = {}
    async def _fake_fb(psid, text, token, *, fallback_tag="ACCOUNT_UPDATE", account_id="default"):
        captured.update(psid=psid, account_id=account_id)
        return {"ok": True, "data": {"message_id": "mid9"}}
    import src.integrations.facebook_webhook as fbw
    monkeypatch.setattr(fbw, "fb_send_with_window_fallback", _fake_fb)

    acc = {"platform": "messenger", "account_id": "PAGE9",
           "meta": {"page_access_token": "T"}}
    w = OfficialApiWorker(acc, {})
    await w.start()
    out = await w.send("fb:user:1234567890", "yo")
    assert out["delivered"] is True and out["message_id"] == "mid9"
    assert captured["psid"] == "1234567890" and captured["account_id"] == "PAGE9"


# ── 端到端：orch.send 路由（owns→worker.send）保留前缀 chat_key 出站回写 ──────

async def test_orchestrator_send_routes_to_official_worker(monkeypatch):
    import src.integrations.account_orchestrator as ao
    import src.integrations.protocol_bridge as pb

    mirrored = []
    monkeypatch.setattr(pb, "_sink", lambda m: mirrored.append(m), raising=False)

    sent = {}
    async def _fake_wa(to, text, pnid, token, **k):
        sent.update(to=to, text=text)
        return {"ok": True, "data": {"messages": [{"id": "x1"}]}}
    import src.integrations.whatsapp_cloud as wac
    monkeypatch.setattr(wac, "wa_send_text", _fake_wa)

    orch = ao.AccountOrchestrator()
    acc = {"platform": "whatsapp", "account_id": "PNID",
           "meta": {"access_token": "T", "phone_number_id": "PNID"}}
    worker = OfficialApiWorker(acc, {})
    await worker.start()
    # 注入受管 worker（模拟 sync_from_registry 已建好 running worker）
    m = ao._Managed(key=ao.account_key("whatsapp", "PNID"), platform="whatsapp",
                    account_id="PNID", mode="official", worker=worker, state="running")
    orch._managed[m.key] = m

    assert orch.owns("whatsapp", "PNID") is True
    res = await orch.send("whatsapp", "PNID", "wa:user:8613800138000", "在的")
    assert res["delivered"] is True
    # worker 收到的是裸号码
    assert sent["to"] == "8613800138000"
    # 出站镜像回写收件箱时保留前缀 chat_key（线程分组一致）
    outs = [e for e in mirrored if e["direction"] == "out"]
    assert outs and outs[0]["chat_key"] == "wa:user:8613800138000"
