"""Messenger RPA web 路由 HTTP 端点测试（TestClient 脚手架首发）。

为本 repo 首次引入 messenger_rpa_routes 的 HTTP 集成测试，后续 web 层
改动（如批量审批/观测字段扩展）可复用本文件的 fixture 继续加 case。

当前覆盖：
- GET /api/messenger-rpa/status —— 无 svc 路径 + pending_empty_count 接入
- GET /api/messenger-rpa/approvals —— 默认 + status / chat_key /
  reply_text_empty 过滤透传
- GET /api/messenger-rpa/approvals/{id} —— 详情 + 404
- POST /api/messenger-rpa/approvals/{id}/update —— reply_text 回填

不覆盖：
- 发送链路（需 MessengerRpaService mock，超出 stage 3 scope）
- HTML 页（/messenger-rpa）—— 模板渲染不是 API 契约
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI
from starlette.testclient import TestClient

from src.integrations.messenger_rpa.state_store import MessengerRpaStateStore
from src.web.routes.messenger_rpa_routes import register_messenger_rpa_routes


class _StubConfigMgr:
    def __init__(self, enabled: bool = True, config_path: Path | None = None) -> None:
        self.config = {
            "messenger_rpa": {
                "enabled": enabled,
                "reply_profiles": {
                    "default": "warm",
                    "profiles": [{"id": "warm", "language": "auto"}],
                },
            }
        }
        self.config_path = config_path or Path("config/config.yaml")
        self.saved = 0

    def save(self) -> bool:
        self.saved += 1
        return True


def _noop_api_auth(request):
    return None


def _noop_page_auth(request):
    return None


@pytest.fixture
def store(tmp_path: Path) -> MessengerRpaStateStore:
    return MessengerRpaStateStore(tmp_path / "msg.db")


@pytest.fixture
def client(store: MessengerRpaStateStore) -> TestClient:
    """无 svc 的 App：触 /status 走 "available=False" 路径，但 store 存在。

    多数 /approvals* 路由不依赖 svc，只读 store，这个 fixture 够用。发送链路
    的集成测试需要 mock svc，留给后续 PR。
    """
    app = FastAPI()
    register_messenger_rpa_routes(
        app,
        page_auth=_noop_page_auth,
        api_auth=_noop_api_auth,
        templates=None,  # /messenger-rpa HTML 路由本套件不调
        config_manager=_StubConfigMgr(enabled=True),
    )
    app.state.messenger_rpa_state_store = store
    app.state.messenger_rpa_service = None  # 显式无 svc
    return TestClient(app)


@pytest.fixture
def client_with_cfg(store: MessengerRpaStateStore, tmp_path: Path) -> tuple[TestClient, _StubConfigMgr]:
    app = FastAPI()
    cm = _StubConfigMgr(enabled=True, config_path=tmp_path / "config.yaml")
    register_messenger_rpa_routes(
        app,
        page_auth=_noop_page_auth,
        api_auth=_noop_api_auth,
        templates=None,
        config_manager=cm,
    )
    app.state.messenger_rpa_state_store = store
    app.state.messenger_rpa_service = None
    return TestClient(app), cm


# ───────────────── /status ─────────────────


def test_status_no_service_returns_available_false(
    client: TestClient, store: MessengerRpaStateStore,
) -> None:
    """svc 未注入时 available=False + enabled_cfg 从 config 读出。"""
    r = client.get("/api/messenger-rpa/status")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is False
    assert body["enabled_cfg"] is True
    assert "hint" in body


def test_status_includes_pending_empty_count(
    client: TestClient, store: MessengerRpaStateStore,
) -> None:
    """pending_empty_count 是 stage 3 新增观测字段，svc 无关从 store 读。"""
    # 空 store → 0
    r0 = client.get("/api/messenger-rpa/status")
    assert r0.json()["pending_empty_count"] == 0

    # 一条 escalation 占位行 + 一条正常 pending
    store.enqueue_approval(
        chat_key="ck:esc", chat_name="E",
        peer_text="q", peer_kind="text", reply_text="",
        allow_empty_reply=True,
    )
    store.enqueue_approval(
        chat_key="ck:n", chat_name="N",
        peer_text="q", peer_kind="text", reply_text="有回复",
    )

    r1 = client.get("/api/messenger-rpa/status")
    assert r1.json()["pending_empty_count"] == 1  # 只数空的


def test_config_get_and_patch(client_with_cfg: tuple[TestClient, _StubConfigMgr]) -> None:
    client, cm = client_with_cfg
    r0 = client.get("/api/messenger-rpa/config")
    assert r0.status_code == 200
    assert r0.json()["operations"]["enabled"] is True

    r1 = client.put(
        "/api/messenger-rpa/config",
        json={
            "autostart": False,
            "reply_mode": "approve",
            "run_once_target_names": "Victor Zan, Test User",
        },
    )
    assert r1.status_code == 200
    assert cm.saved == 1
    cfg = cm.config["messenger_rpa"]
    assert cfg["reply_mode"] == "approve"
    assert cfg["run_once_target_names"] == ["Victor Zan", "Test User"]


def test_personas_update_validates_default(
    client_with_cfg: tuple[TestClient, _StubConfigMgr],
) -> None:
    client, cm = client_with_cfg
    bad = client.put(
        "/api/messenger-rpa/personas",
        json={"default": "missing", "profiles": [{"id": "warm"}]},
    )
    assert bad.status_code == 400

    ok = client.put(
        "/api/messenger-rpa/personas",
        json={
            "default": "sato",
            "profiles": [
                {"id": "sato", "language": "ja", "match_names": ["Victor Zan"]},
            ],
        },
    )
    assert ok.status_code == 200
    assert cm.config["messenger_rpa"]["reply_profiles"]["default"] == "sato"


def test_leads_returns_chat_state_and_profile(
    client_with_cfg: tuple[TestClient, _StubConfigMgr],
    store: MessengerRpaStateStore,
) -> None:
    client, cm = client_with_cfg
    cm.config["messenger_rpa"]["reply_profiles"] = {
        "default": "warm",
        "profiles": [
            {"id": "warm", "language": "auto"},
            {"id": "sato", "language": "ja", "match_names": ["Victor Zan"]},
        ],
    }
    store.update_chat_state(
        "acc_bg_phone_2:Victor Zan",
        chat_name="Victor Zan",
        last_peer_text="こんにちは",
        last_reply="こんばんは",
    )
    r = client.get("/api/messenger-rpa/leads")
    assert r.status_code == 200
    body = r.json()
    assert body["summary"]["total"] >= 1
    item = body["items"][0]
    assert item["chat_name"] == "Victor Zan"
    assert item["persona_id"] == "sato"


# ───────────────── /approvals （列表 + 新 filter） ─────────────────


def test_approvals_list_default_filters_pending(
    client: TestClient, store: MessengerRpaStateStore,
) -> None:
    """默认 status=pending，approved/rejected 不入结果。"""
    aid = store.enqueue_approval(
        chat_key="ck", chat_name="C",
        peer_text="q", peer_kind="text", reply_text="r",
    )
    other = store.enqueue_approval(
        chat_key="ck2", chat_name="C2",
        peer_text="q", peer_kind="text", reply_text="r2",
    )
    store.decide_approval(other, approve=True)

    r = client.get("/api/messenger-rpa/approvals")
    assert r.status_code == 200
    ids = [a["id"] for a in r.json()["approvals"]]
    assert ids == [aid]


def test_approvals_list_reply_text_empty_true_only_escalations(
    client: TestClient, store: MessengerRpaStateStore,
) -> None:
    """stage 3 新增 query：?reply_text_empty=true 仅回 escalation 占位。"""
    esc = store.enqueue_approval(
        chat_key="ck:e", chat_name="E",
        peer_text="q", peer_kind="text", reply_text="",
        allow_empty_reply=True,
    )
    store.enqueue_approval(
        chat_key="ck:n", chat_name="N",
        peer_text="q", peer_kind="text", reply_text="有回复",
    )

    r = client.get(
        "/api/messenger-rpa/approvals",
        params={"reply_text_empty": "true"},
    )
    assert r.status_code == 200
    ids = [a["id"] for a in r.json()["approvals"]]
    assert ids == [esc]


def test_approvals_list_reply_text_empty_false_only_drafts(
    client: TestClient, store: MessengerRpaStateStore,
) -> None:
    store.enqueue_approval(
        chat_key="ck:e", chat_name="E",
        peer_text="q", peer_kind="text", reply_text="",
        allow_empty_reply=True,
    )
    normal = store.enqueue_approval(
        chat_key="ck:n", chat_name="N",
        peer_text="q", peer_kind="text", reply_text="有回复",
    )

    r = client.get(
        "/api/messenger-rpa/approvals",
        params={"reply_text_empty": "false"},
    )
    assert r.status_code == 200
    ids = [a["id"] for a in r.json()["approvals"]]
    assert ids == [normal]


def test_approvals_list_without_filter_returns_both(
    client: TestClient, store: MessengerRpaStateStore,
) -> None:
    """不传 reply_text_empty 时行为同 pre-stage-3（空+非空都回）。"""
    a1 = store.enqueue_approval(
        chat_key="ck:e", chat_name="E",
        peer_text="q", peer_kind="text", reply_text="",
        allow_empty_reply=True,
    )
    a2 = store.enqueue_approval(
        chat_key="ck:n", chat_name="N",
        peer_text="q", peer_kind="text", reply_text="r",
    )

    r = client.get("/api/messenger-rpa/approvals")
    ids = {a["id"] for a in r.json()["approvals"]}
    assert ids == {a1, a2}


def test_approvals_detail_exists_and_404(
    client: TestClient, store: MessengerRpaStateStore,
) -> None:
    aid = store.enqueue_approval(
        chat_key="ck", chat_name="C",
        peer_text="q", peer_kind="text", reply_text="r",
    )
    r1 = client.get(f"/api/messenger-rpa/approvals/{aid}")
    assert r1.status_code == 200
    assert r1.json()["reply_text"] == "r"

    r2 = client.get("/api/messenger-rpa/approvals/999999")
    assert r2.status_code == 404


# ───────────────── /approvals/{id}/update ─────────────────


def test_approval_update_fills_escalation_placeholder(
    client: TestClient, store: MessengerRpaStateStore,
) -> None:
    """escalation 端到端：空占位 → /update 回填 → store 读到新文案。"""
    aid = store.enqueue_approval(
        chat_key="ck:esc", chat_name="E",
        peer_text="q", peer_kind="text", reply_text="",
        allow_empty_reply=True,
    )
    r = client.post(
        f"/api/messenger-rpa/approvals/{aid}/update",
        json={"reply_text": "人工回复"},
    )
    assert r.status_code == 200
    assert r.json()["reply_text"] == "人工回复"

    row = store.get_approval(aid)
    assert row["reply_text"] == "人工回复"
    assert row["status"] == "pending"  # 回填不 decide


def test_approval_update_rejects_empty_reply(
    client: TestClient, store: MessengerRpaStateStore,
) -> None:
    """API 层 400：/update 不允许再把 reply_text 清空。"""
    aid = store.enqueue_approval(
        chat_key="ck", chat_name="C",
        peer_text="q", peer_kind="text", reply_text="r",
    )
    r = client.post(
        f"/api/messenger-rpa/approvals/{aid}/update",
        json={"reply_text": "   "},
    )
    assert r.status_code == 400


def test_approval_update_on_decided_returns_409(
    client: TestClient, store: MessengerRpaStateStore,
) -> None:
    """已 approved 的 approval 不能再 /update（非 pending → 409）。"""
    aid = store.enqueue_approval(
        chat_key="ck", chat_name="C",
        peer_text="q", peer_kind="text", reply_text="r",
    )
    store.decide_approval(aid, approve=True)
    r = client.post(
        f"/api/messenger-rpa/approvals/{aid}/update",
        json={"reply_text": "新文案"},
    )
    assert r.status_code == 409
