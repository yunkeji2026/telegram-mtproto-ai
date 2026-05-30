"""W3-3G：draft_log CRUD + DraftSuccessEvaluator 单测。"""

from __future__ import annotations

import pytest

from src.contacts.store import ContactStore
from src.contacts.draft_eval import DraftSuccessEvaluator


@pytest.fixture
def store(tmp_path):
    s = ContactStore(db_path=tmp_path / "c.db")
    yield s
    s.close()


def _make_journey(store, jid="j1", contact_id="c1"):
    """造一个最小 journey（直接写库，避开 gateway 复杂度）。"""
    import time
    now = int(time.time())
    with store._lock:
        store._conn.execute(
            "INSERT INTO contacts (contact_id, primary_name, language_hint, "
            "timezone_hint, country_hint, created_at, last_active_at, notes) "
            "VALUES (?, '', '', '', '', ?, ?, '')",
            (contact_id, now, now),
        )
        store._conn.execute(
            "INSERT INTO journeys (journey_id, contact_id, persona_id, "
            "funnel_stage, intimacy_score, created_at, updated_at) "
            "VALUES (?, ?, '', 'BONDED', 22.0, ?, ?)",
            (jid, contact_id, now, now),
        )
        store._conn.commit()
    return jid


def _add_msg_in(store, jid, ts, event_id=None):
    import uuid
    eid = event_id or uuid.uuid4().hex
    with store._lock:
        store._conn.execute(
            "INSERT INTO journey_events (event_id, journey_id, trace_id, "
            "event_type, payload_json, ts) VALUES (?, ?, '', 'msg_in', '{}', ?)",
            (eid, jid, ts),
        )
        store._conn.commit()
    return eid


class TestDraftStoreCRUD:
    def test_record_and_fetch_latest_unsent(self, store):
        jid = _make_journey(store)
        did = store.record_draft(
            journey_id=jid, draft_text="hi there",
            draft_lang="zh", intimacy_score=22.0,
            silent_days=15, funnel_stage="BONDED",
        )
        assert did
        d = store.latest_unsent_draft_for(jid)
        assert d is not None
        assert d["draft_id"] == did
        assert d["draft_text"] == "hi there"
        assert d["sent_ts"] is None

    def test_latest_unsent_returns_most_recent(self, store):
        jid = _make_journey(store)
        d1 = store.record_draft(journey_id=jid, draft_text="v1")
        d2 = store.record_draft(journey_id=jid, draft_text="v2")  # 后写的赢
        latest = store.latest_unsent_draft_for(jid)
        assert latest["draft_id"] == d2

    def test_mark_sent_then_no_more_unsent(self, store):
        jid = _make_journey(store)
        did = store.record_draft(journey_id=jid, draft_text="hi")
        assert store.mark_draft_sent(did, sent_by="alice") is True
        # 第二次 mark 应失败（已发）
        assert store.mark_draft_sent(did, sent_by="bob") is False
        # 没有 unsent 了
        assert store.latest_unsent_draft_for(jid) is None

    def test_pending_eval_respects_window(self, store):
        import time
        jid = _make_journey(store)
        did = store.record_draft(journey_id=jid, draft_text="hi")
        store.mark_draft_sent(did, sent_by="op")
        now = int(time.time())
        # 默认窗口 24h；现在刚发，未到期 → 不在 pending 列表
        pending = store.list_drafts_pending_eval(window_secs=86400, now_ts=now)
        assert pending == []
        # 把 sent_ts 倒推 25h → 应进入 pending
        with store._lock:
            store._conn.execute(
                "UPDATE draft_log SET sent_ts=? WHERE draft_id=?",
                (now - 25 * 3600, did),
            )
            store._conn.commit()
        pending = store.list_drafts_pending_eval(window_secs=86400, now_ts=now)
        assert len(pending) == 1
        assert pending[0]["draft_id"] == did

    def test_eval_draft_success_is_idempotent(self, store):
        jid = _make_journey(store)
        did = store.record_draft(journey_id=jid, draft_text="hi")
        store.mark_draft_sent(did)
        assert store.eval_draft_success(did, success=True, reply_event_id="ev1") is True
        # 第二次评估应被守门拒绝
        assert store.eval_draft_success(did, success=False) is False


class TestDraftQualityStats:
    def test_empty(self, store):
        s = store.draft_quality_stats(days=7)
        assert s["generated"] == 0
        assert s["sent"] == 0
        assert s["evaluated"] == 0
        assert s["success_rate"] is None
        assert s["by_lang"] == {}
        assert s["by_variant"] == {}

    def test_only_generated(self, store):
        jid = _make_journey(store)
        store.record_draft(journey_id=jid, draft_text="hi", draft_lang="zh")
        s = store.draft_quality_stats(days=7)
        assert s["generated"] == 1
        assert s["sent"] == 0
        assert s["success_rate"] is None
        assert s["by_lang"] == {}  # by_lang 只统计 sent_rows

    def test_sent_not_evaluated(self, store):
        jid = _make_journey(store)
        did = store.record_draft(journey_id=jid, draft_text="hi", draft_lang="en")
        store.mark_draft_sent(did)
        s = store.draft_quality_stats(days=7)
        assert s["sent"] == 1
        assert s["evaluated"] == 0
        assert s["success_rate"] is None
        # by_lang 该有 en
        assert s["by_lang"]["en"]["sent"] == 1
        assert s["by_lang"]["en"]["success_rate"] is None

    def test_mixed_success_rate(self, store):
        jid = _make_journey(store)
        # 3 个 zh：2 成功 1 失败 → 0.667
        for i, ok in enumerate([True, True, False]):
            did = store.record_draft(journey_id=jid, draft_text=f"d{i}",
                                      draft_lang="zh")
            store.mark_draft_sent(did)
            store.eval_draft_success(did, success=ok)
        # 2 个 en：1 成功 1 未评估
        d_en1 = store.record_draft(journey_id=jid, draft_text="en1", draft_lang="en")
        store.mark_draft_sent(d_en1)
        store.eval_draft_success(d_en1, success=True)
        d_en2 = store.record_draft(journey_id=jid, draft_text="en2", draft_lang="en")
        store.mark_draft_sent(d_en2)
        # 不评估 d_en2
        s = store.draft_quality_stats(days=7)
        assert s["sent"] == 5
        assert s["evaluated"] == 4
        assert s["success"] == 3
        assert s["success_rate"] == 0.75
        assert s["by_lang"]["zh"]["sent"] == 3
        assert s["by_lang"]["zh"]["evaluated"] == 3
        assert s["by_lang"]["zh"]["success"] == 2
        assert abs(s["by_lang"]["zh"]["success_rate"] - 0.667) < 0.01
        assert s["by_lang"]["en"]["sent"] == 2
        assert s["by_lang"]["en"]["evaluated"] == 1
        assert s["by_lang"]["en"]["success_rate"] == 1.0


class TestWilsonAndSilentBand:
    """W3-3H.4：Wilson 下界 + silent_band 分桶。"""

    def test_wilson_lower_zero_total(self, store):
        assert store._wilson_lower(0, 0) is None

    def test_wilson_lower_perfect_3_of_3_below_100(self, store):
        # 3/3 = 100% 是不可信的，下界应明显 < 1.0
        lower = store._wilson_lower(3, 3)
        assert 0 < lower < 0.7  # 大约 0.44

    def test_wilson_lower_50_pct_with_low_sample(self, store):
        # 5/10=50%，下界应在 0.2-0.4
        lower = store._wilson_lower(5, 10)
        assert 0.2 < lower < 0.5

    def test_wilson_lower_50_pct_with_high_sample(self, store):
        # 500/1000=50%，下界更接近 0.5
        lower = store._wilson_lower(500, 1000)
        assert 0.45 < lower < 0.5

    def test_wilson_lower_never_negative(self, store):
        # 0/100 = 0%；下界应 ≥ 0
        lower = store._wilson_lower(0, 100)
        assert lower == 0.0

    @pytest.mark.parametrize("days,expected", [
        (0, "0-6d"), (3, "0-6d"),
        (7, "7-13d"), (10, "7-13d"),
        (14, "14-29d"), (29, "14-29d"),
        (30, "30-59d"), (59, "30-59d"),
        (60, "60d+"), (90, "60d+"), (365, "60d+"),
    ])
    def test_silent_band(self, store, days, expected):
        assert store._silent_band(days) == expected


class TestDraftQualityStatsExtended:
    """W3-3H.4：stats 必须返回 success_rate_lower + by_silent_band。"""

    def test_includes_wilson_lower(self, store):
        jid = _make_journey(store)
        # 3 个 draft：2 success 1 fail
        for ok in [True, True, False]:
            did = store.record_draft(journey_id=jid, draft_text="x")
            store.mark_draft_sent(did)
            store.eval_draft_success(did, success=ok)
        stats = store.draft_quality_stats(days=7)
        assert stats["success_rate"] == round(2/3, 3)
        assert stats["success_rate_lower"] is not None
        assert stats["success_rate_lower"] < stats["success_rate"]

    def test_by_silent_band_grouping(self, store):
        jid = _make_journey(store)
        # 各放一个不同 band
        for sd in [3, 10, 20, 45, 100]:
            did = store.record_draft(
                journey_id=jid, draft_text="x", silent_days=sd,
            )
            store.mark_draft_sent(did)
        stats = store.draft_quality_stats(days=7)
        bands = stats["by_silent_band"]
        assert "0-6d" in bands
        assert "7-13d" in bands
        assert "14-29d" in bands
        assert "30-59d" in bands
        assert "60d+" in bands
        assert all(b["sent"] == 1 for b in bands.values())


class TestDraftEvalScheduler:
    """W3-3K.1：DraftEvalScheduler 自适应间隔 + status 可观测性。"""

    # ── status 初始状态 ────────────────────────────────────────────────────────

    def test_initial_status_has_nulls(self, store):
        from src.contacts.draft_eval import DraftEvalScheduler
        s = DraftEvalScheduler(store)
        st = s.status()
        assert st["last_run_at"] is None
        assert st["last_result"] is None
        assert st["total_runs"] == 0
        assert st["is_running"] is False
        # "available" 是 API 层加的字段，core status() 不含
        assert "available" not in st

    def test_status_available_field_absent_from_core(self, store):
        """DraftEvalScheduler.status() 本身不含 available 字段（那是 API 层加的）。"""
        from src.contacts.draft_eval import DraftEvalScheduler
        s = DraftEvalScheduler(store)
        assert "available" not in s.status()

    # ── run_once + 状态更新 ────────────────────────────────────────────────────

    def test_run_once_updates_last_run(self, store):
        import time
        from src.contacts.draft_eval import DraftEvalScheduler
        s = DraftEvalScheduler(store)
        before = int(time.time())
        s.run_once()
        after = int(time.time())
        st = s.status()
        assert st["last_run_at"] is not None
        # status() stores last_run_at as int(timestamp)
        assert before <= st["last_run_at"] <= after + 1
        assert st["total_runs"] == 1

    def test_run_once_returns_result_dict(self, store):
        from src.contacts.draft_eval import DraftEvalScheduler
        s = DraftEvalScheduler(store)
        r = s.run_once()
        for key in ("evaluated", "success", "fail"):
            assert key in r

    def test_run_once_does_not_raise(self, store):
        """run_once 在 store 异常时也不应抛出。"""
        from src.contacts.draft_eval import DraftEvalScheduler
        class _BadStore:
            def list_drafts_pending_eval(self, **kw):
                raise RuntimeError("simulated DB error")
        s = DraftEvalScheduler(_BadStore())
        r = s.run_once()   # must not raise
        assert r["evaluated"] == 0

    # ── 自适应间隔 ─────────────────────────────────────────────────────────────

    def test_interval_backs_off_when_nothing_evaluated(self, store):
        """evaluated==0 → 间隔加倍（最多到 max_interval）。"""
        from src.contacts.draft_eval import DraftEvalScheduler
        s = DraftEvalScheduler(store, base_interval_secs=100, max_interval_secs=400)
        assert s.next_interval_secs == 100
        s.run_once()  # 无草稿 → evaluated=0 → backoff
        assert s.next_interval_secs == 200
        s.run_once()
        assert s.next_interval_secs == 400
        s.run_once()
        assert s.next_interval_secs == 400  # 已到 max，不再增长

    def test_interval_resets_when_evaluated(self, store):
        """有评估内容后间隔重置到 base。"""
        import time
        from src.contacts.draft_eval import DraftEvalScheduler
        # NOTE: DraftSuccessEvaluator 强制 max(60, eval_window_secs) 最小 60s。
        # 因此 sent_ts 必须 > 60s 前。注入 now_ts 避免真实时钟不确定性。
        fake_now = int(time.time())
        s = DraftEvalScheduler(store, base_interval_secs=100, max_interval_secs=400,
                               eval_window_secs=60)   # 60 = effective minimum

        # 先 backoff 到 400（注入 now_ts，无 pending eval）
        s.run_once(now_ts=fake_now); s.run_once(now_ts=fake_now)
        assert s.next_interval_secs == 400

        jid = _make_journey(store)
        did = store.record_draft(journey_id=jid, draft_text="x")
        # sent_ts = fake_now - 90：90s 前发出，超过 60s 评估窗口 → 满足 pending
        sent_ts = fake_now - 90
        with store._lock:
            store._conn.execute(
                "UPDATE draft_log SET sent_ts=? WHERE draft_id=?", (sent_ts, did),
            )
            store._conn.commit()
        # deadline_query = fake_now - 60；sent_ts = fake_now - 90 <= fake_now - 60 ✓
        s.run_once(now_ts=fake_now)
        assert s.next_interval_secs == 100

    def test_next_run_at_advances(self, store):
        from src.contacts.draft_eval import DraftEvalScheduler
        import time
        s = DraftEvalScheduler(store, base_interval_secs=60)
        s.run_once()
        st = s.status()
        assert st["next_run_at"] is not None
        assert st["next_run_in_secs"] >= 0
        assert st["next_run_at"] > time.time() - 1  # 约 last_run + 60

    # ── status next_run_in_secs decreases over time ───────────────────────────

    def test_status_eval_window_reflected(self, store):
        from src.contacts.draft_eval import DraftEvalScheduler
        s = DraftEvalScheduler(store, eval_window_secs=7200)
        assert s.status()["eval_window_secs"] == 7200


class TestWilsonHelpers:
    """W3-3I.1：_wilson_upper + pick_winning_variant 单测。"""

    # ── _wilson_upper ──────────────────────────────────────────────────────────

    def test_wilson_upper_zero_total_returns_none(self, store):
        assert store._wilson_upper(0, 0) is None

    def test_wilson_upper_all_success(self, store):
        u = store._wilson_upper(10, 10)
        assert u is not None
        assert 0.7 < u <= 1.0   # CI 上界应接近 1

    def test_wilson_upper_zero_success(self, store):
        u = store._wilson_upper(0, 10)
        assert u is not None
        assert u < 0.35          # CI 上界应远低于 0.5

    def test_wilson_upper_geq_lower(self, store):
        """上界 ≥ 下界（任意 s/n 组合）。"""
        for s, n in [(1, 1), (3, 10), (0, 5), (10, 10), (5, 20)]:
            lo = store._wilson_lower(s, n)
            hi = store._wilson_upper(s, n)
            if lo is not None and hi is not None:
                assert hi >= lo, f"upper < lower for s={s} n={n}"

    # ── pick_winning_variant ────────────────────────────────────────────────────

    def _make_bucket(self, success, evaluated):
        """构造 by_variant 所需的 bucket dict（和 draft_quality_stats 格式对齐）。"""
        rate = round(success / evaluated, 3) if evaluated else None
        lower = ContactStore._wilson_lower(success, evaluated)
        return {
            "success": success,
            "evaluated": evaluated,
            "success_rate": rate,
            "success_rate_lower": lower,
        }

    def test_insufficient_samples_returns_none(self, store):
        bv = {
            "v1": self._make_bucket(8, 9),   # 9 < min_evaluated=10
            "v2": self._make_bucket(2, 9),
        }
        assert ContactStore.pick_winning_variant(bv) is None

    def test_only_one_variant_returns_none(self, store):
        bv = {"v1": self._make_bucket(8, 15)}
        assert ContactStore.pick_winning_variant(bv) is None

    def test_overlapping_ci_returns_none(self, store):
        """两组相近成功率 → CI 重叠 → 无显著差异。"""
        bv = {
            "v1": self._make_bucket(8, 15),  # ~53%
            "v2": self._make_bucket(6, 15),  # ~40%
        }
        # 样本小、差距不大 → CI 一定重叠
        assert ContactStore.pick_winning_variant(bv) is None

    def test_clear_winner_returns_result(self, store):
        """v1 极高 v2 极低 + 足够样本 → 应宣布 v1 胜出。"""
        bv = {
            "v1": self._make_bucket(18, 20),  # 90%
            "v2": self._make_bucket(2, 20),   # 10%
        }
        result = ContactStore.pick_winning_variant(bv)
        assert result is not None
        assert result["winner"] == "v1"
        assert result["runner_up"] == "v2"
        assert result["gap_pct"] > 0
        assert result["winner_evaluated"] == 20
        assert result["runner_up_evaluated"] == 20

    def test_custom_min_evaluated(self, store):
        """min_evaluated 调低到 5 → 9 条数据也能判断。"""
        bv = {
            "v1": self._make_bucket(9, 9),   # 100%
            "v2": self._make_bucket(0, 9),   # 0%
        }
        result = ContactStore.pick_winning_variant(bv, min_evaluated=5)
        assert result is not None
        assert result["winner"] == "v1"

    def test_winner_result_fields_present(self, store):
        bv = {
            "v1": self._make_bucket(18, 20),
            "v2": self._make_bucket(2, 20),
        }
        r = ContactStore.pick_winning_variant(bv)
        for key in ("winner", "winner_rate", "winner_evaluated",
                    "runner_up", "runner_up_rate", "runner_up_evaluated",
                    "gap_pct"):
            assert key in r, f"missing key: {key}"

    def test_empty_by_variant_returns_none(self, store):
        assert ContactStore.pick_winning_variant({}) is None

    def test_missing_success_rate_lower_skipped(self, store):
        """bucket 没有 success_rate_lower 字段 → 跳过，不 KeyError。"""
        bv = {
            "v1": {"success": 18, "evaluated": 20, "success_rate": 0.9},
            "v2": {"success": 2, "evaluated": 20, "success_rate": 0.1},
        }
        # success_rate_lower 缺失 → eligible 过滤掉 → None
        assert ContactStore.pick_winning_variant(bv) is None


class TestDraftSuccessEvaluator:
    def test_no_pending_returns_zero(self, store):
        ev = DraftSuccessEvaluator(store)
        r = ev.evaluate_due()
        assert r == {"evaluated": 0, "success": 0, "fail": 0}

    def test_success_when_reply_in_window(self, store):
        import time
        jid = _make_journey(store)
        did = store.record_draft(journey_id=jid, draft_text="hi")
        now = int(time.time())
        # 把 draft sent_ts 设到 25h 前
        sent_ts = now - 25 * 3600
        with store._lock:
            store._conn.execute(
                "UPDATE draft_log SET sent_ts=? WHERE draft_id=?",
                (sent_ts, did),
            )
            store._conn.commit()
        # 在 sent_ts 后 12h 写一条 msg_in
        reply_eid = _add_msg_in(store, jid, sent_ts + 12 * 3600)
        ev = DraftSuccessEvaluator(store, eval_window_secs=86400)
        r = ev.evaluate_due(now_ts=now)
        assert r["evaluated"] == 1
        assert r["success"] == 1
        # 回查 draft_log
        with store._lock:
            row = dict(store._conn.execute(
                "SELECT * FROM draft_log WHERE draft_id=?", (did,),
            ).fetchone())
        assert row["success"] == 1
        assert row["reply_event_id"] == reply_eid

    def test_fail_when_no_reply(self, store):
        import time
        jid = _make_journey(store)
        did = store.record_draft(journey_id=jid, draft_text="hi")
        now = int(time.time())
        sent_ts = now - 25 * 3600
        with store._lock:
            store._conn.execute(
                "UPDATE draft_log SET sent_ts=? WHERE draft_id=?",
                (sent_ts, did),
            )
            store._conn.commit()
        # 故意不写 msg_in
        ev = DraftSuccessEvaluator(store, eval_window_secs=86400)
        r = ev.evaluate_due(now_ts=now)
        assert r["evaluated"] == 1
        assert r["fail"] == 1
        assert r["success"] == 0

    def test_msg_in_before_sent_not_counted(self, store):
        """msg_in 早于 sent_ts → 不算作回复（防误判）。"""
        import time
        jid = _make_journey(store)
        did = store.record_draft(journey_id=jid, draft_text="hi")
        now = int(time.time())
        sent_ts = now - 25 * 3600
        with store._lock:
            store._conn.execute(
                "UPDATE draft_log SET sent_ts=? WHERE draft_id=?",
                (sent_ts, did),
            )
            store._conn.commit()
        # msg_in 早于 sent_ts 1 小时 → 不应触发 success
        _add_msg_in(store, jid, sent_ts - 3600)
        ev = DraftSuccessEvaluator(store, eval_window_secs=86400)
        r = ev.evaluate_due(now_ts=now)
        assert r["success"] == 0
        assert r["fail"] == 1

    def test_msg_in_after_window_not_counted(self, store):
        """msg_in 在 24h 窗口外 → 不算作 success。"""
        import time
        jid = _make_journey(store)
        did = store.record_draft(journey_id=jid, draft_text="hi")
        now = int(time.time())
        sent_ts = now - 49 * 3600  # 49 小时前发，窗口 24h 早就过
        with store._lock:
            store._conn.execute(
                "UPDATE draft_log SET sent_ts=? WHERE draft_id=?",
                (sent_ts, did),
            )
            store._conn.commit()
        # msg_in 在 sent_ts 后 30h（已超过 24h 窗口）
        _add_msg_in(store, jid, sent_ts + 30 * 3600)
        ev = DraftSuccessEvaluator(store, eval_window_secs=86400)
        r = ev.evaluate_due(now_ts=now)
        assert r["success"] == 0
        assert r["fail"] == 1

    def test_idempotent_does_not_double_count(self, store):
        import time
        jid = _make_journey(store)
        did = store.record_draft(journey_id=jid, draft_text="hi")
        now = int(time.time())
        with store._lock:
            store._conn.execute(
                "UPDATE draft_log SET sent_ts=? WHERE draft_id=?",
                (now - 25 * 3600, did),
            )
            store._conn.commit()
        _add_msg_in(store, jid, now - 12 * 3600)
        ev = DraftSuccessEvaluator(store)
        r1 = ev.evaluate_due(now_ts=now)
        assert r1["evaluated"] == 1
        # 第二次跑不应重复评估
        r2 = ev.evaluate_due(now_ts=now)
        assert r2["evaluated"] == 0
