"""W4-定时：ContactsSubsystem 后台 decay 循环的行为验证。

使用 asyncio.run 绕过 pytest-asyncio 依赖（该项目仅装了 anyio）。
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.contacts import bootstrap_contacts_subsystem
from src.contacts.models import (
    CHANNEL_MESSENGER, STAGE_HANDOFF_SENT, STAGE_LOST_HANDOFF,
)

CFG_DIR = Path(__file__).resolve().parent.parent / "config"


def _cfg(db_path: Path, **over):
    c = {
        "contacts": {
            "enabled": True,
            "db_path": str(db_path),
            "daily_cap": 3,
            "token_ttl_hours": 24,
            "scripts_path": str(CFG_DIR / "handoff_scripts.yaml"),
            "compliance_path": str(CFG_DIR / "handoff_compliance.yaml"),
        }
    }
    c["contacts"].update(over)
    return c


class TestStartStopIdempotent:
    def test_start_twice_does_not_double_task(self, tmp_path):
        async def scenario():
            sub = bootstrap_contacts_subsystem(
                _cfg(tmp_path / "c.db", decay_interval_minutes=5), CFG_DIR)
            try:
                sub.start_background_tasks()
                n_first = len(sub._bg_tasks)
                sub.start_background_tasks()    # 第二次是 no-op
                assert len(sub._bg_tasks) == n_first  # 幂等：数量不变
                # close 会自动 stop；允许任务被 cancel
                tasks_snapshot = list(sub._bg_tasks)
            finally:
                sub.close()
            for t in tasks_snapshot:
                # 取消后给循环一个机会完成取消
                try:
                    await t
                except asyncio.CancelledError:
                    pass
                assert t.cancelled() or t.done()
        asyncio.run(scenario())

    def test_disabled_when_interval_zero(self, tmp_path):
        async def scenario():
            sub = bootstrap_contacts_subsystem(
                _cfg(tmp_path / "c.db",
                     decay_interval_minutes=0,
                     kpi_alert_interval_minutes=0), CFG_DIR)
            try:
                sub.start_background_tasks()
                assert sub._bg_tasks == [], "两个 interval=0 时不应启动任何后台任务"
            finally:
                sub.close()
        asyncio.run(scenario())

    def test_intimacy_refresh_starts_with_interval_and_engine(self, tmp_path):
        """interval>0 且 intimacy_engine 就绪时启动 intimacy-refresh 后台任务。"""
        async def scenario():
            sub = bootstrap_contacts_subsystem(
                _cfg(tmp_path / "c.db",
                     decay_interval_minutes=0,
                     kpi_alert_interval_minutes=0,
                     intimacy_refresh_interval_minutes=5), CFG_DIR)
            try:
                assert sub.intimacy_engine is not None
                sub.start_background_tasks()
                names = {t.get_name() for t in sub._bg_tasks}
                assert "contacts-intimacy-refresh" in names
            finally:
                sub.close()
        asyncio.run(scenario())

    def test_health_exposes_intimacy_refresh_block(self, tmp_path):
        """health() 暴露 intimacy_refresh 可观测块：enabled / 运行快照 / 积压 gauge。"""
        sub = bootstrap_contacts_subsystem(
            _cfg(tmp_path / "c.db",
                 intimacy_refresh_interval_minutes=360,
                 intimacy_refresh_stale_hours=24), CFG_DIR)
        try:
            h = sub.health()["intimacy_refresh"]
            assert h["enabled"] is True            # interval>0 且引擎就绪
            assert h["interval_minutes"] == 360
            assert h["runs"] == 0 and h["last_count"] == 0
            assert h["stale_backlog"] == 0         # 空库无积压
        finally:
            sub.close()

    def test_intimacy_refresh_loop_updates_health_stats(self, tmp_path):
        """跑一轮 intimacy_refresh loop 后，health 快照应反映 runs/last_run_ts。"""
        async def scenario():
            sub = bootstrap_contacts_subsystem(
                _cfg(tmp_path / "c.db",
                     intimacy_refresh_interval_minutes=360), CFG_DIR)
            try:
                task = asyncio.create_task(
                    sub._intimacy_refresh_loop(interval_sec=1))
                await asyncio.sleep(1.4)   # 前置 sleep min(90,1)=1，跑到一轮
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                st = sub.health()["intimacy_refresh"]
                assert st["runs"] >= 1
                assert st["last_run_ts"] > 0
            finally:
                sub.close()
        asyncio.run(scenario())

    def test_health_intimacy_refresh_disabled_when_interval_zero(self, tmp_path):
        sub = bootstrap_contacts_subsystem(_cfg(tmp_path / "c.db"), CFG_DIR)
        try:
            h = sub.health()["intimacy_refresh"]
            assert h["enabled"] is False
            assert h["interval_minutes"] == 0
        finally:
            sub.close()

    def test_start_without_running_loop_logs_and_skips(self, tmp_path):
        """没有 running loop 时（同步语境）start 应静默跳过，不抛。"""
        sub = bootstrap_contacts_subsystem(
            _cfg(tmp_path / "c.db", decay_interval_minutes=5), CFG_DIR)
        try:
            sub.start_background_tasks()
            # 同步上下文下 asyncio.get_running_loop 会 raise → 被 bootstrap 吞掉
            assert sub._bg_tasks == []
        finally:
            sub.close()


class TestDecayActuallyRuns:
    def test_apply_silence_decay_demotes_stale_handoff(self, tmp_path):
        """直接调 apply_silence_decay：证明后台 loop 里的那一步会有效果。"""
        sub = bootstrap_contacts_subsystem(
            _cfg(tmp_path / "c.db", decay_interval_minutes=1), CFG_DIR)
        try:
            ctx = sub.gateway.on_peer_seen(
                channel=CHANNEL_MESSENGER, account_id="acc-A",
                external_id="fb_stale")
            past = int(time.time()) - 73 * 3600
            with sub.store._lock:
                sub.store._conn.execute(
                    "UPDATE journeys SET funnel_stage=?, updated_at=? "
                    "WHERE journey_id=?",
                    (STAGE_HANDOFF_SENT, past, ctx.journey.journey_id))
                sub.store._conn.commit()

            from src.contacts.journey_fsm import apply_silence_decay
            cnt = apply_silence_decay(sub.store)
            assert cnt >= 1
            j = sub.store.get_journey(ctx.journey.journey_id)
            assert j.funnel_stage == STAGE_LOST_HANDOFF
        finally:
            sub.close()

    def test_decay_loop_keeps_running_after_exception(self, tmp_path, monkeypatch):
        """apply_silence_decay 抛异常：loop 吞掉并继续跑下一轮，不炸。"""
        async def scenario():
            sub = bootstrap_contacts_subsystem(
                _cfg(tmp_path / "c.db", decay_interval_minutes=5), CFG_DIR)
            try:
                calls = {"n": 0}

                def boom(_store):
                    calls["n"] += 1
                    raise RuntimeError("simulated")

                monkeypatch.setattr(
                    "src.contacts.bootstrap.apply_silence_decay", boom)

                # 开一个极短间隔的 loop：前置 sleep 走 min(60, 0.05)=0.05
                task = asyncio.create_task(
                    sub._decay_loop(interval_sec=1))
                # 原 loop 会 await asyncio.sleep(min(60, interval_sec))=1s；
                # 这里让它跑到 apply + 下一轮 sleep 一次就足
                await asyncio.sleep(1.6)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

                # 至少跑一次（第一次 sleep 60 被 clamp 为 1）
                assert calls["n"] >= 1
            finally:
                sub.close()

        asyncio.run(scenario())
