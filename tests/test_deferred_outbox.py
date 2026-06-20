"""多平台 deferred 队列单测。

覆盖：enqueue/drain 基本 + staleness 过期 + kill-switch 推后 + quiet_hours 顺延 +
pacing 最小间隔 + 无 sender 推后 + sender 成功/返回 False/抛异常 + max_per_tick 限流。
"""
from datetime import datetime

from src.integrations.shared.deferred_outbox import (
    DeferredDispatcher,
    DeferredOutboxStore,
    DeferredSenderNotReady,
    shift_out_of_quiet_hours,
)

# 周三 10:00（非安静时段 23-8）
NOW = datetime(2026, 6, 17, 10, 0, 0).timestamp()


def _no_ks(platform, account_id):
    return (False, "", "")


def _recorder():
    sent = []

    async def _send(account_id, chat_key, text):
        sent.append({"account_id": account_id, "chat_key": chat_key, "text": text})
        return True
    return sent, _send


def _disp(store, senders=None, ks=_no_ks, **kw):
    return DeferredDispatcher(
        store=store, senders=senders or {}, kill_switch_check=ks,
        quiet_start_hour=23, quiet_end_hour=8, **kw)


# ── store 基本 ──────────────────────────────────────────────────────────
def test_enqueue_and_drain_due():
    s = DeferredOutboxStore(":memory:")
    rid = s.enqueue(platform="telegram", account_id="a", chat_key="123",
                    reply_text="hi", defer_until=NOW - 10, now=NOW - 100)
    assert rid > 0
    # 未到期的不返回
    s.enqueue(platform="telegram", account_id="a", chat_key="456",
              reply_text="later", defer_until=NOW + 9999, now=NOW)
    due = s.drain_due(now=NOW, limit=10)
    assert len(due) == 1 and due[0]["chat_key"] == "123"


def test_enqueue_rejects_missing_fields():
    s = DeferredOutboxStore(":memory:")
    assert s.enqueue(platform="", account_id="a", chat_key="1",
                     reply_text="x", defer_until=NOW) == 0
    assert s.enqueue(platform="telegram", account_id="a", chat_key="",
                     reply_text="x", defer_until=NOW) == 0
    assert s.enqueue(platform="telegram", account_id="a", chat_key="1",
                     reply_text="  ", defer_until=NOW) == 0


# ── dispatcher 护栏 ─────────────────────────────────────────────────────
async def test_dispatch_success_calls_sender_and_marks_sent():
    s = DeferredOutboxStore(":memory:")
    s.enqueue(platform="telegram", account_id="a", chat_key="123",
              reply_text="你好呀", defer_until=NOW - 10, now=NOW - 100)
    sent, send = _recorder()
    d = _disp(s, {"telegram": send})
    n = await d.run_once(now=NOW)
    assert n == 1 and len(sent) == 1
    assert sent[0]["chat_key"] == "123" and sent[0]["text"] == "你好呀"
    assert s.count(status="sent") == 1 and s.count(status="pending") == 0


async def test_staleness_expires_without_send():
    s = DeferredOutboxStore(":memory:")
    # created 2 天前，staleness 1 天 → 过期
    s.enqueue(platform="telegram", account_id="a", chat_key="123",
              reply_text="old", defer_until=NOW - 10, staleness_sec=86400,
              now=NOW - 2 * 86400)
    sent, send = _recorder()
    d = _disp(s, {"telegram": send})
    n = await d.run_once(now=NOW)
    assert n == 0 and not sent
    assert s.count(status="expired") == 1


async def test_kill_switch_pushes_until():
    s = DeferredOutboxStore(":memory:")
    s.enqueue(platform="telegram", account_id="a", chat_key="123",
              reply_text="hi", defer_until=NOW - 10, now=NOW - 100)
    sent, send = _recorder()

    def _blocked(platform, account_id):
        return (True, "platform:telegram", "manual freeze")
    d = _disp(s, {"telegram": send}, ks=_blocked, ks_backoff_sec=1800)
    n = await d.run_once(now=NOW)
    assert n == 0 and not sent
    assert s.count(status="pending") == 1  # 没丢，推后
    row = s.list_recent(status="pending")[0]
    assert row["defer_until"] >= NOW + 1800 - 1
    assert "kill_switch" in row["reason"]


async def test_quiet_hours_pushes_to_window_end():
    s = DeferredOutboxStore(":memory:")
    late = datetime(2026, 6, 17, 23, 30, 0).timestamp()  # 深夜
    s.enqueue(platform="telegram", account_id="a", chat_key="123",
              reply_text="hi", defer_until=late - 10, now=late - 100)
    sent, send = _recorder()
    d = _disp(s, {"telegram": send})
    n = await d.run_once(now=late)
    assert n == 0 and not sent
    row = s.list_recent(status="pending")[0]
    dd = datetime.fromtimestamp(row["defer_until"])
    assert dd.hour >= 8 and dd.day == 18  # 顺延到次日 08:00
    assert row["reason"] == "quiet_hours"


async def test_pacing_min_gap_pushes_second_send():
    s = DeferredOutboxStore(":memory:")
    # 两条同 (platform,account) 都到期
    s.enqueue(platform="telegram", account_id="a", chat_key="111",
              reply_text="one", defer_until=NOW - 20, now=NOW - 100)
    s.enqueue(platform="telegram", account_id="a", chat_key="222",
              reply_text="two", defer_until=NOW - 10, now=NOW - 100)
    sent, send = _recorder()
    d = _disp(s, {"telegram": send}, min_gap_sec=300, max_per_tick=5)
    n = await d.run_once(now=NOW)
    assert n == 1 and len(sent) == 1  # 第一条发，第二条被 pacing 推后
    pend = s.list_recent(status="pending")
    assert len(pend) == 1 and pend[0]["reason"] == "pacing_min_gap"
    assert pend[0]["defer_until"] >= NOW + 300 - 1


async def test_no_sender_keeps_pending_pushed():
    s = DeferredOutboxStore(":memory:")
    s.enqueue(platform="line", account_id="a", chat_key="123",
              reply_text="hi", defer_until=NOW - 10, now=NOW - 100)
    sent, send = _recorder()
    d = _disp(s, {"telegram": send}, no_sender_backoff_sec=600)  # 没注册 line
    n = await d.run_once(now=NOW)
    assert n == 0 and not sent
    row = s.list_recent(status="pending")[0]
    assert row["reason"] == "no_sender"
    assert row["defer_until"] >= NOW + 600 - 1


async def test_sender_returns_false_marks_failed():
    s = DeferredOutboxStore(":memory:")
    s.enqueue(platform="telegram", account_id="a", chat_key="123",
              reply_text="hi", defer_until=NOW - 10, now=NOW - 100)

    async def _fail(account_id, chat_key, text):
        return False
    d = _disp(s, {"telegram": _fail})
    n = await d.run_once(now=NOW)
    assert n == 0
    assert s.count(status="failed") == 1


async def test_sender_exception_marks_failed():
    s = DeferredOutboxStore(":memory:")
    s.enqueue(platform="telegram", account_id="a", chat_key="123",
              reply_text="hi", defer_until=NOW - 10, now=NOW - 100)

    async def _boom(account_id, chat_key, text):
        raise RuntimeError("boom")
    d = _disp(s, {"telegram": _boom})
    n = await d.run_once(now=NOW)
    assert n == 0
    assert s.count(status="failed") == 1


async def test_sender_not_ready_keeps_pending_pushed():
    s = DeferredOutboxStore(":memory:")
    s.enqueue(platform="telegram", account_id="a", chat_key="123",
              reply_text="hi", defer_until=NOW - 10, now=NOW - 100)

    async def _not_ready(account_id, chat_key, text):
        raise DeferredSenderNotReady("worker down")
    d = _disp(s, {"telegram": _not_ready}, no_sender_backoff_sec=600)
    n = await d.run_once(now=NOW)
    assert n == 0
    assert s.count(status="pending") == 1  # 暂态 → 推后，不标失败
    row = s.list_recent(status="pending")[0]
    assert row["reason"] == "sender_not_ready"
    assert row["defer_until"] >= NOW + 600 - 1


async def test_max_per_tick_limits_sends():
    s = DeferredOutboxStore(":memory:")
    for i in range(5):
        s.enqueue(platform="telegram", account_id=f"a{i}", chat_key=f"c{i}",
                  reply_text="hi", defer_until=NOW - 10, now=NOW - 100)
    sent, send = _recorder()
    # 不同 account → pacing 不互相影响；max_per_tick=2 限流
    d = _disp(s, {"telegram": send}, max_per_tick=2)
    n = await d.run_once(now=NOW)
    assert n == 2 and len(sent) == 2
    assert s.count(status="pending") == 3


def test_register_sender_and_has_sender():
    s = DeferredOutboxStore(":memory:")
    d = _disp(s)
    assert d.has_sender("telegram") is False

    async def _send(a, c, t):
        return True
    d.register_sender("telegram", _send)
    assert d.has_sender("telegram") is True


def test_quiet_hours_pure_fn():
    assert shift_out_of_quiet_hours(NOW, start_hour=8, end_hour=8) == NOW
    assert shift_out_of_quiet_hours(NOW, start_hour=23, end_hour=8) == NOW


async def test_stats_groups_pending_by_reason():
    s = DeferredOutboxStore(":memory:")
    # 两条同账号到期 + 一条未注册平台 → 一条发、一条 pacing、一条 no_sender
    s.enqueue(platform="telegram", account_id="a", chat_key="1",
              reply_text="one", defer_until=NOW - 30, now=NOW - 100)
    s.enqueue(platform="telegram", account_id="a", chat_key="2",
              reply_text="two", defer_until=NOW - 20, now=NOW - 100)
    s.enqueue(platform="line", account_id="a", chat_key="3",
              reply_text="three", defer_until=NOW - 10, now=NOW - 100)
    sent, send = _recorder()
    d = _disp(s, {"telegram": send}, min_gap_sec=300, max_per_tick=5)
    await d.run_once(now=NOW)
    st = s.stats()
    assert st["by_status"].get("sent") == 1
    assert st["by_status"].get("pending") == 2
    # 一条卡 pacing、一条卡 no_sender
    assert st["pending_by_reason"].get("pacing_min_gap") == 1
    assert st["pending_by_reason"].get("no_sender") == 1
    assert st["pending_by_platform"].get("telegram") == 1
    assert st["pending_by_platform"].get("line") == 1


def test_registered_platforms_sorted():
    s = DeferredOutboxStore(":memory:")
    d = _disp(s)

    async def _x(a, c, t):
        return True
    d.register_sender("whatsapp", _x)
    d.register_sender("line", _x)
    assert d.registered_platforms() == ["line", "whatsapp"]


# ── 运营动作：重试 / 取消 / 清理 / 暂停 ──────────────────────────────────
def test_requeue_single_terminal_row():
    s = DeferredOutboxStore(":memory:")
    rid = s.enqueue(platform="line", account_id="a", chat_key="c",
                    reply_text="x", defer_until=NOW, now=NOW)
    s.mark_failed(rid, "boom")
    assert s.requeue(rid, now=NOW) is True
    assert s.count(status="pending") == 1 and s.count(status="failed") == 0
    # 已是 pending 的不可再 requeue（仅终态可）
    assert s.requeue(rid, now=NOW) is False


def test_requeue_status_bulk_only_terminal():
    s = DeferredOutboxStore(":memory:")
    for i in range(3):
        rid = s.enqueue(platform="line", account_id="a", chat_key=f"c{i}",
                        reply_text="x", defer_until=NOW, now=NOW)
        s.mark_failed(rid, "boom")
    assert s.requeue_status("failed", now=NOW) == 3
    assert s.count(status="pending") == 3
    # 非法 status 不动
    assert s.requeue_status("pending", now=NOW) == 0


def test_cancel_pending_requires_filter():
    s = DeferredOutboxStore(":memory:")
    s.enqueue(platform="line", account_id="a", chat_key="c",
              reply_text="x", defer_until=NOW + 9999, now=NOW)
    # 无过滤条件 → 不动（防误清空）
    assert s.cancel_pending() == 0
    assert s.count(status="pending") == 1


def test_cancel_pending_by_reason_and_platform():
    s = DeferredOutboxStore(":memory:")
    r1 = s.enqueue(platform="line", account_id="a", chat_key="c1",
                   reply_text="x", defer_until=NOW + 9999, now=NOW)
    s.push_until(r1, NOW + 9999, note="no_sender")
    s.enqueue(platform="telegram", account_id="a", chat_key="c2",
              reply_text="x", defer_until=NOW + 9999, now=NOW, reason="other")
    assert s.cancel_pending(reason="no_sender") == 1
    assert s.count(status="cancelled") == 1
    assert s.count(status="pending") == 1
    # 按平台取消剩下那条
    assert s.cancel_pending(platform="telegram") == 1
    assert s.count(status="pending") == 0


def test_purge_terminal_removes_old_only():
    s = DeferredOutboxStore(":memory:")
    old = s.enqueue(platform="line", account_id="a", chat_key="old",
                    reply_text="x", defer_until=NOW, now=NOW - 100000)
    s.mark_failed(old, "boom")
    fresh = s.enqueue(platform="line", account_id="a", chat_key="fresh",
                      reply_text="x", defer_until=NOW, now=NOW)
    s.mark_failed(fresh, "boom")
    removed = s.purge_terminal(older_than_sec=3600, now=NOW)
    assert removed == 1
    rows = s.list_recent(limit=10)
    assert [r["chat_key"] for r in rows] == ["fresh"]


async def test_paused_platform_pushes_without_send():
    s = DeferredOutboxStore(":memory:")
    s.enqueue(platform="line", account_id="a", chat_key="c",
              reply_text="x", defer_until=NOW - 10, now=NOW - 100)
    sent, send = _recorder()
    d = _disp(s, {"line": send})
    d.pause("line")
    n = await d.run_once(now=NOW)
    assert n == 0 and len(sent) == 0
    assert s.count(status="pending") == 1
    assert s.stats()["pending_by_reason"].get("paused") == 1
    # resume 后恢复投递（暂停时 defer_until 被推后 interval，故须越过该时点）
    d.resume("line")
    n2 = await d.run_once(now=NOW + 200)
    assert n2 == 1 and len(sent) == 1
