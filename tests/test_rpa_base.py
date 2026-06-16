"""P3：rpa_base 跨平台基类回归测试

覆盖：
- DailyCapTracker 基本行为 + 跨日 reset + 线程安全
- PendingItem.from_dict 兼容 LINE / WhatsApp / Messenger 三家字段命名
- AlertItem.from_dict 兼容多种字段
- RpaStatusSummary.from_status_dict 聚合 4 平台 status() 输出
- Protocol 运行时检查（duck typing）
- RpaPlatform.api_prefix / display_name 正确性
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional

import pytest

from src.integrations.rpa_base import (
    AlertItem,
    AlertSeverity,
    DailyCapTracker,
    PendingItem,
    PendingStatus,
    RpaPlatform,
    RpaService,
    RpaServiceWithPending,
    RpaServiceWithAlerts,
    RpaStatusSummary,
)


# ════════════════════════════════════════════════════════════════════════
# DailyCapTracker
# ════════════════════════════════════════════════════════════════════════


class TestDailyCapTracker:
    def test_no_cap_never_exceeds(self) -> None:
        cap = DailyCapTracker(daily_cap=0)
        assert cap.would_exceed(1) is False
        assert cap.would_exceed(99999) is False
        assert cap.remaining() == -1  # -1 表示不限
        cap.record_sent(50)
        assert cap.daily_sent == 50
        assert cap.would_exceed(1) is False  # 仍不限

    def test_cap_with_record_sent(self) -> None:
        cap = DailyCapTracker(daily_cap=10)
        assert cap.remaining() == 10
        cap.record_sent(3)
        assert cap.daily_sent == 3
        assert cap.remaining() == 7
        assert cap.would_exceed(7) is False  # 刚好到上限
        assert cap.would_exceed(8) is True

    def test_record_sent_zero_or_negative(self) -> None:
        cap = DailyCapTracker(daily_cap=10)
        cap.record_sent(0)
        cap.record_sent(-1)
        assert cap.daily_sent == 0

    def test_set_cap_runtime(self) -> None:
        cap = DailyCapTracker(daily_cap=10)
        cap.record_sent(5)
        cap.set_cap(20)
        assert cap.remaining() == 15
        cap.set_cap(3)  # 缩小到比已发还少
        # remaining 不会变成负数
        assert cap.remaining() == 0
        assert cap.would_exceed(1) is True

    def test_reset_manual(self) -> None:
        cap = DailyCapTracker(daily_cap=10)
        cap.record_sent(7)
        cap.reset()
        assert cap.daily_sent == 0
        assert cap.remaining() == 10

    def test_initial_sent_recovery(self) -> None:
        # 模拟从 DB 恢复。initial_day 必须与 tracker 的 _today_key 同时区，
        # 否则在本地时区 != tracker tz 的环境（如 CI 用 UTC）会被误判跨日而 reset。
        # 这里把两边都钉到 UTC：tz_offset_hours=0 + time.gmtime()。
        cap = DailyCapTracker(
            daily_cap=10,
            tz_offset_hours=0,
            initial_sent=4,
            initial_day=time.strftime("%Y-%m-%d", time.gmtime()),
        )
        assert cap.daily_sent == 4
        assert cap.remaining() == 6

    def test_cross_day_reset(self) -> None:
        # 模拟昨天遗留的计数
        cap = DailyCapTracker(
            daily_cap=10,
            initial_sent=8,
            initial_day="2020-01-01",  # 远古日期
        )
        # 第一次访问应触发 reset
        assert cap.daily_sent == 0
        assert cap.remaining() == 10

    def test_thread_safety(self) -> None:
        """100 线程并发 record_sent，最终计数应精确。"""
        cap = DailyCapTracker(daily_cap=0)  # 不限
        N_THREADS = 50
        N_PER_THREAD = 100

        def worker() -> None:
            for _ in range(N_PER_THREAD):
                cap.record_sent(1)

        threads = [threading.Thread(target=worker) for _ in range(N_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert cap.daily_sent == N_THREADS * N_PER_THREAD

    def test_snapshot(self) -> None:
        cap = DailyCapTracker(daily_cap=100)
        cap.record_sent(15)
        snap = cap.snapshot()
        assert snap.daily_cap == 100
        assert snap.daily_sent == 15
        assert snap.remaining == 85
        assert snap.reset_at_ts > time.time()  # 未来时间

    def test_snapshot_no_cap_remaining_minus_one(self) -> None:
        cap = DailyCapTracker(daily_cap=0)
        cap.record_sent(5)
        snap = cap.snapshot()
        assert snap.remaining == -1


# ════════════════════════════════════════════════════════════════════════
# PendingItem.from_dict —— 跨平台兼容
# ════════════════════════════════════════════════════════════════════════


class TestPendingItemFromDict:
    def test_line_format(self) -> None:
        """LINE 用 ts / proposed_reply / chat_key"""
        d = {
            "id": 12,
            "status": "pending",
            "ts": 1715000000.0,
            "chat_key": "line:abc",
            "peer_text": "你好",
            "proposed_reply": "您好，有什么可以帮您？",
            "peer_name": "Alice",
        }
        item = PendingItem.from_dict(d)
        assert item.id == 12
        assert item.status == PendingStatus.PENDING
        assert item.ts == 1715000000.0
        assert item.proposed_reply == "您好，有什么可以帮您？"
        assert item.peer_name == "Alice"

    def test_whatsapp_format(self) -> None:
        """WhatsApp 字段命名跟 LINE 几乎一样"""
        d = {
            "id": 5,
            "status": "approved",
            "ts": 1715000100.0,
            "chat_key": "wa:+8612345",
            "peer_text": "How much?",
            "proposed_reply": "$99",
        }
        item = PendingItem.from_dict(d)
        assert item.status == PendingStatus.APPROVED
        assert item.proposed_reply == "$99"

    def test_messenger_format(self) -> None:
        """Messenger 用 created_at / reply_text / chat_name"""
        d = {
            "id": 7,
            "status": "deferred",
            "created_at": 1715000200.0,
            "chat_key": "msgr:thread/9876",
            "peer_text": "在吗",
            "reply_text": "在的，请稍等",
            "chat_name": "Bob",
            "ai_tier": "premium",
        }
        item = PendingItem.from_dict(d)
        assert item.status == PendingStatus.DEFERRED
        assert item.ts == 1715000200.0  # created_at → ts
        assert item.proposed_reply == "在的，请稍等"  # reply_text → proposed_reply
        assert item.peer_name == "Bob"  # chat_name → peer_name
        assert item.extra.get("ai_tier") == "premium"

    def test_unknown_status_falls_back_to_pending(self) -> None:
        d = {"id": 1, "status": "weird_xyz", "ts": 0.0, "chat_key": "x"}
        item = PendingItem.from_dict(d)
        assert item.status == PendingStatus.PENDING

    def test_to_dict_roundtrip(self) -> None:
        original = {
            "id": 1, "status": "pending", "ts": 1.0, "chat_key": "x",
            "peer_text": "hi", "proposed_reply": "hello",
            "peer_name": "P", "reply_lang": "zh",
        }
        item = PendingItem.from_dict(original)
        out = item.to_dict()
        assert out["id"] == 1
        assert out["proposed_reply"] == "hello"
        assert out["status"] == "pending"


# ════════════════════════════════════════════════════════════════════════
# AlertItem.from_dict
# ════════════════════════════════════════════════════════════════════════


class TestAlertItemFromDict:
    def test_with_severity(self) -> None:
        d = {
            "id": 1, "severity": "error", "ts": 1.0,
            "code": "ime_lost", "title": "IME 丢失",
            "acked": False,
        }
        a = AlertItem.from_dict(d)
        assert a.severity == AlertSeverity.ERROR
        assert a.code == "ime_lost"
        assert a.acked is False

    def test_legacy_level_field(self) -> None:
        """有些旧表用 level 而不是 severity"""
        d = {"id": 1, "level": "warning", "ts": 1.0}
        a = AlertItem.from_dict(d)
        assert a.severity == AlertSeverity.WARNING

    def test_unknown_severity_falls_back_to_warning(self) -> None:
        d = {"id": 1, "severity": "blue", "ts": 1.0}
        a = AlertItem.from_dict(d)
        assert a.severity == AlertSeverity.WARNING

    def test_acked_at(self) -> None:
        d = {"id": 1, "severity": "info", "ts": 1.0,
             "acked": True, "acked_at": 999.5, "acked_by": "ops"}
        a = AlertItem.from_dict(d)
        assert a.acked is True
        assert a.acked_at == 999.5
        assert a.acked_by == "ops"


# ════════════════════════════════════════════════════════════════════════
# RpaStatusSummary.from_status_dict —— 4 平台兼容
# ════════════════════════════════════════════════════════════════════════


class TestRpaStatusSummary:
    def test_line_status(self) -> None:
        """LINE: stats_24h.avg_send_ms / pending_stats.pending / alerts_unacked

        注意 LINE service 实际用 `alerts_unacked`（不是 `unacked_alerts`），
        与 WhatsApp 命名相反 —— from_status_dict 必须同时兼容两种。
        """
        line_status = {
            "available": True, "enabled": True, "running": True, "paused": False,
            "reply_mode": "auto",
            "stats_24h": {"sent": 50, "total": 60, "avg_send_ms": 1200},
            "pending_stats": {"pending": 3, "sent": 100},
            "alerts_unacked": 1,  # LINE 真实字段名
            "daily_cap": 200, "daily_sent": 50,
            "last_tick_ts": 1715000000.0,
        }
        s = RpaStatusSummary.from_status_dict(RpaPlatform.LINE, line_status)
        assert s.platform == RpaPlatform.LINE
        assert s.sent_24h == 50
        assert s.total_24h == 60
        assert s.avg_ms_24h == 1200
        assert s.pending_count == 3
        assert s.unacked_alerts == 1  # 从 alerts_unacked 别名拿到
        assert s.success_rate == round(50/60*100, 1)
        assert s.health_status == "warn"  # unacked_alerts > 0

    def test_unacked_alerts_naming_compat(self) -> None:
        """两种命名都能识别（防止未来回归）。"""
        # WhatsApp 风格
        s1 = RpaStatusSummary.from_status_dict(
            RpaPlatform.WHATSAPP,
            {"available": True, "enabled": True, "running": True,
             "unacked_alerts": 4},
        )
        # LINE 风格
        s2 = RpaStatusSummary.from_status_dict(
            RpaPlatform.LINE,
            {"available": True, "enabled": True, "running": True,
             "alerts_unacked": 7},
        )
        assert s1.unacked_alerts == 4
        assert s2.unacked_alerts == 7

    def test_whatsapp_status(self) -> None:
        """WhatsApp: stats_24h.avg_ms / pending_count 直接字段"""
        wa = {
            "available": True, "enabled": True, "running": True, "paused": False,
            "reply_mode": "approve",
            "stats_24h": {"sent": 30, "total": 30, "avg_ms": 800},
            "pending_count": 2,
            "unacked_alerts": 0,
            "daily_sent": 30,
        }
        s = RpaStatusSummary.from_status_dict(RpaPlatform.WHATSAPP, wa)
        assert s.platform == RpaPlatform.WHATSAPP
        assert s.avg_ms_24h == 800
        assert s.pending_count == 2
        assert s.success_rate == 100.0
        assert s.health_status == "ok"  # 无告警

    def test_messenger_status(self) -> None:
        """Messenger: send_stats.sent_24h + approval_sla.pending_count"""
        m = {
            "available": True, "enabled": True, "running": True, "paused": False,
            "send_stats": {"sent_24h": 20, "total_24h": 25, "avg_ms": 1500},
            "approval_sla": {"pending_count": 5, "overdue_count": 1},
            "unacked_alerts": 0,
        }
        s = RpaStatusSummary.from_status_dict(RpaPlatform.MESSENGER, m)
        assert s.sent_24h == 20
        assert s.total_24h == 25
        assert s.pending_count == 5

    def test_paused_health(self) -> None:
        d = {"available": True, "enabled": True, "running": True, "paused": True}
        s = RpaStatusSummary.from_status_dict(RpaPlatform.LINE, d)
        assert s.health_status == "paused"

    def test_offline_health(self) -> None:
        d = {"available": True, "enabled": False, "running": False}
        s = RpaStatusSummary.from_status_dict(RpaPlatform.LINE, d)
        assert s.health_status == "offline"

    def test_err_health(self) -> None:
        """available + enabled 但 running=False → err"""
        d = {"available": True, "enabled": True, "running": False}
        s = RpaStatusSummary.from_status_dict(RpaPlatform.LINE, d)
        assert s.health_status == "err"

    def test_zero_total_success_rate(self) -> None:
        d = {"stats_24h": {"sent": 0, "total": 0, "avg_ms": 0}}
        s = RpaStatusSummary.from_status_dict(RpaPlatform.LINE, d)
        assert s.success_rate == 0.0

    def test_to_dict_includes_all_fields(self) -> None:
        d = {
            "available": True, "enabled": True, "running": True,
            "stats_24h": {"sent": 10, "total": 12, "avg_ms": 500},
            "unacked_alerts": 0,
        }
        s = RpaStatusSummary.from_status_dict(RpaPlatform.WHATSAPP, d)
        out = s.to_dict()
        assert out["platform"] == "whatsapp"
        assert out["platform_name"] == "WhatsApp"
        assert out["api_prefix"] == "/api/whatsapp-rpa"
        assert out["health_status"] == "ok"
        assert out["success_rate"] == round(10/12*100, 1)


# ════════════════════════════════════════════════════════════════════════
# RpaPlatform Enum
# ════════════════════════════════════════════════════════════════════════


class TestRpaPlatform:
    def test_api_prefix(self) -> None:
        assert RpaPlatform.LINE.api_prefix == "/api/line-rpa"
        assert RpaPlatform.WHATSAPP.api_prefix == "/api/whatsapp-rpa"
        assert RpaPlatform.MESSENGER.api_prefix == "/api/messenger-rpa"
        assert RpaPlatform.TELEGRAM.api_prefix == "/api/telegram"

    def test_display_name(self) -> None:
        assert RpaPlatform.LINE.display_name == "LINE"
        assert RpaPlatform.WHATSAPP.display_name == "WhatsApp"

    def test_str_value(self) -> None:
        assert RpaPlatform.LINE.value == "line"
        # Enum 继承 str，可直接当字符串用
        assert RpaPlatform.LINE == "line"


# ════════════════════════════════════════════════════════════════════════
# Protocol 运行时检查
# ════════════════════════════════════════════════════════════════════════


class _FakeRpaService:
    """模拟一个最小满足 RpaService 的实现（duck typing）。"""
    def status(self) -> Dict[str, Any]:
        return {"available": True, "enabled": True, "running": True}

    def effective_config(self) -> Dict[str, Any]:
        return {}

    def pause_for(self, seconds: float) -> None:
        pass

    def resume(self) -> None:
        pass


class _FakeServiceWithPending(_FakeRpaService):
    def list_pending(self, *, status: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        return []
    def resolve_pending(
        self, pending_id: int, action: str, *, text: Optional[str] = None, by: str = ""
    ) -> Optional[Dict[str, Any]]:
        return None
    def pending_stats(self) -> Dict[str, int]:
        return {}


class _IncompleteService:
    """缺 resume() 方法。"""
    def status(self) -> Dict[str, Any]:
        return {}
    def effective_config(self) -> Dict[str, Any]:
        return {}
    def pause_for(self, seconds: float) -> None:
        pass


class TestProtocolDuckTyping:
    def test_fake_service_satisfies_protocol(self) -> None:
        svc = _FakeRpaService()
        assert isinstance(svc, RpaService)

    def test_pending_service_satisfies_both(self) -> None:
        svc = _FakeServiceWithPending()
        assert isinstance(svc, RpaService)
        assert isinstance(svc, RpaServiceWithPending)

    def test_incomplete_service_fails_protocol(self) -> None:
        svc = _IncompleteService()
        # runtime_checkable Protocol 检查方法签名是否齐全
        # _IncompleteService 缺 resume → 不应通过
        assert not isinstance(svc, RpaService)

    def test_fake_service_not_pending(self) -> None:
        svc = _FakeRpaService()
        # _FakeRpaService 不实现 list_pending → 不应满足 RpaServiceWithPending
        assert not isinstance(svc, RpaServiceWithPending)


# ════════════════════════════════════════════════════════════════════════
# 集成检查：现有 4 个 service 是否符合 Protocol（轻量验证，不强制）
# ════════════════════════════════════════════════════════════════════════


class TestExistingServicesShape:
    """验证现有 service.py 的接口形状（不实例化，只看类是否有方法）。

    若日后某个 service 重构去掉了关键方法，此测试会及早提示。
    """

    def test_line_service_class_has_methods(self) -> None:
        from src.integrations.line_rpa.service import LineRpaService
        for name in ("status", "effective_config", "pause_for", "resume", "trigger_once"):
            assert hasattr(LineRpaService, name), f"LineRpaService missing {name}"

    def test_whatsapp_service_class_has_methods(self) -> None:
        from src.integrations.whatsapp_rpa.service import WhatsAppRpaService
        for name in ("status", "effective_config", "pause_for", "resume", "trigger_once"):
            assert hasattr(WhatsAppRpaService, name), f"WhatsAppRpaService missing {name}"

    def test_messenger_service_class_has_methods(self) -> None:
        from src.integrations.messenger_rpa.service import MessengerRpaService
        for name in ("status", "pause_for", "resume", "trigger_once"):
            assert hasattr(MessengerRpaService, name), f"MessengerRpaService missing {name}"

    def test_pending_capable_services_have_pending_methods(self) -> None:
        """LINE / WhatsApp 的 service 都应有审核队列方法（直接代理给 state_store）。"""
        from src.integrations.line_rpa.service import LineRpaService
        from src.integrations.whatsapp_rpa.service import WhatsAppRpaService
        for cls in (LineRpaService, WhatsAppRpaService):
            for name in ("list_pending", "resolve_pending", "pending_stats"):
                assert hasattr(cls, name), f"{cls.__name__} missing {name}"
