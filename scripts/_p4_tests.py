"""P4 阶段单元测试：pace_check / credit / replay.rerun 路径解析 / device_health 组合。"""
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ok_cnt = 0
fail_cnt = 0


def ok(msg):
    global ok_cnt
    ok_cnt += 1
    print(f"  [OK] {msg}")


def fail(msg):
    global fail_cnt
    fail_cnt += 1
    print(f"  [FAIL] {msg}")


def test_pace_check():
    print("\n=== test_pace_check ===")
    from src.integrations.messenger_rpa.state_store import MessengerRpaStateStore
    tmp = tempfile.mkdtemp(prefix="p4pace_")
    store = MessengerRpaStateStore(os.path.join(tmp, "t.db"))

    # 冷启动：0 样本 → allow
    r = store.pace_check(min_samples=20)
    assert r["allow"] and r["samples"] == 0 and r["decision"] == "allow", r
    ok("冷启动 allow")

    # 手工插入历史数据：过去 7 天同一小时每天 4 条
    import sqlite3
    now = time.time()
    hour = int(time.localtime(now).tm_hour)
    conn = sqlite3.connect(store._db_path, timeout=10)
    for d in range(1, 8):
        day_start = now - d * 86400
        for i in range(4):
            # 精确落到 local hour
            ts = day_start - (day_start % 3600) + hour * 3600 + i * 300
            conn.execute(
                "INSERT INTO messenger_rpa_send_log(ts, hour_local) VALUES(?,?)",
                (ts, hour),
            )
    conn.commit()
    conn.close()

    # 现在本小时没发过 → ratio=0/4=0 → allow
    r = store.pace_check(min_samples=20)
    assert r["allow"] and r["decision"] == "allow", r
    ok(f"历史有数据本小时 0 → allow (hist_median={r['hist_median']})")

    # 手动插入本小时 5 条发送 → ratio=5/4=1.25 → allow
    conn = sqlite3.connect(store._db_path, timeout=10)
    hour_start = now - (now % 3600)
    for i in range(5):
        conn.execute(
            "INSERT INTO messenger_rpa_send_log(ts, hour_local) VALUES(?,?)",
            (hour_start + i * 60, hour),
        )
    conn.commit()
    conn.close()
    r = store.pace_check(min_samples=20, median_multiplier=1.5,
                          block_multiplier=2.5)
    assert r["allow"] and r["decision"] == "allow", r
    ok(f"ratio={r['ratio']} < 1.5 → allow")

    # 再加 3 条（本小时共 8）→ ratio=8/4=2.0 → throttle
    conn = sqlite3.connect(store._db_path, timeout=10)
    for i in range(3):
        conn.execute(
            "INSERT INTO messenger_rpa_send_log(ts, hour_local) VALUES(?,?)",
            (hour_start + 300 + i * 60, hour),
        )
    conn.commit()
    conn.close()
    r = store.pace_check(min_samples=20)
    assert r["throttle"] and r["decision"] == "throttle", r
    ok(f"ratio={r['ratio']} >= 1.5 → throttle")

    # 再加 5 条（本小时共 13）→ ratio=13/4=3.25 → deny
    conn = sqlite3.connect(store._db_path, timeout=10)
    for i in range(5):
        conn.execute(
            "INSERT INTO messenger_rpa_send_log(ts, hour_local) VALUES(?,?)",
            (hour_start + 600 + i * 60, hour),
        )
    conn.commit()
    conn.close()
    r = store.pace_check(min_samples=20)
    assert not r["allow"] and r["decision"] == "deny", r
    ok(f"ratio={r['ratio']} >= 2.5 → deny")


def test_credit():
    print("\n=== test_credit ===")
    from src.integrations.messenger_rpa.state_store import MessengerRpaStateStore
    tmp = tempfile.mkdtemp(prefix="p4credit_")
    store = MessengerRpaStateStore(os.path.join(tmp, "t.db"))

    # 新 chat 默认 100
    r = store.get_credit("chat_A")
    assert r["credit"] == 100
    ok("默认 100")

    # 扣 -15 (reject) × 4 次 → 100 - 60 = 40
    for i in range(4):
        store.adjust_credit("chat_A", -15, reason=f"reject {i}")
    r = store.get_credit("chat_A")
    assert r["credit"] == 40, r
    ok("连续 reject 扣到 40")

    # 再扣 -15 → 25
    store.adjust_credit("chat_A", -15, reason="reject 5")
    r = store.get_credit("chat_A")
    assert r["credit"] == 25, r
    ok("继续扣到 25（进入 blacklist 区间下沿附近）")

    # 加 +2 恢复 → 27
    store.adjust_credit("chat_A", 2, reason="send_ok")
    r = store.get_credit("chat_A")
    assert r["credit"] == 27
    ok("+2 恢复")

    # 扣到底 → floor clamp 0
    store.adjust_credit("chat_A", -100, reason="crash")
    r = store.get_credit("chat_A")
    assert r["credit"] == 0, r
    ok("clamp 到 floor=0")

    # 加超 100 → ceil clamp 100
    store.adjust_credit("chat_A", 200, reason="manual_reset")
    r = store.get_credit("chat_A")
    assert r["credit"] == 100
    ok("clamp 到 ceil=100")

    # 多 chat 分布
    for i, c in enumerate([95, 75, 55, 35, 15]):
        store.adjust_credit(f"chat_B{i}", c - 100, reason="test")
    stats = store.credit_stats()
    assert stats["total_tracked"] >= 6, stats
    assert stats["distribution"]["100"] >= 1  # chat_A
    assert stats["distribution"]["80_99"] >= 1  # 95
    assert stats["distribution"]["60_79"] >= 1  # 75
    assert stats["distribution"]["40_59"] >= 1  # 55
    assert stats["distribution"]["20_39"] >= 1  # 35
    assert stats["distribution"]["0_19"] >= 1  # 15
    ok(f"分布: {stats['distribution']}")

    # low_credit_chats
    low = stats["low_credit_chats"]
    assert any(r["credit"] < 40 for r in low)
    ok(f"低信用候选 {len(low)} 个")


def test_replay_resolve():
    print("\n=== test_replay_resolve ===")
    from src.integrations.messenger_rpa.replay import (
        _resolve_zip, _simple_diff,
    )
    tmp = tempfile.mkdtemp(prefix="p4replay_")
    cfg = {"debug_screenshot_dir": tmp}
    (Path(tmp) / "replays").mkdir()
    zp = Path(tmp) / "replays" / "20260421_abc.zip"
    zp.write_bytes(b"PK\x03\x04")  # fake empty zip header
    # basename
    r = _resolve_zip("20260421_abc.zip", cfg)
    assert r == zp
    ok("basename 解析")
    # abs path
    r = _resolve_zip(str(zp), cfg)
    assert r == zp
    ok("abs 解析")
    # not found
    try:
        _resolve_zip("nope.zip", cfg)
        fail("不存在应该抛错")
    except FileNotFoundError:
        ok("不存在抛 FileNotFoundError")

    # diff hint
    assert _simple_diff("", "") == "both empty"
    assert _simple_diff("aaa", "aaa") == "identical"
    d = _simple_diff("hello world", "hello python")
    assert "jaccard" in d, d
    ok(f"simple_diff 工作正常: {d}")


def test_device_health_imports():
    print("\n=== test_device_health_imports ===")
    from src.integrations.messenger_rpa.device_health import (
        ensure_device_ready, _get_current_ime, _set_ime,
        _hard_restart_adb_server, probe_devices,
    )
    # 不真连 adb，只验签名
    import inspect
    sig = inspect.signature(ensure_device_ready)
    assert "preferred_ime" in sig.parameters
    assert "hard_restart_on_fail" in sig.parameters
    ok("ensure_device_ready 新参数齐全")


if __name__ == "__main__":
    test_pace_check()
    test_credit()
    test_replay_resolve()
    test_device_health_imports()
    print(f"\n=== TOTAL: {ok_cnt} OK / {fail_cnt} FAIL ===")
    sys.exit(0 if fail_cnt == 0 else 1)
