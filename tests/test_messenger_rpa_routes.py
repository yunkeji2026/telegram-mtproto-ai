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
import json
import sqlite3
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI
from starlette.testclient import TestClient

from src.integrations.messenger_rpa.state_store import (
    MessengerRpaStateStore,
    default_state_db_path,
)
import src.web.routes.messenger_rpa_routes as messenger_routes
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


def test_strategy_runtime_and_simulate_are_backend_backed(
    client_with_cfg: tuple[TestClient, _StubConfigMgr],
    store: MessengerRpaStateStore,
) -> None:
    client, _cm = client_with_cfg
    store.upsert_strategy_account(
        account_id="acc_ja",
        label="JA account",
        supported_languages=["ja"],
        supported_customer_types=["lead"],
        persona_ids=["sato"],
        health_score=91,
        current_load=1,
    )
    store.upsert_persona(
        persona_id="sato",
        name="Sato",
        language="ja",
        customer_type="lead",
        facts=["occupation: engineer"],
        persona={"name": "Sato"},
    )
    store.update_conversation_state(
        "chat:akiko",
        chat_key="chat:akiko",
        account_id="acc_ja",
        persona_id="sato",
        customer_language="ja",
        customer_type="lead",
        stage="qualification",
        memory_summary="asked about price",
    )
    store.enqueue_auto_run_message(
        customer_id="chat:akiko",
        chat_key="chat:akiko",
        text="料金は？",
        language="ja",
        account_id="acc_ja",
        persona_id="sato",
        stage="qualification",
        message_id="m-akiko-route",
    )

    runtime = client.get("/api/messenger-rpa/strategy/runtime")
    assert runtime.status_code == 200
    body = runtime.json()
    assert body["available"] is True
    assert body["summary"]["accounts"] == 1
    assert body["summary"]["pending_jobs"] == 1
    assert body["accounts"][0]["account_id"] == "acc_ja"
    assert body["conversation_states"][0]["stage"] == "qualification"

    before_jobs = len(store.list_auto_run_jobs(status="all"))
    sim = client.post(
        "/api/messenger-rpa/strategy/simulate",
        json={
            "customer_id": "chat:akiko",
            "chat_key": "chat:akiko",
            "text": "こんにちは、料金を知りたいです",
        },
    )
    assert sim.status_code == 200
    data = sim.json()
    assert data["dry_run"] is True
    assert data["account_id"] == "acc_ja"
    assert data["persona_id"] == "sato"
    assert data["language"] == "ja"
    assert len(store.list_auto_run_jobs(status="all")) == before_jobs


def test_strategy_runtime_operator_mutations(
    client_with_cfg: tuple[TestClient, _StubConfigMgr],
    store: MessengerRpaStateStore,
) -> None:
    client, cm = client_with_cfg
    cm.config["messenger_rpa"]["reply_profiles"] = {
        "default": "sato",
        "profiles": [
            {
                "id": "sato",
                "language": "ja",
                "style_hint": "old style",
                "persona": {
                    "name": "Old",
                    "role": "old role",
                    "speaking": {"forbidden_phrases": ["old"]},
                },
            }
        ],
    }
    store.upsert_strategy_account(
        account_id="acc_ja",
        label="JA account",
        supported_languages=["ja"],
        supported_customer_types=["lead"],
        persona_ids=["sato"],
    )
    store.update_conversation_state(
        "chat:akiko",
        chat_key="chat:akiko",
        account_id="acc_ja",
        persona_id="sato",
        stage="qualification",
        used_persona_facts=["occupation: engineer"],
    )
    job_id = store.enqueue_auto_run_message(
        customer_id="chat:akiko",
        chat_key="chat:akiko",
        text="料金は？",
        account_id="acc_ja",
        persona_id="sato",
        message_id="m-mutate-1",
    )

    p = client.patch(
        "/api/messenger-rpa/strategy/personas/sato",
        json={
            "name": "佐藤",
            "language": "ja",
            "customer_type": "lead",
            "style_hint": "new style",
            "match_names": ["Akiko"],
            "forbidden_phrases": ["AIです"],
            "background_facts": ["occupation: engineer"],
        },
    )
    assert p.status_code == 200
    profile = cm.config["messenger_rpa"]["reply_profiles"]["profiles"][0]
    assert profile["style_hint"] == "new style"
    assert profile["persona"]["name"] == "佐藤"
    assert profile["persona"]["speaking"]["forbidden_phrases"] == ["AIです"]

    a = client.patch(
        "/api/messenger-rpa/strategy/accounts/acc_ja",
        json={
            "status": "warming",
            "supported_languages": ["ja", "en"],
            "supported_customer_types": ["lead"],
            "max_daily_send": 80,
        },
    )
    assert a.status_code == 200
    account = store.list_strategy_accounts()[0]
    assert account["status"] == "warming"
    assert account["supported_languages"] == ["ja", "en"]
    assert account["max_daily_send"] == 80

    c = client.patch(
        "/api/messenger-rpa/strategy/conversations/chat:akiko",
        json={"action": "clear_used_facts"},
    )
    assert c.status_code == 200
    assert store.get_conversation_state("chat:akiko")["used_persona_facts"] == []

    r = client.post(f"/api/messenger-rpa/strategy/jobs/{job_id}/retry", json={})
    assert r.status_code == 200
    assert store.list_auto_run_jobs(status="all")[0]["status"] == "pending"
    x = client.post(f"/api/messenger-rpa/strategy/jobs/{job_id}/cancel", json={})
    assert x.status_code == 200
    assert store.list_auto_run_jobs(status="all")[0]["status"] == "canceled"

    newp = client.post(
        "/api/messenger-rpa/strategy/personas",
        json={"id": "fresh", "name": "Fresh", "language": "en"},
    )
    assert newp.status_code == 200
    assert any(
        p["id"] == "fresh"
        for p in cm.config["messenger_rpa"]["reply_profiles"]["profiles"]
    )

    copyp = client.post(
        "/api/messenger-rpa/strategy/personas",
        json={"action": "copy", "id": "sato_copy", "source_id": "sato"},
    )
    assert copyp.status_code == 200
    assert any(
        p["id"] == "sato_copy"
        for p in cm.config["messenger_rpa"]["reply_profiles"]["profiles"]
    )

    disabled = client.post(
        "/api/messenger-rpa/strategy/personas/sato/disable", json={}
    )
    assert disabled.status_code == 200
    sato = next(
        p for p in cm.config["messenger_rpa"]["reply_profiles"]["profiles"]
        if p["id"] == "sato"
    )
    assert sato["status"] == "disabled"

    defaulted = client.post(
        "/api/messenger-rpa/strategy/personas/fresh/set_default", json={}
    )
    assert defaulted.status_code == 200
    assert cm.config["messenger_rpa"]["reply_profiles"]["default"] == "fresh"

    summary = client.patch(
        "/api/messenger-rpa/strategy/conversations/chat:akiko",
        json={"action": "update", "memory_summary": "edited summary"},
    )
    assert summary.status_code == 200
    assert store.get_conversation_state("chat:akiko")["memory_summary"] == "edited summary"

    audit_runtime = client.get("/api/messenger-rpa/strategy/runtime")
    assert audit_runtime.status_code == 200
    body = audit_runtime.json()
    audit = body["audit"]
    assert any(x["action"] == "persona.disable" for x in audit)
    assert any(x["action"] == "account.update" for x in audit)
    assert body["jobs"][0]["incoming_text"] == "料金は？"

    acc_audit = next(x for x in audit if x["action"] == "account.update")
    rollback = client.post(
        f"/api/messenger-rpa/strategy/audit/{acc_audit['id']}/rollback",
        json={},
    )
    assert rollback.status_code == 200
    rolled = next(a for a in store.list_strategy_accounts() if a["account_id"] == "acc_ja")
    assert rolled["status"] == "active"
    assert rolled["max_daily_send"] == 200


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


def test_lead_detail_returns_handoff_dossier(
    client_with_cfg: tuple[TestClient, _StubConfigMgr],
    store: MessengerRpaStateStore,
    tmp_path: Path,
) -> None:
    client, cm = client_with_cfg
    cm.config["messenger_rpa"].update({
        "accounts": [{
            "id": "bg_phone_2",
            "label": "05号测试机",
            "adb_serial": "SERIAL05",
            "device_number": "05",
            "device_alias": "05号",
            "login_account": "fb_login_05",
            "reply_profile_id": "warm",
            "line_id": "@line05",
        }],
    })
    chat_key = "acc_bg_phone_2:Victor Zan"
    account_store = MessengerRpaStateStore(
        default_state_db_path(cm.config_path, "bg_phone_2"),
        account_id="bg_phone_2",
    )
    now = time.time()
    account_store.update_chat_state(
        chat_key,
        chat_name="Victor Zan",
        last_peer_text="私は東京で会社を経営しています",
        last_reply="お仕事お疲れさまです",
        last_sent_at=now - 20,
    )
    account_store.append_run({
        "ts": now - 60,
        "chat_key": chat_key,
        "chat_name": "Victor Zan",
        "ok": True,
        "step": "sent",
        "peer_text": "私は東京で会社を経営しています",
        "peer_kind": "text",
        "reply_text": "お仕事お疲れさまです",
    })
    account_store.enqueue_approval(
        chat_key=chat_key,
        chat_name="Victor Zan",
        peer_text="LINEでも話せますか？",
        peer_kind="text",
        reply_text="もちろんです",
        reply_lang="ja",
    )
    db = tmp_path / "bot.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE user_context (user_id TEXT PRIMARY KEY, data TEXT, updated_at REAL)")
    c.execute(
        "INSERT INTO user_context(user_id, data, updated_at) VALUES (?, ?, ?)",
        (
            chat_key,
            json.dumps({
                "chat_title": "Victor Zan",
                "reply_lang": "ja",
                "last_message": "LINEでも話せますか？",
                "last_reply": "もちろんです",
                "_conversation_summary": "東京の経営者として仕事の話をしている。",
                "_conversation_history": [
                    {"role": "user", "content": "私は東京で会社を経営しています"},
                    {"role": "assistant", "content": "お仕事お疲れさまです"},
                ],
                "lead_qualification": {
                    "icp_score": 86,
                    "stage": "HANDOFF_READY",
                    "country": "JP",
                    "gender": "female",
                    "age_range": "40-50",
                    "occupation": "会社経営",
                    "occupation_tier": "high_income_signal",
                    "income_band": "high",
                    "missing_fields": [],
                    "evidence": ["occupation:owner", "income:high"],
                },
            }, ensure_ascii=False),
            now,
        ),
    )
    c.commit()
    c.close()

    r = client.get(f"/api/messenger-rpa/leads/{chat_key}")
    assert r.status_code == 200
    body = r.json()
    assert body["lead"]["score"] == 86
    assert body["account"]["account_id"] == "bg_phone_2"
    assert body["account"]["device_number"] == "05"
    assert body["customer_profile"]["occupation"] == "会社経営"
    assert body["handoff_brief"]["handoff_advice"]["line_ready"] is True
    assert body["operator_handoff"]["status"] == "new"
    assert body["timeline"]
    assert body["history_turns"][0]["role"] == "user"


def test_lead_handoff_update_persists_to_list_and_detail(
    client_with_cfg: tuple[TestClient, _StubConfigMgr],
    store: MessengerRpaStateStore,
) -> None:
    client, cm = client_with_cfg
    cm.config["messenger_rpa"].update({
        "accounts": [{
            "id": "bg_phone_2",
            "label": "05号测试机",
            "reply_profile_id": "warm",
            "line_id": "@line05",
        }],
    })
    chat_key = "acc_bg_phone_2:Victor Zan"
    account_store = MessengerRpaStateStore(
        default_state_db_path(cm.config_path, "bg_phone_2"),
        account_id="bg_phone_2",
    )
    account_store.update_chat_state(
        chat_key,
        chat_name="Victor Zan",
        last_peer_text="LINEでも話せますか？",
        last_reply="もちろんです",
    )

    bad = client.put(
        f"/api/messenger-rpa/leads/{chat_key}/handoff",
        json={"status": "unknown"},
    )
    assert bad.status_code == 400

    ok = client.put(
        f"/api/messenger-rpa/leads/{chat_key}/handoff",
        json={
            "owner": "客服A",
            "status": "in_progress",
            "line_status": "sent",
            "priority": "high",
            "outcome": "已发送LINE，等待添加",
            "notes": "客户对继续沟通有兴趣，下一步人工客服承接。",
            "next_followup_at": 1893456000,
        },
    )
    assert ok.status_code == 200
    saved = ok.json()["handoff"]
    assert saved["account_id"] == "bg_phone_2"
    assert saved["owner"] == "客服A"
    assert saved["status"] == "in_progress"
    assert saved["line_status"] == "sent"

    detail = client.get(f"/api/messenger-rpa/leads/{chat_key}").json()
    assert detail["operator_handoff"]["owner"] == "客服A"
    assert detail["operator_handoff"]["priority"] == "high"
    assert detail["operator_handoff"]["next_followup_at"] == 1893456000

    listing = client.get("/api/messenger-rpa/leads").json()
    assert listing["summary"]["handoff_statuses"]["in_progress"] >= 1
    assert listing["summary"]["line_statuses"]["sent"] >= 1
    assert listing["summary"]["actions"]["active_followup"] >= 1
    item = next(x for x in listing["items"] if x["chat_key"] == chat_key)
    assert item["operator_handoff"]["status"] == "in_progress"
    assert item["operator_handoff"]["line_status"] == "sent"


def test_lead_detail_derives_icp_from_history_when_profile_missing(
    client_with_cfg: tuple[TestClient, _StubConfigMgr],
    tmp_path: Path,
) -> None:
    client, cm = client_with_cfg
    cm.config["messenger_rpa"].update({
        "accounts": [{
            "id": "bg_phone_2",
            "label": "05号测试机",
            "reply_profile_id": "warm",
            "line_id": "@line05",
        }],
        "lead_qualification": {
            "enabled": True,
            "target": {"country": "JP", "gender": "female", "age_min": 37, "age_max": 60},
            "min_score_for_line": 80,
            "handoff": {"line_id": "@line05", "min_turns_before_send": 6},
        },
    })
    chat_key = "acc_bg_phone_2:Yumi"
    account_store = MessengerRpaStateStore(
        default_state_db_path(cm.config_path, "bg_phone_2"),
        account_id="bg_phone_2",
    )
    account_store.update_chat_state(
        chat_key,
        chat_name="Yumi",
        last_peer_text="詳しく相談したいです。",
    )
    db = tmp_path / "bot.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE user_context (user_id TEXT PRIMARY KEY, data TEXT, updated_at REAL)")
    c.execute(
        "INSERT INTO user_context(user_id, data, updated_at) VALUES (?, ?, ?)",
        (
            chat_key,
            json.dumps({
                "chat_title": "Yumi",
                "reply_lang": "ja",
                "_conversation_history": [
                    {"role": "user", "content": "日本の港区に住んでいる女性です。娘は25歳で独立しました。"},
                    {"role": "user", "content": "今は離婚して一人暮らしで少し寂しいです。"},
                    {"role": "user", "content": "美容サロンを経営していて、ゴルフと海外旅行が好きです。"},
                    {"role": "user", "content": "詳しく相談したいです。"},
                ],
            }, ensure_ascii=False),
            time.time(),
        ),
    )
    c.commit()
    c.close()

    r = client.get(f"/api/messenger-rpa/leads/{chat_key}")
    assert r.status_code == 200
    lead = r.json()["lead"]["lead"]
    assert lead["derived_from_history"] is True
    assert lead["icp_score"] >= 80
    assert lead["occupation_tier"] == "high_income_signal"
    assert "income_signal" not in lead["missing_fields"]


def test_bindings_read_mobile_auto_and_patch_account(
    client_with_cfg: tuple[TestClient, _StubConfigMgr],
    tmp_path: Path,
) -> None:
    client, cm = client_with_cfg
    root = tmp_path / "mobile-auto"
    cfg_dir = root / "config"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "device_aliases.json").write_text(
        json.dumps({
            "SERIAL05": {"number": 5, "alias": "05号", "display_name": "Pixel"}
        }),
        encoding="utf-8",
    )
    (cfg_dir / "device_registry.json").write_text(
        json.dumps({
            "hw-05": {
                "current_serial": "SERIAL05",
                "previous_serials": ["OLD05"],
                "model": "Pixel",
                "number": 5,
                "alias": "05号",
            }
        }),
        encoding="utf-8",
    )
    (cfg_dir / "chat.yaml").write_text(
        "device_aliases:\n  \"05\": \"SERIAL05\"\n",
        encoding="utf-8",
    )
    cm.config["messenger_rpa"].update({
        "mobile_auto": {"root_path": str(root)},
        "accounts": [{"id": "acc05", "label": "05号测试机", "adb_serial": "SERIAL05"}],
    })

    r0 = client.get("/api/messenger-rpa/bindings")
    assert r0.status_code == 200
    body = r0.json()
    assert body["binding_summary"]["mapped_devices"] == 1
    assert body["bindings"][0]["device_number"] == "05"

    r1 = client.put(
        "/api/messenger-rpa/bindings",
        json={
            "accounts": [{
                "account_id": "acc05",
                "reply_profile_id": "warm",
                "login_account": "fb_login_05",
                "line_id": "@line05",
            }]
        },
    )
    assert r1.status_code == 200
    acc = cm.config["messenger_rpa"]["accounts"][0]
    assert acc["reply_profile_id"] == "warm"
    assert acc["login_account"] == "fb_login_05"
    assert acc["line_id"] == "@line05"


def test_mobile_auto_status_aggregates_device_runtime(
    client_with_cfg: tuple[TestClient, _StubConfigMgr],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, cm = client_with_cfg
    root = tmp_path / "mobile-auto"
    cfg_dir = root / "config"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "device_aliases.json").write_text(
        json.dumps({
            "SERIAL05": {"number": 5, "alias": "05号"},
            "W03SER": {"number": 9, "alias": "09号"},
        }),
        encoding="utf-8",
    )
    (cfg_dir / "device_registry.json").write_text(
        json.dumps({"hw-05": {"current_serial": "SERIAL05", "number": 5}}),
        encoding="utf-8",
    )
    cm.config["messenger_rpa"].update({
        "mobile_auto": {"root_path": str(root), "api_base": "http://mobile"},
        "accounts": [{"id": "acc05", "adb_serial": "SERIAL05"}],
    })

    def fake_get_json(base: str, path: str, *, timeout: float = 4.0):
        assert base == "http://mobile"
        if path == "/devices":
            return [{"device_id": "SERIAL05", "status": "connected", "busy": False}]
        if path == "/cluster/devices":
            return {"devices": [
                {"device_id": "W03SER", "status": "connected", "host_name": "W03", "host_id": "w03"},
            ]}
        if path == "/devices/performance/all":
            return {"devices": {
                "SERIAL05": {"battery_level": 88, "mem_usage": 31},
                "W03SER": {"battery_level": 76, "mem_usage": 42},
            }}
        if path == "/vpn/status":
            return {"devices": [
                {"device_id": "SERIAL05", "connected": True, "country": "JP"},
                {"device_id": "W03SER", "connected": True, "country": "JP"},
            ]}
        if path == "/tasks?limit=300":
            return [{"device_id": "SERIAL05", "status": "running", "type": "facebook_check_inbox"}]
        if path == "/screen-stats":
            return {"health_avg": 79}
        raise AssertionError(path)

    monkeypatch.setattr(messenger_routes, "_mobile_auto_get_json", fake_get_json)
    r = client.get("/api/messenger-rpa/mobile-auto/status")
    assert r.status_code == 200
    body = r.json()
    assert body["summary"]["devices_total"] == 2
    assert body["summary"]["devices_online"] == 2
    assert body["summary"]["cluster_devices"] == 1
    assert body["summary"]["hosts_total"] == 1
    assert body["summary"]["vpn_connected"] == 2
    row = body["accounts"][0]
    assert row["account_id"] == "acc05"
    assert row["online"] is True
    assert row["battery_level"] == 88
    assert row["vpn_connected"] is True
    assert row["task_status"] == "running"
    worker = next(d for d in body["devices"] if d["device_id"] == "W03SER")
    assert worker["row_id"] == "device:W03SER"
    assert worker["is_cluster"] is True
    assert worker["host_name"] == "W03"
    assert worker["device_number"] == "09"
    assert worker["device_alias"] == "09号"
    assert worker["screen_url"].endswith("/mobile-auto/cluster/devices/W03SER/screenshot")


def test_mobile_auto_device_action_proxies_allowed_actions(
    client_with_cfg: tuple[TestClient, _StubConfigMgr],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, cm = client_with_cfg
    cm.config["messenger_rpa"].update({
        "mobile_auto": {"api_base": "http://mobile"},
    })
    calls: list[tuple[str, str, dict]] = []

    def fake_post_json(base: str, path: str, body: dict | None = None, *, timeout: float = 8.0):
        calls.append((base, path, body or {}))
        if path.endswith("/shell") and (body or {}).get("command") == "wm size":
            return {"ok": True, "output": "Physical size: 1080x2400"}
        return {"forwarded": path}

    def fake_get_json(base: str, path: str, *, timeout: float = 4.0):
        assert base == "http://mobile"
        if path == "/devices/SERIAL05/screen-size":
            return {"width": 1080, "height": 1920}
        raise AssertionError(path)

    monkeypatch.setattr(messenger_routes, "_mobile_auto_post_json", fake_post_json)
    monkeypatch.setattr(messenger_routes, "_mobile_auto_get_json", fake_get_json)
    r = client.post(
        "/api/messenger-rpa/mobile-auto/devices/W03SER/action",
        json={"action": "reconnect", "is_cluster": True},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["path"] == "/cluster/batch-reconnect"
    assert calls == [("http://mobile", "/cluster/batch-reconnect", {})]

    tap = client.post(
        "/api/messenger-rpa/mobile-auto/devices/W03SER/action",
        json={"action": "tap", "is_cluster": True, "x": 123, "y": 456},
    )
    assert tap.status_code == 200
    assert calls[-1] == (
        "http://mobile",
        "/cluster/devices/W03SER/input/tap",
        {"x": 123, "y": 456},
    )

    key = client.post(
        "/api/messenger-rpa/mobile-auto/devices/SERIAL05/action",
        json={"action": "key_home"},
    )
    assert key.status_code == 200
    assert calls[-1] == (
        "http://mobile",
        "/devices/SERIAL05/input/key",
        {"keycode": 3},
    )

    app = client.post(
        "/api/messenger-rpa/mobile-auto/devices/W03SER/action",
        json={"action": "open_messenger", "is_cluster": True},
    )
    assert app.status_code == 200
    assert calls[-1][1] == "/cluster/devices/W03SER/shell"
    assert calls[-1][2]["command"].startswith("monkey -p com.facebook.orca ")

    ratio = client.post(
        "/api/messenger-rpa/mobile-auto/devices/SERIAL05/action",
        json={"action": "tap_ratio", "x_ratio": 0.5, "y_ratio": 0.25, "image_width": 405, "image_height": 720},
    )
    assert ratio.status_code == 200
    assert calls[-1] == (
        "http://mobile",
        "/devices/SERIAL05/input/tap",
        {"x": 540, "y": 480},
    )

    cluster_ratio = client.post(
        "/api/messenger-rpa/mobile-auto/devices/W03SER/action",
        json={"action": "tap_ratio", "is_cluster": True, "x_ratio": 0.25, "y_ratio": 0.5, "image_width": 405, "image_height": 720},
    )
    assert cluster_ratio.status_code == 200
    assert calls[-2] == (
        "http://mobile",
        "/cluster/devices/W03SER/shell",
        {"command": "wm size"},
    )
    assert calls[-1] == (
        "http://mobile",
        "/cluster/devices/W03SER/input/tap",
        {"x": 270, "y": 1200},
    )

    bad = client.post(
        "/api/messenger-rpa/mobile-auto/devices/W03SER/action",
        json={"action": "shell"},
    )
    assert bad.status_code == 400

    bad_pkg = client.post(
        "/api/messenger-rpa/mobile-auto/devices/W03SER/action",
        json={"action": "open_messenger", "package": "com.android.settings"},
    )
    assert bad_pkg.status_code == 400


def test_media_config_get_and_patch(
    client_with_cfg: tuple[TestClient, _StubConfigMgr],
) -> None:
    client, cm = client_with_cfg
    cm.config["messenger_rpa"]["voice_output"] = {
        "voice_profile": {
            "command_args": [
                "python", "tools/glm_tts_infer.py",
                "--text", "{text}", "--ref", "{reference_audio}", "--out", "{out}",
            ],
        },
    }
    r0 = client.get("/api/messenger-rpa/media")
    assert r0.status_code == 200
    assert r0.json()["capabilities"]["receive_image"] is True

    r1 = client.put(
        "/api/messenger-rpa/media",
        json={
            "media_handling_policy": "ai",
            "media_deep_understand": {"enabled": True, "timeout_sec": 5},
            "voice_input": {
                "enabled": True,
                "prefer_transcribe": True,
                "capture_mode": "screenrecord",
                "audio_pipeline": {
                    "backend": "openai",
                    "model": "whisper-1",
                    "language": "ja",
                },
            },
            "voice_output": {
                "enabled": True,
                "mode": "approval_only",
                "backend": "voice_clone_command",
                "voice": "my_voice",
                "voice_profile": {
                    "enabled": True,
                    "owner_consent": True,
                    "speaker_id": "my_voice",
                    "reference_audio_path": "voice_samples/my_voice.wav",
                    "backend": "voice_clone_command",
                    "command_template": "clone --text {text} --ref {reference_audio} --out {out}",
                },
                "send_text_summary": True,
            },
        },
    )
    assert r1.status_code == 200
    mr = cm.config["messenger_rpa"]
    assert mr["media_handling_policy"] == "ai"
    assert mr["media_deep_understand"]["timeout_sec"] == 5
    assert mr["voice_input"]["enabled"] is True
    assert mr["voice_input"]["capture_mode"] == "screenrecord"
    assert mr["voice_input"]["audio_pipeline"]["backend"] == "openai"
    assert mr["voice_output"]["enabled"] is True
    assert mr["voice_output"]["voice_profile"]["speaker_id"] == "my_voice"
    assert mr["voice_output"]["voice_profile"]["command_args"][0] == "python"

    r2 = client.get("/api/messenger-rpa/media")
    assert r2.status_code == 200
    body = r2.json()
    assert body["voice_runtime"]["input"]["capture_mode"] == "screenrecord"
    assert body["voice_runtime"]["output"]["backend"] == "voice_clone_command"
    assert body["tts_pipeline"]["voice_profile_enabled"] is True


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



# ════════════════════════════════════════════════════════════════════
#  P1-E2: 紧急停发 API（运营舆情应急一键灭火）
# ════════════════════════════════════════════════════════════════════


from unittest.mock import MagicMock


@pytest.fixture
def client_with_svc(
    store: MessengerRpaStateStore, tmp_path: Path,
):
    """P1-E2 fixture：注入带 mock svc + 真 state_store 的 client。
    紧急停发路由需要 svc._account_registry 和 svc._get_or_create_runner。"""
    from src.integrations.messenger_rpa.runner import _PersistentSelfSkipDict

    # mock runner：持有真 state_store，模拟 _self_skip_until 行为
    runner = MagicMock()
    runner._chat_key_prefix = 'test'
    runner._state = store
    runner._self_skip_until = _PersistentSelfSkipDict(store)

    # mock account context（reg.get(account_id) 返回非 None 即认为存在）
    ctx = MagicMock()

    # mock account_registry
    reg = MagicMock()
    reg.get = MagicMock(side_effect=lambda aid: ctx if aid == 'acc1' else None)

    # mock service
    svc = MagicMock()
    svc._account_registry = reg
    svc._get_or_create_runner = MagicMock(return_value=runner)

    app = FastAPI()
    register_messenger_rpa_routes(
        app,
        page_auth=_noop_page_auth,
        api_auth=_noop_api_auth,
        templates=None,
        config_manager=_StubConfigMgr(enabled=True),
    )
    app.state.messenger_rpa_state_store = store
    app.state.messenger_rpa_service = svc
    return TestClient(app), runner, svc


def test_emergency_stop_requires_chat_name(client_with_svc):
    client, runner, svc = client_with_svc
    r = client.post(
        '/api/messenger-rpa/accounts/acc1/chats/emergency_stop',
        json={},
    )
    assert r.status_code == 400


def test_emergency_stop_unknown_account_404(client_with_svc):
    client, runner, svc = client_with_svc
    r = client.post(
        '/api/messenger-rpa/accounts/acc_does_not_exist/chats/emergency_stop',
        json={'chat_name': 'Alice'},
    )
    assert r.status_code == 404


def test_emergency_stop_writes_blacklist(client_with_svc, store):
    client, runner, svc = client_with_svc
    r = client.post(
        '/api/messenger-rpa/accounts/acc1/chats/emergency_stop',
        json={
            'chat_name': 'Yunshan Zan',
            'reason': '运营测试',
            'self_skip_sec': 0,  # 仅测试黑名单写入
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body['ok'] is True
    assert body['chat_name'] == 'Yunshan Zan'
    assert body['chat_key'] == 'test:Yunshan Zan'
    assert body['reason'] == '运营测试'
    # state_store 应当真的写入了
    assert store.is_skipped_chat('test:Yunshan Zan') is True


def test_emergency_stop_writes_self_skip(client_with_svc, store):
    client, runner, svc = client_with_svc
    r = client.post(
        '/api/messenger-rpa/accounts/acc1/chats/emergency_stop',
        json={
            'chat_name': 'Victor Zan',
            'reason': 'OCR 死循环',
            'self_skip_sec': 1800,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body['self_skip_sec'] == 1800
    # self_skip_until_ts 在未来 ~30 分钟
    assert body['self_skip_until_ts'] > time.time() + 1500
    # P0-4 持久化表也要有记录
    active = store.load_active_self_skips()
    from src.integrations.messenger_rpa.runner import _self_skip_norm_key
    norm_key = _self_skip_norm_key('Victor Zan')
    assert norm_key in active


def test_emergency_stop_release_clears_blacklist(client_with_svc, store):
    client, runner, svc = client_with_svc
    # 先停发
    client.post(
        '/api/messenger-rpa/accounts/acc1/chats/emergency_stop',
        json={'chat_name': 'Bob', 'self_skip_sec': 600},
    )
    assert store.is_skipped_chat('test:Bob') is True
    # 释放（TestClient.delete 不接受 json=，用 request() 显式传）
    r = client.request(
        'DELETE',
        '/api/messenger-rpa/accounts/acc1/chats/emergency_stop',
        json={'chat_name': 'Bob'},
    )
    assert r.status_code == 200
    body = r.json()
    assert body['ok'] is True
    assert body['removed_blacklist'] is True
    assert body['cleared_self_skip'] is True
    # state_store 真的清了
    assert store.is_skipped_chat('test:Bob') is False
    from src.integrations.messenger_rpa.runner import _self_skip_norm_key
    active = store.load_active_self_skips()
    assert _self_skip_norm_key('Bob') not in active


def test_emergency_stop_release_via_query_param(client_with_svc, store):
    """DELETE 也支持 query string（curl -X DELETE 默认无 body）。"""
    client, runner, svc = client_with_svc
    client.post(
        '/api/messenger-rpa/accounts/acc1/chats/emergency_stop',
        json={'chat_name': 'Carol', 'self_skip_sec': 0},
    )
    r = client.delete(
        '/api/messenger-rpa/accounts/acc1/chats/emergency_stop?chat_name=Carol',
    )
    assert r.status_code == 200
    assert r.json()['removed_blacklist'] is True


def test_skipped_chats_list(client_with_svc):
    client, runner, svc = client_with_svc
    client.post(
        '/api/messenger-rpa/accounts/acc1/chats/emergency_stop',
        json={'chat_name': 'A', 'self_skip_sec': 0},
    )
    client.post(
        '/api/messenger-rpa/accounts/acc1/chats/emergency_stop',
        json={'chat_name': 'B', 'self_skip_sec': 0},
    )
    r = client.get('/api/messenger-rpa/accounts/acc1/chats/skipped')
    assert r.status_code == 200
    body = r.json()
    assert body['ok'] is True
    assert body['count'] >= 2
    names = {row['chat_name'] for row in body['chats']}
    assert {'A', 'B'} <= names

