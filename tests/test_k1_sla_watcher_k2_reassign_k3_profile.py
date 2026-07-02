"""K1 SLA 预警推送 + K2 自动再分配 + K3 客户画像 测试。

K1 SLAWatcher:
  - 超时 L3/L4 草稿 → 发布 draft_sla_breach 事件
  - 未超时草稿不触发
  - L0/L1/L2 草稿不触发
  - 同一草稿只触发一次（边沿触发）
  - 恢复后（draft 被处置）从告警集移除

K2 自动再分配:
  - 坐席断线 + 名下有 L3+ claim → 自动再分配给在线主管
  - 无在线主管时不再分配
  - 已再分配草稿不重复分配
  - list_claims_by_agent 正确过滤

K3 客户画像 API:
  - 空 conversation_id → 400
  - 无 inbox_store → ok: True / empty fields
  - 有 conv_meta → 正确返回
  - recent_decisions 按 conversation_id 过滤
  - 路由在 inventory 中

SLAWatcher:
  - status_snapshot 包含所有关键字段
  - stop() 使 run() 退出
"""

import asyncio
import time
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.inbox.store import InboxStore
from src.inbox.drafts import DraftService
from src.inbox.sla_watcher import SLAWatcher
from src.inbox.template_seeds import SEED_TEMPLATES
from src.web.routes.unified_inbox_routes import register_unified_inbox_routes


# ─────────────────────────────────────
# helpers
# ─────────────────────────────────────

def _make_store() -> InboxStore:
    s = InboxStore(":memory:")
    s.seed_templates(SEED_TEMPLATES)
    return s


def _make_svc(store: InboxStore) -> DraftService:
    return DraftService(
        inbox_store=store, line_services=[], wa_services=[], messenger_service=None
    )


def _insert_pending_draft(
    store: InboxStore,
    draft_id: str,
    conv_id: str,
    level: str = "L3",
    risk: str = "medium",
    created_ago_sec: float = 0,
):
    ts = time.time() - created_ago_sec
    with store._lock:
        store._conn.execute(
            "INSERT OR REPLACE INTO reply_drafts "
            "(draft_id,conversation_id,platform,account_id,chat_key,"
            "source_kind,source_id,autopilot_level,risk_level,draft_text,"
            "peer_text,status,risk_reasons_json,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (draft_id, conv_id, "line", "acc1", "u1",
             "inbox", conv_id, level, risk, "test draft", "test msg",
             "pending", "[]", ts, ts),
        )
        store._conn.commit()


# ─────────────────────────────────────
# K1: SLAWatcher breach detection
# ─────────────────────────────────────

class TestK1SLABreachDetection:
    def test_l3_overdue_publishes_event(self):
        """L3 pending 草稿超过 SLA → 发布 draft_sla_breach 事件"""
        store = _make_store()
        svc = _make_svc(store)
        watcher = SLAWatcher(
            draft_service=svc, inbox_store=store, config={"sla_hours": 0.001}
        )
        _insert_pending_draft(store, "d-1", "conv-1", "L3", created_ago_sec=60)

        published = []
        with patch("src.integrations.shared.event_bus.get_event_bus") as mock_bus:
            mock_bus.return_value.publish = lambda etype, data: published.append((etype, data))
            watcher._check_sla_breach()

        assert any(e[0] == "draft_sla_breach" for e in published)
        breach_events = [e[1] for e in published if e[0] == "draft_sla_breach"]
        assert breach_events[0]["draft_id"] == "d-1"

    def test_l4_overdue_publishes_event(self):
        store = _make_store()
        svc = _make_svc(store)
        watcher = SLAWatcher(
            draft_service=svc, inbox_store=store, config={"sla_hours": 0.001}
        )
        _insert_pending_draft(store, "d-2", "conv-2", "L4", created_ago_sec=60)

        published = []
        with patch("src.integrations.shared.event_bus.get_event_bus") as mock_bus:
            mock_bus.return_value.publish = lambda etype, data: published.append((etype, data))
            watcher._check_sla_breach()

        assert any(e[0] == "draft_sla_breach" for e in published)

    def test_l2_overdue_does_not_trigger(self):
        """L2 草稿超时不触发 SLA 预警（L2 自动发送，无需人工干预）"""
        store = _make_store()
        svc = _make_svc(store)
        watcher = SLAWatcher(
            draft_service=svc, inbox_store=store, config={"sla_hours": 0.001}
        )
        _insert_pending_draft(store, "d-3", "conv-3", "L2", created_ago_sec=600)

        published = []
        with patch("src.integrations.shared.event_bus.get_event_bus") as mock_bus:
            mock_bus.return_value.publish = lambda etype, data: published.append((etype, data))
            watcher._check_sla_breach()

        assert not any(e[0] == "draft_sla_breach" for e in published)

    def test_not_yet_overdue_does_not_trigger(self):
        """未超 SLA 阈值的草稿不触发"""
        store = _make_store()
        svc = _make_svc(store)
        watcher = SLAWatcher(
            draft_service=svc, inbox_store=store,
            config={"sla_hours": 24}  # 24h 阈值，草稿只有 1 分钟
        )
        _insert_pending_draft(store, "d-4", "conv-4", "L3", created_ago_sec=60)

        published = []
        with patch("src.integrations.shared.event_bus.get_event_bus") as mock_bus:
            mock_bus.return_value.publish = lambda etype, data: published.append((etype, data))
            watcher._check_sla_breach()

        assert not any(e[0] == "draft_sla_breach" for e in published)

    def test_deduplication_same_draft_only_once(self):
        """同一草稿只触发一次告警（边沿触发去重）"""
        store = _make_store()
        svc = _make_svc(store)
        watcher = SLAWatcher(
            draft_service=svc, inbox_store=store, config={"sla_hours": 0.001}
        )
        _insert_pending_draft(store, "d-5", "conv-5", "L3", created_ago_sec=600)

        published = []
        with patch("src.integrations.shared.event_bus.get_event_bus") as mock_bus:
            mock_bus.return_value.publish = lambda etype, data: published.append((etype, data))
            watcher._check_sla_breach()
            watcher._check_sla_breach()  # 第二次不应再发

        breach = [e for e in published if e[0] == "draft_sla_breach"]
        assert len(breach) == 1  # 只发一次

    def test_resolved_draft_removed_from_alerted_set(self):
        """草稿被处置后从已告警集移除，下次可重新告警（模拟超时解除后重入）"""
        store = _make_store()
        svc = _make_svc(store)
        watcher = SLAWatcher(
            draft_service=svc, inbox_store=store, config={"sla_hours": 0.001}
        )
        _insert_pending_draft(store, "d-6", "conv-6", "L3", created_ago_sec=600)

        published = []
        with patch("src.integrations.shared.event_bus.get_event_bus") as mock_bus:
            mock_bus.return_value.publish = lambda etype, data: published.append((etype, data))
            watcher._check_sla_breach()
            assert "d-6" in watcher._alerted_draft_ids

            # 模拟草稿被处置（状态改为 approved）
            with store._lock:
                store._conn.execute(
                    "UPDATE reply_drafts SET status='approved' WHERE draft_id='d-6'"
                )
                store._conn.commit()

            # 再次 check → d-6 已从 pending 消失，_alerted_draft_ids 应更新
            watcher._check_sla_breach()
            assert "d-6" not in watcher._alerted_draft_ids

    def test_total_breach_events_counter(self):
        store = _make_store()
        svc = _make_svc(store)
        watcher = SLAWatcher(
            draft_service=svc, inbox_store=store, config={"sla_hours": 0.001}
        )
        _insert_pending_draft(store, "d-7", "conv-7", "L3", created_ago_sec=600)
        _insert_pending_draft(store, "d-8", "conv-8", "L4", created_ago_sec=600)

        with patch("src.integrations.shared.event_bus.get_event_bus") as mock_bus:
            mock_bus.return_value.publish = MagicMock()
            watcher._check_sla_breach()

        assert watcher.total_breach_events == 2


# ─────────────────────────────────────
# K1+: 再告警指数退避 + 陈旧封顶 + pending 自动作废
# ─────────────────────────────────────

class TestK1RealertBackoff:
    def test_no_realert_within_backoff_window(self):
        """同一草稿在退避窗口内多次 tick 不重复告警（对旧版每小时全清风暴的修复）"""
        store = _make_store()
        svc = _make_svc(store)
        watcher = SLAWatcher(
            draft_service=svc, inbox_store=store,
            config={"sla_hours": 0.001, "realert_base_sec": 3600},
        )
        _insert_pending_draft(store, "d-bk1", "conv-bk1", "L3", created_ago_sec=600)

        published = []
        with patch("src.integrations.shared.event_bus.get_event_bus") as mock_bus:
            mock_bus.return_value.publish = lambda etype, data: published.append((etype, data))
            watcher._check_sla_breach()
            watcher._check_sla_breach()
            watcher._check_sla_breach()

        assert len([e for e in published if e[0] == "draft_sla_breach"]) == 1

    def test_realert_after_backoff_elapsed(self):
        """退避到点后允许再次告警，且 alert_count 递增"""
        store = _make_store()
        svc = _make_svc(store)
        watcher = SLAWatcher(
            draft_service=svc, inbox_store=store,
            config={"sla_hours": 0.001, "realert_base_sec": 3600},
        )
        _insert_pending_draft(store, "d-bk2", "conv-bk2", "L3", created_ago_sec=600)

        published = []
        with patch("src.integrations.shared.event_bus.get_event_bus") as mock_bus:
            mock_bus.return_value.publish = lambda etype, data: published.append((etype, data))
            watcher._check_sla_breach()
            # 模拟退避窗口已过
            watcher._alert_state["d-bk2"]["next_ts"] = time.time() - 1
            watcher._check_sla_breach()

        breaches = [e[1] for e in published if e[0] == "draft_sla_breach"]
        assert len(breaches) == 2
        assert breaches[0]["alert_count"] == 1
        assert breaches[1]["alert_count"] == 2

    def test_backoff_interval_grows_and_caps(self):
        """指数退避：下次告警间隔随次数翻倍，并被 realert_max_sec 封顶"""
        store = _make_store()
        svc = _make_svc(store)
        watcher = SLAWatcher(
            draft_service=svc, inbox_store=store,
            config={"sla_hours": 0.001, "realert_base_sec": 100,
                    "realert_backoff": 2.0, "realert_max_sec": 250},
        )
        _insert_pending_draft(store, "d-bk3", "conv-bk3", "L4", created_ago_sec=600)

        gaps = []
        with patch("src.integrations.shared.event_bus.get_event_bus") as mock_bus:
            mock_bus.return_value.publish = MagicMock()
            for _ in range(4):
                now = time.time()
                watcher._check_sla_breach()
                gaps.append(round(watcher._alert_state["d-bk3"]["next_ts"] - now))
                watcher._alert_state["d-bk3"]["next_ts"] = time.time() - 1

        # 100 → 200 → 250(capped) → 250(capped)
        assert gaps[0] == 100
        assert gaps[1] == 200
        assert gaps[2] == 250
        assert gaps[3] == 250

    def test_stale_hours_quiesces_after_first_alert(self):
        """stale_hours>0：陈旧草稿首告警后静默，后续即便到点也不再告警"""
        store = _make_store()
        svc = _make_svc(store)
        watcher = SLAWatcher(
            draft_service=svc, inbox_store=store,
            config={"sla_hours": 0.001, "realert_base_sec": 100, "stale_hours": 1},
        )
        # 草稿 2 小时前建 → 超过 stale_hours=1h
        _insert_pending_draft(store, "d-stale", "conv-stale", "L3", created_ago_sec=7200)

        published = []
        with patch("src.integrations.shared.event_bus.get_event_bus") as mock_bus:
            mock_bus.return_value.publish = lambda etype, data: published.append((etype, data))
            watcher._check_sla_breach()
            assert watcher._alert_state["d-stale"]["quiesced"] is True
            # 即便强制到点也不再发
            watcher._alert_state["d-stale"]["next_ts"] = time.time() - 1
            watcher._check_sla_breach()

        assert len([e for e in published if e[0] == "draft_sla_breach"]) == 1
        assert watcher.quiesced_count == 1

    def test_snapshot_has_new_fields(self):
        store = _make_store()
        svc = _make_svc(store)
        watcher = SLAWatcher(draft_service=svc, inbox_store=store)
        snap = watcher.status_snapshot()
        for k in ("realert_base_sec", "realert_backoff", "realert_max_sec",
                  "stale_hours", "auto_expire_hours", "total_expired", "quiesced_count",
                  "backlog_summary", "total_summary_events"):
            assert k in snap


class TestK1BacklogSummary:
    def _run(self, watcher):
        published = []
        with patch("src.integrations.shared.event_bus.get_event_bus") as mock_bus:
            mock_bus.return_value.publish = lambda etype, data: published.append((etype, data))
            watcher._check_sla_breach()
        return published

    def test_off_by_default_no_summary(self):
        """默认关：不发 draft_backlog_summary（旧行为不变）"""
        store = _make_store()
        svc = _make_svc(store)
        watcher = SLAWatcher(draft_service=svc, inbox_store=store, config={"sla_hours": 0.001})
        _insert_pending_draft(store, "d-bs0", "conv-bs0", "L3", created_ago_sec=600)
        published = self._run(watcher)
        assert not any(e[0] == "draft_backlog_summary" for e in published)

    def test_emits_aggregate_count_and_levels(self):
        """开启后：发一条汇总，count/l3/l4 与实际越线数一致"""
        store = _make_store()
        svc = _make_svc(store)
        watcher = SLAWatcher(
            draft_service=svc, inbox_store=store,
            config={"sla_hours": 0.001, "backlog_summary": True},
        )
        _insert_pending_draft(store, "d-bs1", "conv-bs1", "L3", created_ago_sec=600)
        _insert_pending_draft(store, "d-bs2", "conv-bs2", "L4", created_ago_sec=600)
        _insert_pending_draft(store, "d-bs3", "conv-bs3", "L4", created_ago_sec=600)
        published = self._run(watcher)
        summaries = [e[1] for e in published if e[0] == "draft_backlog_summary"]
        assert len(summaries) == 1
        assert summaries[0]["count"] == 3
        assert summaries[0]["l3"] == 1
        assert summaries[0]["l4"] == 2
        assert watcher.total_summary_events == 1

    def test_not_reemitted_when_count_unchanged_within_interval(self):
        """数量不变且未到重发间隔 → 不重复发（避免每 tick 刷屏）"""
        store = _make_store()
        svc = _make_svc(store)
        watcher = SLAWatcher(
            draft_service=svc, inbox_store=store,
            config={"sla_hours": 0.001, "backlog_summary": True,
                    "backlog_summary_interval_sec": 3600},
        )
        _insert_pending_draft(store, "d-bs4", "conv-bs4", "L3", created_ago_sec=600)
        published = []
        with patch("src.integrations.shared.event_bus.get_event_bus") as mock_bus:
            mock_bus.return_value.publish = lambda etype, data: published.append((etype, data))
            watcher._check_sla_breach()
            watcher._check_sla_breach()
            watcher._check_sla_breach()
        assert len([e for e in published if e[0] == "draft_backlog_summary"]) == 1

    def test_reemits_when_count_changes(self):
        """数量变化 → 立即重发一条新汇总"""
        store = _make_store()
        svc = _make_svc(store)
        watcher = SLAWatcher(
            draft_service=svc, inbox_store=store,
            config={"sla_hours": 0.001, "backlog_summary": True,
                    "backlog_summary_interval_sec": 3600},
        )
        _insert_pending_draft(store, "d-bs5", "conv-bs5", "L3", created_ago_sec=600)
        published = []
        with patch("src.integrations.shared.event_bus.get_event_bus") as mock_bus:
            mock_bus.return_value.publish = lambda etype, data: published.append((etype, data))
            watcher._check_sla_breach()
            _insert_pending_draft(store, "d-bs6", "conv-bs6", "L4", created_ago_sec=600)
            watcher._check_sla_breach()
        summaries = [e[1] for e in published if e[0] == "draft_backlog_summary"]
        assert len(summaries) == 2
        assert summaries[0]["count"] == 1
        assert summaries[1]["count"] == 2

    def test_min_threshold_suppresses_small_backlog(self):
        """backlog_summary_min：越线数低于阈值不发汇总"""
        store = _make_store()
        svc = _make_svc(store)
        watcher = SLAWatcher(
            draft_service=svc, inbox_store=store,
            config={"sla_hours": 0.001, "backlog_summary": True,
                    "backlog_summary_min": 3},
        )
        _insert_pending_draft(store, "d-bs7", "conv-bs7", "L3", created_ago_sec=600)
        published = self._run(watcher)
        assert not any(e[0] == "draft_backlog_summary" for e in published)


class TestExpireStalePendingDrafts:
    def test_dry_run_lists_without_mutating(self):
        store = _make_store()
        _insert_pending_draft(store, "d-ex1", "conv-ex1", "L3", created_ago_sec=8 * 86400)
        victims = store.expire_stale_pending_drafts(max_age_hours=168, dry_run=True)
        assert [v["draft_id"] for v in victims] == ["d-ex1"]
        # 未变更
        rows = store.list_drafts(status="pending")
        assert any(r["draft_id"] == "d-ex1" for r in rows)

    def test_expires_old_pending_and_audits(self):
        store = _make_store()
        _insert_pending_draft(store, "d-ex2", "conv-ex2", "L4", created_ago_sec=10 * 86400)
        victims = store.expire_stale_pending_drafts(max_age_hours=168)
        assert [v["draft_id"] for v in victims] == ["d-ex2"]
        # 已转 cancelled，不再 pending
        assert not any(r["draft_id"] == "d-ex2" for r in store.list_drafts(status="pending"))
        assert any(r["draft_id"] == "d-ex2" for r in store.list_drafts(status="cancelled"))
        # 审计留痕
        audit = store.list_draft_audit(draft_id="d-ex2")
        assert any(a["action"] == "auto_expired" for a in audit)

    def test_fresh_pending_untouched(self):
        store = _make_store()
        _insert_pending_draft(store, "d-ex3", "conv-ex3", "L3", created_ago_sec=3600)
        victims = store.expire_stale_pending_drafts(max_age_hours=168)
        assert victims == []
        assert any(r["draft_id"] == "d-ex3" for r in store.list_drafts(status="pending"))

    def test_level_filter(self):
        store = _make_store()
        _insert_pending_draft(store, "d-ex-l1", "conv-l1", "L1", created_ago_sec=10 * 86400)
        _insert_pending_draft(store, "d-ex-l3", "conv-l3", "L3", created_ago_sec=10 * 86400)
        victims = store.expire_stale_pending_drafts(max_age_hours=168, levels=["L3", "L4"])
        ids = [v["draft_id"] for v in victims]
        assert "d-ex-l3" in ids
        assert "d-ex-l1" not in ids

    def test_groups_only_spares_private_chats(self):
        """groups_only=True 只作废群/频道会话，1:1 私聊待审草稿不误伤。"""
        store = _make_store()
        # 群会话（Telegram 负 peer id）
        with store._lock:
            store._conn.execute(
                "INSERT OR REPLACE INTO reply_drafts "
                "(draft_id,conversation_id,platform,account_id,chat_key,source_kind,"
                "source_id,autopilot_level,risk_level,draft_text,peer_text,status,"
                "risk_reasons_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("d-grp", "telegram:acc:-1001560025690", "telegram", "acc",
                 "-1001560025690", "inbox", "telegram:acc:-1001560025690", "L4",
                 "high", "x", "y", "pending", "[]",
                 time.time() - 10 * 86400, time.time() - 10 * 86400),
            )
            # 私聊会话（正 peer id）
            store._conn.execute(
                "INSERT OR REPLACE INTO reply_drafts "
                "(draft_id,conversation_id,platform,account_id,chat_key,source_kind,"
                "source_id,autopilot_level,risk_level,draft_text,peer_text,status,"
                "risk_reasons_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("d-dm", "telegram:acc:8142915241", "telegram", "acc",
                 "8142915241", "inbox", "telegram:acc:8142915241", "L4",
                 "high", "x", "y", "pending", "[]",
                 time.time() - 10 * 86400, time.time() - 10 * 86400),
            )
            store._conn.commit()
        victims = store.expire_stale_pending_drafts(
            max_age_hours=168, levels=["L3", "L4"], groups_only=True)
        ids = [v["draft_id"] for v in victims]
        assert "d-grp" in ids
        assert "d-dm" not in ids
        # 私聊仍 pending
        assert any(r["draft_id"] == "d-dm" for r in store.list_drafts(status="pending"))

    def test_watcher_auto_expire_wires_store(self):
        """SLAWatcher 开 auto_expire_hours 后，_expire_stale 调 store 并清告警态"""
        store = _make_store()
        svc = _make_svc(store)
        _insert_pending_draft(store, "d-ex4", "conv-ex4", "L4", created_ago_sec=10 * 86400)
        watcher = SLAWatcher(
            draft_service=svc, inbox_store=store,
            config={"auto_expire_hours": 168, "auto_expire_levels": ["L3", "L4"]},
        )
        watcher._expire_stale()
        assert watcher.total_expired == 1
        assert not any(r["draft_id"] == "d-ex4" for r in store.list_drafts(status="pending"))


# ─────────────────────────────────────
# K2: 自动再分配
# ─────────────────────────────────────

class TestK2AutoReassign:
    def test_list_claims_by_agent(self):
        """list_claims_by_agent 只返回该坐席的 claims"""
        store = _make_store()
        now = time.time()
        store.set_conversation_claim("conv-a", "agent-1", agent_name="Alice", ttl_sec=3600)
        store.set_conversation_claim("conv-b", "agent-2", agent_name="Bob", ttl_sec=3600)
        claims = store.list_claims_by_agent("agent-1")
        assert len(claims) == 1
        assert claims[0]["conversation_id"] == "conv-a"

    def test_list_claims_by_agent_empty(self):
        store = _make_store()
        claims = store.list_claims_by_agent("nonexistent")
        assert claims == []

    def test_offline_agent_with_claim_gets_reassigned(self):
        """断线坐席名下有 L3+ claimed conv → 再分配给在线主管"""
        store = _make_store()
        svc = _make_svc(store)

        # 坐席 agent-1 断线（last_seen_at 很早）
        old_ts = time.time() - 600
        with store._lock:
            store._conn.execute(
                "INSERT OR REPLACE INTO agent_presence "
                "(agent_id, display_name, status, last_seen_at, updated_at) VALUES (?,?,?,?,?)",
                ("agent-1", "Alice", "online", old_ts, old_ts),
            )
            store._conn.commit()

        # 在线主管 sup-1
        store.upsert_agent_presence("sup-1", display_name="Supervisor", status="online")

        # 断线坐席持有 conv claim
        store.set_conversation_claim("conv-c", "agent-1", agent_name="Alice", ttl_sec=7200, force=True)

        # 待处置 L3 草稿
        _insert_pending_draft(store, "d-ra", "conv-c", "L3", created_ago_sec=100)

        watcher = SLAWatcher(
            draft_service=svc, inbox_store=store,
            config={"absent_sec": 300},
        )

        published = []
        with patch("src.integrations.shared.event_bus.get_event_bus") as mock_bus:
            mock_bus.return_value.publish = lambda etype, data: published.append((etype, data))
            watcher._check_reassign()

        assert any(e[0] == "draft_reassigned" for e in published)
        reassigned = [e[1] for e in published if e[0] == "draft_reassigned"]
        assert reassigned[0]["from_agent"] == "agent-1"
        assert reassigned[0]["to_agent"] == "sup-1"

        # claim 应已更新
        claim = store.get_conversation_claim("conv-c")
        assert claim is not None
        assert claim["agent_id"] == "sup-1"

    def test_no_online_supervisor_skips_reassign(self):
        """无在线主管 → 不再分配"""
        store = _make_store()
        svc = _make_svc(store)

        old_ts = time.time() - 600
        with store._lock:
            store._conn.execute(
                "INSERT OR REPLACE INTO agent_presence "
                "(agent_id, display_name, status, last_seen_at, updated_at) VALUES (?,?,?,?,?)",
                ("agent-x", "Offline", "online", old_ts, old_ts),
            )
            store._conn.commit()

        store.set_conversation_claim("conv-d", "agent-x", ttl_sec=7200, force=True)
        _insert_pending_draft(store, "d-rb", "conv-d", "L3", created_ago_sec=100)

        watcher = SLAWatcher(draft_service=svc, inbox_store=store, config={"absent_sec": 300})

        published = []
        with patch("src.integrations.shared.event_bus.get_event_bus") as mock_bus:
            mock_bus.return_value.publish = lambda etype, data: published.append((etype, data))
            watcher._check_reassign()

        assert not any(e[0] == "draft_reassigned" for e in published)

    def test_already_reassigned_draft_not_repeated(self):
        """已再分配的草稿不重复再分配"""
        store = _make_store()
        svc = _make_svc(store)

        old_ts = time.time() - 600
        with store._lock:
            store._conn.execute(
                "INSERT OR REPLACE INTO agent_presence "
                "(agent_id, display_name, status, last_seen_at, updated_at) VALUES (?,?,?,?,?)",
                ("agent-2", "Bob", "online", old_ts, old_ts),
            )
            store._conn.commit()

        store.upsert_agent_presence("sup-2", display_name="Sup2", status="online")
        store.set_conversation_claim("conv-e", "agent-2", ttl_sec=7200, force=True)
        _insert_pending_draft(store, "d-rc", "conv-e", "L3", created_ago_sec=100)

        watcher = SLAWatcher(draft_service=svc, inbox_store=store, config={"absent_sec": 300})

        published = []
        with patch("src.integrations.shared.event_bus.get_event_bus") as mock_bus:
            mock_bus.return_value.publish = lambda etype, data: published.append((etype, data))
            watcher._check_reassign()  # 第一次
            watcher._check_reassign()  # 第二次

        reassigned = [e for e in published if e[0] == "draft_reassigned"]
        assert len(reassigned) == 1  # 只分配一次


# ─────────────────────────────────────
# K1: SLAWatcher 生命周期 + 状态快照
# ─────────────────────────────────────

class TestSLAWatcherLifecycle:
    def test_status_snapshot_fields(self):
        store = _make_store()
        svc = _make_svc(store)
        watcher = SLAWatcher(draft_service=svc, inbox_store=store)
        snap = watcher.status_snapshot()
        assert "running" in snap
        assert "sla_hours" in snap
        assert "total_breach_events" in snap
        assert "total_reassigned" in snap
        assert "alerted_count" in snap
        assert "last_tick_ts" in snap

    def test_stop_signals_event(self):
        store = _make_store()
        svc = _make_svc(store)
        watcher = SLAWatcher(draft_service=svc, inbox_store=store, config={"tick_sec": 0.1})
        watcher.stop()
        assert watcher._stop_evt.is_set()


# ─────────────────────────────────────
# K3: Contact Profile API
# ─────────────────────────────────────

def _make_profile_app(store: InboxStore = None, role: str = "agent"):
    from src.web.routes.unified_inbox_routes import register_unified_inbox_routes
    app = FastAPI()

    @app.middleware("http")
    async def _inject(request: Request, call_next):
        request.scope["session"] = {"role": role, "user_id": "u1"}
        return await call_next(request)

    from fastapi import Request as _Req
    def api_auth(r: _Req): return True
    def page_auth(r: _Req): return True

    # Minimal config manager stub
    cfg = MagicMock()
    cfg.config = {}
    cfg.get = lambda k, d=None: d

    register_unified_inbox_routes(
        app,
        config_manager=cfg,
        api_auth=api_auth,
        page_auth=page_auth,
        templates=MagicMock(),
    )
    if store:
        app.state.inbox_store = store
    return TestClient(app, raise_server_exceptions=True)


class TestK3ContactProfileAPI:
    def test_empty_conversation_id_400(self):
        """conversation_id 为空字符串 → 400（FastAPI 默认传 "" 时我们返回 400）"""
        client = _make_profile_app()
        # FastAPI 传递空字符串参数到 endpoint，endpoint 返回 400
        r = client.get("/api/unified-inbox/contact-profile", params={"conversation_id": ""})
        assert r.status_code == 400

    def test_no_store_returns_ok_empty(self):
        client = _make_profile_app(store=None)
        r = client.get("/api/unified-inbox/contact-profile?conversation_id=line:acc:u1")
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is True
        assert d["conv_meta"] is None
        assert d["recent_decisions"] == []

    def test_returns_conv_meta(self):
        store = _make_store()
        store.update_conv_meta("line:acc:u1", platform="line", intent="退款", emotion="愤怒", risk="medium")
        client = _make_profile_app(store)
        r = client.get("/api/unified-inbox/contact-profile?conversation_id=line:acc:u1")
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is True
        meta = d.get("conv_meta")
        assert meta is not None
        assert meta["last_intent"] == "退款"

    def test_recent_decisions_filtered_by_conv_id(self):
        store = _make_store()
        # 写两条不同 conv 的审计记录
        store.record_draft_audit(
            "d-profile-1", autopilot_level="L3", action="approved",
            agent_id="agent1", risk_level="medium", conversation_id="line:acc:target"
        )
        store.record_draft_audit(
            "d-profile-2", autopilot_level="L3", action="rejected",
            agent_id="agent2", risk_level="high", conversation_id="line:acc:other"
        )
        client = _make_profile_app(store)
        r = client.get("/api/unified-inbox/contact-profile?conversation_id=line:acc:target")
        assert r.status_code == 200
        d = r.json()
        recs = d.get("recent_decisions", [])
        assert all(rec.get("conversation_id") != "line:acc:other" for rec in recs)
        ids = [rec["draft_id"] for rec in recs]
        assert "d-profile-1" in ids
        assert "d-profile-2" not in ids

    def test_recent_decisions_max_5(self):
        store = _make_store()
        for i in range(8):
            store.record_draft_audit(
                f"d-many-{i}", autopilot_level="L3", action="approved",
                agent_id="a1", risk_level="low", conversation_id="conv-many"
            )
        client = _make_profile_app(store)
        r = client.get("/api/unified-inbox/contact-profile?conversation_id=conv-many")
        assert r.status_code == 200
        d = r.json()
        assert len(d.get("recent_decisions", [])) <= 5

    def test_inventory_includes_contact_profile(self):
        with open("tests/test_admin_route_inventory.py", encoding="utf-8") as f:
            content = f.read()
        assert "/api/unified-inbox/contact-profile\tGET" in content
