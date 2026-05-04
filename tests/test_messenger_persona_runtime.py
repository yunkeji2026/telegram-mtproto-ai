from __future__ import annotations

from pathlib import Path

from src.integrations.messenger_rpa.persona_runtime import (
    AccountCandidate,
    AccountSelector,
    AutoRunPlanner,
    ConversationStateMachine,
    detect_customer_language,
    flatten_persona_facts,
)
from src.integrations.messenger_rpa.state_store import MessengerRpaStateStore


def test_account_selector_uses_language_health_load_and_continuity():
    selector = AccountSelector(min_health_score=40)
    candidates = [
        AccountCandidate(
            account_id="bad_health",
            supported_languages=("ja",),
            supported_customer_types=("lead",),
            health_score=20,
            current_load=0,
        ),
        AccountCandidate(
            account_id="busy_ja",
            supported_languages=("ja",),
            supported_customer_types=("lead",),
            health_score=95,
            current_load=12,
        ),
        AccountCandidate(
            account_id="stable_ja",
            supported_languages=("ja",),
            supported_customer_types=("lead",),
            health_score=90,
            current_load=1,
        ),
    ]

    picked = selector.select(
        candidates,
        customer_language="ja",
        customer_type="lead",
        previous_account_id="stable_ja",
    )

    assert picked is not None
    assert picked.account_id == "stable_ja"
    assert picked.reason["continuity"] == 10.0


def test_conversation_state_machine_tracks_stage_topics_and_used_facts():
    persona = {
        "background": {
            "occupation": "Tokyo software engineer",
            "hobbies": ["golf", "music"],
        }
    }
    facts = flatten_persona_facts(persona)
    fsm = ConversationStateMachine()

    state = fsm.advance(
        {},
        peer_text="こんにちは、料金と相談内容を知りたいです",
        customer_language=detect_customer_language("こんにちは"),
        customer_type="lead",
        persona_facts=facts,
        now=1000,
    )
    state = fsm.mark_used_facts(
        state,
        "Tokyo software engineer として話せるよ。",
        facts,
    )
    block = fsm.prompt_block(state)

    assert state["stage"] in {"education", "offer"}
    assert state["recent_topics"]
    assert any("software engineer" in f for f in state["used_persona_facts"])
    assert "不要重复" in block


def test_strategy_store_persists_accounts_personas_states_and_jobs(tmp_path: Path):
    store = MessengerRpaStateStore(tmp_path / "msgr.db", account_id="test")
    store.upsert_strategy_account(
        account_id="acc_ja",
        label="Japanese account",
        supported_languages=["ja"],
        supported_customer_types=["lead"],
        health_score=88,
        current_load=2,
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
        "chat:alice",
        chat_key="chat:alice",
        account_id="acc_ja",
        persona_id="sato",
        customer_language="ja",
        customer_type="lead",
        stage="qualification",
        memory_summary="asked about price",
        recent_topics=["price"],
        used_persona_facts=["occupation: engineer"],
        last_message_at=1234,
    )
    job_id = store.enqueue_auto_run_message(
        customer_id="chat:alice",
        chat_key="chat:alice",
        text="料金は？",
        language="ja",
        account_id="acc_ja",
        persona_id="sato",
        stage="qualification",
        strategy={"mode": "auto"},
        priority=80,
        run_after=100,
        message_id="m1",
    )

    accounts = store.list_strategy_accounts()
    personas = store.list_personas()
    state = store.get_conversation_state("chat:alice")
    leased = store.lease_auto_run_jobs(worker_id="w1", now_ts=101, limit=1)
    store.record_strategy_chat_run(
        customer_id="chat:alice",
        job_id=job_id,
        account_id="acc_ja",
        persona_id="sato",
        previous_stage="qualification",
        next_stage="education",
        strategy={"mode": "auto"},
        reply_text="短く説明します。",
        status="sent",
    )
    store.mark_auto_run_job_done(job_id)

    assert accounts[0]["account_id"] == "acc_ja"
    assert accounts[0]["supported_languages"] == ["ja"]
    assert personas[0]["persona_id"] == "sato"
    assert state["stage"] == "qualification"
    assert state["used_persona_facts"] == ["occupation: engineer"]
    assert leased[0]["job_id"] == job_id
    assert leased[0]["strategy"] == {"mode": "auto"}


def test_auto_run_planner_selects_account_persona_and_enqueues(tmp_path: Path):
    store = MessengerRpaStateStore(tmp_path / "planner.db", account_id="test")
    store.upsert_strategy_account(
        account_id="acc_en",
        supported_languages=["en"],
        supported_customer_types=["support"],
        persona_ids=["support_persona"],
        health_score=95,
        current_load=0,
    )
    store.upsert_strategy_account(
        account_id="acc_ja",
        supported_languages=["ja"],
        supported_customer_types=["lead"],
        persona_ids=["sato"],
        health_score=90,
        current_load=0,
    )
    store.upsert_persona(
        persona_id="sato",
        name="Sato",
        language="ja",
        customer_type="lead",
        facts=["occupation: engineer"],
        persona={"name": "Sato"},
    )

    plan = AutoRunPlanner(store).plan_and_enqueue(
        customer_id="chat:akiko",
        chat_key="chat:akiko",
        text="こんにちは、料金を知りたいです",
        message_id="m-akiko-1",
        run_after=10,
    )
    leased = store.lease_auto_run_jobs(worker_id="worker", now_ts=11, limit=1)
    state = store.get_conversation_state("chat:akiko")

    assert plan["account_id"] == "acc_ja"
    assert plan["persona_id"] == "sato"
    assert plan["language"] == "ja"
    assert leased[0]["account_id"] == "acc_ja"
    assert leased[0]["persona_id"] == "sato"
    assert state["stage"] in {"education", "offer"}
