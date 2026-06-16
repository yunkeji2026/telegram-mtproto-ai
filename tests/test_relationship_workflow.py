"""P43/P44/P46 — 关系阶段 + 工作链执行器 单元测试。"""
import json
import time
from unittest.mock import MagicMock

import pytest

from src.inbox.store import InboxStore
from src.inbox.workflow_monitor import enrich_execution
from src.inbox.relationship_stage import (
    compute_relationship_stage,
    downgrade_stage_one_level,
    enrich_with_manual_state,
)
from src.inbox.workflow_runner import MAX_STEPS_PER_TICK, WorkflowRunner


class TestRelationshipStage:
    def test_initial_stage_low_signals(self):
        r = compute_relationship_stage(exchange_count=1, intimacy_score=10)
        assert r["stage"] == "initial"
        assert r["progress_pct"] >= 0
        assert len(r["stages"]) == 4

    def test_advanced_detection(self):
        r = compute_relationship_stage(
            exchange_count=15, intimacy_score=60, previous_stage="warming",
        )
        assert r["advanced"] is True
        assert r["previous_stage"] == "warming"

    def test_no_advanced_same_stage(self):
        r = compute_relationship_stage(
            exchange_count=5, intimacy_score=30, previous_stage="warming",
        )
        assert r["advanced"] is False

    def test_advancement_ready_near_threshold(self):
        r = compute_relationship_stage(exchange_count=13, intimacy_score=54)
        assert r["stage"] in ("warming", "intimate")

    def test_stages_viz_active_one(self):
        r = compute_relationship_stage(exchange_count=20, intimacy_score=70)
        active = [s for s in r["stages"] if s["active"]]
        assert len(active) == 1


class TestRelationshipStageManual:
    def test_enrich_pending_advancement(self):
        computed = compute_relationship_stage(exchange_count=20, intimacy_score=70)
        r = enrich_with_manual_state(
            computed, confirmed_stage="warming", pending_stage="intimate",
        )
        assert r["needs_confirmation"] is True
        assert r["pending_stage"] == "intimate"
        assert r["display_stage"] == "warming"
        assert r["advanced"] is False

    def test_enrich_no_pending_when_aligned(self):
        computed = compute_relationship_stage(exchange_count=8, intimacy_score=40)
        r = enrich_with_manual_state(
            computed, confirmed_stage=computed["stage"],
        )
        assert r["pending_advancement"] is False
        assert r["needs_confirmation"] is False

    def test_downgrade_one_level(self):
        assert downgrade_stage_one_level("intimate") == "warming"
        assert downgrade_stage_one_level("initial") == "initial"

    def test_reunion_hidden_after_ack(self):
        computed = compute_relationship_stage(exchange_count=20, intimacy_score=30)
        r = enrich_with_manual_state(
            computed, confirmed_stage="warming", reunion_ack_ts=__import__("time").time(),
        )
        assert r["reunion_acknowledged"] is True
        assert r["reunion"] is False


class TestRelStageStore:
    def test_rel_stage_confirm_pending_cycle(self, tmp_path):
        store = InboxStore(tmp_path / "rel.db")
        cid = "conv_rel_1"
        store.confirm_rel_stage(cid, "warming")
        assert store.get_rel_stage_meta(cid)["confirmed"] == "warming"
        store.set_rel_stage_pending(cid, "intimate", ts=100.0)
        meta = store.get_rel_stage_meta(cid)
        assert meta["pending"] == "intimate"
        assert meta["pending_ts"] == 100.0
        store.confirm_rel_stage(cid, "intimate")
        meta2 = store.get_rel_stage_meta(cid)
        assert meta2["confirmed"] == "intimate"
        assert meta2["pending"] == ""
        store.ack_rel_reunion(cid, ts=200.0)
        assert store.get_rel_stage_meta(cid)["reunion_ack_ts"] == 200.0


class TestWorkflowMonitor:
    def test_enrich_execution_fields(self):
        row = {
            "exec_id": "e1", "chain_id": "c1", "chain_name": "测试链",
            "conversation_id": "conv1", "status": "running",
            "current_step": 1, "started_at": 100, "updated_at": 200,
            "next_step_at": 500, "steps_json": json.dumps([
                {"action_type": "template", "note": "你好"},
                {"action_type": "note", "note": "备注", "delay_hours": 1},
            ]),
            "last_result_json": json.dumps({"text": "你好", "ok": True}),
        }
        r = enrich_execution(row, now=400)
        assert r["total_steps"] == 2
        assert r["current_step_display"] == 2
        assert r["countdown_sec"] == 100
        assert len(r["steps_preview"]) == 2


class TestChainExecutionStore:
    def test_list_and_cancel_execution(self, tmp_path):
        store = InboxStore(tmp_path / "chain.db")
        store.upsert_workflow_chain({
            "chain_id": "c1", "name": "链1",
            "steps": [{"action_type": "template", "note": "hi"}],
        })
        now = time.time()
        store._conn.execute(
            """INSERT INTO conversations
               (conversation_id, platform, display_name, last_ts, created_at, updated_at)
               VALUES (?,?,?,?,?,?)""",
            ("conv1", "line", "客户A", now, now, now),
        )
        store._conn.commit()
        eid = store.start_chain_execution("c1", "conv1", {}, schedule_first_step=True)
        rows = store.list_chain_executions(status="running")
        assert len(rows) == 1
        assert rows[0]["display_name"] == "客户A"
        assert store.cancel_workflow_execution(eid)
        ex = store.get_workflow_execution(eid)
        assert ex["status"] == "cancelled"


class TestWorkflowRunner:
    def _make_store(self):
        store = MagicMock()
        store.list_due_workflow_executions.return_value = []
        store.list_workflow_chains.return_value = []
        return store

    def test_process_due_empty(self):
        runner = WorkflowRunner(self._make_store())
        assert runner.process_due_executions() == 0

    def test_execute_template_publishes_event(self, monkeypatch):
        published = []
        store = MagicMock()
        ex = {
            "exec_id": "e1", "chain_id": "c1", "conversation_id": "conv1",
            "current_step": 0, "chain_name": "测试链", "status": "running",
            "context_json": "{}",
        }
        store.get_workflow_chain.return_value = {
            "chain_id": "c1",
            "steps_json": json.dumps([
                {"action_type": "template", "note": "你好，最近怎么样？"},
            ]),
        }
        store.list_due_workflow_executions.return_value = [ex]
        store.get_workflow_execution.return_value = ex

        class FakeBus:
            def publish(self, t, d):
                published.append((t, d))

        monkeypatch.setattr(
            "src.integrations.shared.event_bus.get_event_bus",
            lambda: FakeBus(),
        )
        runner = WorkflowRunner(store)
        n = runner.process_due_executions()
        assert n == 1
        store.complete_workflow_execution.assert_called_once()
        assert published[0][0] == "workflow_step"

    def test_auto_start_skips_without_conditions(self):
        store = self._make_store()
        store.list_workflow_chains.return_value = [
            {"chain_id": "c1", "enabled": 1, "trigger_conditions": "{}"},
        ]
        runner = WorkflowRunner(store)
        assert runner.auto_start_chains() == 0

    def test_step_retry_on_failure(self, monkeypatch):
        store = MagicMock()
        ex = {
            "exec_id": "e1", "chain_id": "c1", "conversation_id": "conv1",
            "current_step": 0, "chain_name": "链", "context_json": "{}",
            "status": "running",
        }
        store.get_workflow_chain.return_value = {
            "chain_id": "c1",
            "steps_json": json.dumps([{"action_type": "task", "note": "跟进"}]),
        }
        store.list_due_workflow_executions.return_value = [ex]
        store.get_workflow_execution.return_value = ex
        store.get_conv_meta.return_value = {}
        runner = WorkflowRunner(store, contacts_store=None)
        n = runner.process_due_executions()
        assert n == 1
        store.update_workflow_execution.assert_called()
        call_kw = store.update_workflow_execution.call_args[1]
        assert call_kw["next_step_at"] > 0

    def test_max_steps_budget(self, monkeypatch):
        store = MagicMock()
        steps = [{"action_type": "template", "note": f"s{i}"} for i in range(5)]
        ex = {
            "exec_id": "e1", "chain_id": "c1", "conversation_id": "conv1",
            "current_step": 0, "chain_name": "链", "context_json": "{}",
            "status": "running",
        }
        store.get_workflow_chain.return_value = {
            "chain_id": "c1", "steps_json": json.dumps(steps),
        }
        store.list_due_workflow_executions.return_value = [ex]

        state = {"step": 0}

        def _get_ex(eid):
            ex2 = dict(ex)
            ex2["current_step"] = state["step"]
            return ex2

        store.get_workflow_execution.side_effect = lambda eid: _get_ex(eid)

        def _update(eid, **kw):
            if "current_step" in kw:
                state["step"] = kw["current_step"]

        store.update_workflow_execution.side_effect = _update

        class FakeBus:
            def publish(self, t, d):
                pass

        monkeypatch.setattr(
            "src.integrations.shared.event_bus.get_event_bus",
            lambda: FakeBus(),
        )
        runner = WorkflowRunner(store)
        runner.process_due_executions(max_steps=2)
        assert state["step"] <= 2
