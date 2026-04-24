"""P5 阶段测试：AccountPool 并发 / 状态机随机轨迹 / Chaos ADB mock。

不依赖 hypothesis 等外部库；全部手写伪随机 property test。
"""
from __future__ import annotations

import asyncio
import os
import random
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


# ── P5-1：AccountRegistry / AccountPool ──────────
def test_account_registry_compat():
    """未配 accounts 时回到单账号 default，不破坏旧 state db 路径。"""
    print("\n=== test_account_registry_compat ===")
    from src.integrations.messenger_rpa.account_pool import AccountRegistry
    tmp = tempfile.mkdtemp(prefix="p5reg_")
    cfg_path = Path(tmp) / "config.yaml"
    cfg_path.write_text("# test")
    cfg = {"adb_serial": "192.168.0.113:5555"}  # 旧单账号配置
    reg = AccountRegistry.from_config(cfg, cfg_path)
    assert reg.size() == 1, reg.size()
    ctx = reg.get("default")
    assert ctx is not None
    assert ctx.account_id == "default"
    assert ctx.adb_serial == "192.168.0.113:5555"
    # 兼容：default 路径沿用旧名
    assert ctx.state_db_path.name == "messenger_rpa_state.db", ctx.state_db_path
    # chat_key 兼容（default 不加前缀）
    assert ctx.prefix_chat_key("messenger_rpa:xxx") == "messenger_rpa:xxx"
    ok("单账号兼容模式")


def test_account_registry_multi():
    """多账号 config 正确生成各账号的独立 state db + chat_key 前缀。"""
    print("\n=== test_account_registry_multi ===")
    from src.integrations.messenger_rpa.account_pool import AccountRegistry
    tmp = tempfile.mkdtemp(prefix="p5reg2_")
    cfg_path = Path(tmp) / "config.yaml"
    cfg_path.write_text("# test")
    cfg = {
        "accounts": [
            {"id": "acc_A", "adb_serial": "192.168.0.113:5555", "label": "A"},
            {"id": "acc_B", "adb_serial": "192.168.0.114:5555",
             "overrides": {"reply_mode": "approve"}},
        ],
        "account_max_parallel": 3,
    }
    reg = AccountRegistry.from_config(cfg, cfg_path)
    assert reg.size() == 2
    ctxs = reg.all_contexts()
    paths = {c.account_id: c.state_db_path.name for c in ctxs}
    assert paths["acc_A"] == "messenger_rpa_state_acc_A.db", paths
    assert paths["acc_B"] == "messenger_rpa_state_acc_B.db", paths
    ok("两账号独立 state db 路径")
    # chat_key 加前缀
    ctxA = reg.get("acc_A")
    assert ctxA.prefix_chat_key("messenger_rpa:peer1") == (
        "acc_acc_A:messenger_rpa:peer1"
    )
    # 幂等（二次加不重复）
    assert ctxA.prefix_chat_key("acc_acc_A:xxx") == "acc_acc_A:xxx"
    ok("chat_key 前缀幂等")
    # overlay 生效
    ctxB = reg.get("acc_B")
    merged = ctxB.merged_config({"reply_mode": "auto",
                                 "max_sends_per_day": 40})
    assert merged["reply_mode"] == "approve"
    assert merged["max_sends_per_day"] == 40  # 未被 overlay 覆盖的保留
    assert merged["adb_serial"] == "192.168.0.114:5555"
    ok("config overlay 合并正确")


def test_account_pool_concurrency():
    """同 account 串行；不同 account 并发。"""
    print("\n=== test_account_pool_concurrency ===")
    from src.integrations.messenger_rpa.account_pool import AccountPool

    async def run():
        pool = AccountPool(max_parallel=5)
        # 同 account 串行
        order = []

        async def worker(aid, tag, sleep):
            async with pool.acquire(aid):
                order.append(("enter", tag, time.time()))
                await asyncio.sleep(sleep)
                order.append(("exit", tag, time.time()))

        # 3 个 worker 抢同一个 account；每个 sleep 0.1s
        t0 = time.time()
        await asyncio.gather(
            worker("A", "a1", 0.1),
            worker("A", "a2", 0.1),
            worker("A", "a3", 0.1),
        )
        elapsed = time.time() - t0
        assert 0.28 <= elapsed <= 0.6, f"串行应 ~0.3s，实测 {elapsed:.2f}s"
        # 检查严格交替 enter/exit
        events = [(kind, tag) for (kind, tag, _ts) in order]
        for i in range(0, len(events), 2):
            assert events[i][0] == "enter" and events[i + 1][0] == "exit" and \
                events[i][1] == events[i + 1][1], events
        ok(f"同 account 严格串行（3 任务 x 0.1s = {elapsed:.2f}s）")

        # 不同 account 并发
        t0 = time.time()
        await asyncio.gather(
            worker("B", "b1", 0.2),
            worker("C", "c1", 0.2),
            worker("D", "d1", 0.2),
        )
        elapsed = time.time() - t0
        assert 0.15 <= elapsed <= 0.35, (
            f"3 account 并发应 ~0.2s，实测 {elapsed:.2f}s"
        )
        ok(f"跨 account 真并发（3 任务 x 0.2s 并发 = {elapsed:.2f}s）")

        # max_parallel 限制
        pool2 = AccountPool(max_parallel=2)
        t0 = time.time()
        await asyncio.gather(*[
            (asyncio.create_task(
                _one_acquire(pool2, f"acc_{i}", 0.15)
            )) for i in range(4)
        ])
        elapsed = time.time() - t0
        # 4 个 account × 0.15s，max_parallel=2 → 应 ~0.3s
        assert 0.25 <= elapsed <= 0.5, (
            f"max_parallel=2 + 4 account 应 ~0.3s，实测 {elapsed:.2f}s"
        )
        ok(f"max_parallel=2 正确节流（实测 {elapsed:.2f}s）")

    async def _one_acquire(pool, aid, sleep):
        async with pool.acquire(aid):
            await asyncio.sleep(sleep)

    asyncio.run(run())


# ── P5-2：状态机随机轨迹不变量 ──────────────────
def test_credit_invariants():
    """500 次随机 adjust，验 credit 始终 ∈ [0,100]。"""
    print("\n=== test_credit_invariants ===")
    from src.integrations.messenger_rpa.state_store import MessengerRpaStateStore
    tmp = tempfile.mkdtemp(prefix="p5inv_")
    store = MessengerRpaStateStore(os.path.join(tmp, "t.db"))
    rng = random.Random(42)
    chats = [f"c{i}" for i in range(10)]
    for i in range(500):
        ck = rng.choice(chats)
        delta = rng.choice([-50, -20, -15, -10, -5, -2, 0, 2, 5, 10, 50])
        store.adjust_credit(ck, delta, reason=f"random_{i}")
        c = store.get_credit(ck)["credit"]
        assert 0 <= c <= 100, (i, ck, c)
    # 最终分布
    stats = store.credit_stats()
    assert stats["total_tracked"] == 10
    ok("500 次随机 adjust: credit 始终 ∈ [0,100]")


def test_risk_state_monotonic():
    """风控状态严格按照 consecutive 规则演化。

    不变量：
      - status ∈ {normal, warning_once, blocked}
      - 一旦 blocked 且未过期 → clear_risk 不会降级
      - hit_count >= 0
    """
    print("\n=== test_risk_state_monotonic ===")
    from src.integrations.messenger_rpa.state_store import MessengerRpaStateStore
    tmp = tempfile.mkdtemp(prefix="p5rk_")
    store = MessengerRpaStateStore(os.path.join(tmp, "t.db"))
    rng = random.Random(7)
    for i in range(200):
        op = rng.random()
        if op < 0.7:
            sev = rng.choice(["warn", "block"])
            store.record_risk_hit(severity=sev, reason=f"r{i}",
                                  require_consecutive=2,
                                  block_duration_sec=3600)
        else:
            store.clear_risk()
        st = store.get_risk_state()
        assert st["status"] in ("normal", "warning_once", "blocked"), st
        assert st["hit_count"] >= 0
        if st["status"] == "blocked":
            assert st["blocked_until_ts"] > 0
    ok("200 次随机 hit/clear: 风控状态合法")


def test_pace_does_not_crash():
    """Pace check 在空 / 少量 / 大量样本时都不崩且决策合法。"""
    print("\n=== test_pace_does_not_crash ===")
    from src.integrations.messenger_rpa.state_store import MessengerRpaStateStore
    tmp = tempfile.mkdtemp(prefix="p5pc_")
    store = MessengerRpaStateStore(os.path.join(tmp, "t.db"))
    rng = random.Random(11)
    legal = {"allow", "throttle", "deny", "allow_on_error"}
    for n_samples in (0, 5, 25, 100, 500):
        # 插入 n 个 send log
        import sqlite3
        now = time.time()
        conn = sqlite3.connect(store._db_path, timeout=10)
        for i in range(n_samples):
            ts = now - rng.randint(0, 14 * 86400)
            h = int(time.localtime(ts).tm_hour)
            conn.execute(
                "INSERT INTO messenger_rpa_send_log(ts, hour_local) VALUES(?,?)",
                (ts, h),
            )
        conn.commit()
        conn.close()
        r = store.pace_check()
        assert r["decision"] in legal, r
        assert r["samples"] >= 0
    ok("pace_check 在 0/5/25/100/500 样本下均正常")


# ── P5-2：Chaos ADB mock ─────────────────────
def test_chaos_adb_mock():
    """mock adb_helpers 让 50% 调用失败；ensure_device_ready 不崩、及时退出。"""
    print("\n=== test_chaos_adb_mock ===")
    import src.integrations.line_rpa.adb_helpers as adb
    from src.integrations.messenger_rpa import device_health

    class FakeRes:
        def __init__(self, rc=0, stdout="", stderr=""):
            self.returncode = rc
            self.stdout = stdout
            self.stderr = stderr

    # 保存原函数
    orig_run_adb = adb.run_adb
    orig_list = adb.list_device_serials
    call_count = [0]

    def fake_run(args, serial=None, timeout=5.0):
        call_count[0] += 1
        # 50% 失败，30% 返回假 device 在线消息
        r = random.Random(call_count[0]).random()
        if r < 0.5:
            return FakeRes(rc=1, stdout="", stderr="mocked failure")
        # "connected to 192.168.0.1:5555"
        if args and args[0] == "connect":
            return FakeRes(rc=0, stdout="connected to foo")
        if args and args[0] == "kill-server":
            return FakeRes(rc=0)
        if args and args[0] == "start-server":
            return FakeRes(rc=0)
        # shell dumpsys power → screen_on
        if "dumpsys" in args and "power" in args:
            return FakeRes(rc=0, stdout="mWakefulness=Awake")
        if "dumpsys" in args and "window" in args:
            return FakeRes(rc=0, stdout="mDreamingLockscreen=false")
        return FakeRes(rc=0, stdout="")

    def fake_list():
        # 偶尔才"看到"device
        return ["192.168.0.1:5555"] if random.random() < 0.3 else []

    adb.run_adb = fake_run
    adb.list_device_serials = fake_list
    try:
        t0 = time.time()
        healthy, info = device_health.ensure_device_ready(
            "192.168.0.1:5555",
            max_attempts=3, backoff_sec=0.01,  # 缩短等待，避免 test 慢
            hard_restart_on_fail=True,
        )
        elapsed = time.time() - t0
        # 最主要的断言：不抛异常、结构完整
        assert isinstance(healthy, bool)
        assert "attempts" in info
        assert len(info["attempts"]) <= 3
        assert elapsed < 30, f"ensure_device_ready 卡死 {elapsed:.1f}s"
        ok(f"chaos mock 下 ensure_device_ready 正确返回: healthy={healthy} "
           f"attempts={len(info['attempts'])} t={elapsed:.2f}s")
    finally:
        adb.run_adb = orig_run_adb
        adb.list_device_serials = orig_list


# ── P5-4：AI tier overrides ──────────────────
def test_ai_tier_apply():
    """_apply_tier_overrides 正确 merge 而不覆盖显式 strategy_overrides。"""
    print("\n=== test_ai_tier_apply ===")
    from src.ai.ai_client import AIClient
    client = AIClient.__new__(AIClient)  # 不走 __init__
    client._tiers_enabled = True
    client._tiers_default = "normal"
    client._tiers = {
        "premium": {"model": "gpt-4o", "temperature": 0.6, "max_tokens": 1200},
        "normal":  {"model": "deepseek", "temperature": 0.7, "max_tokens": 800},
        "low":     {"model": "mini", "temperature": 0.8, "max_tokens": 300},
    }
    # 1) 不给 ai_tier → 默认 normal
    r = client._apply_tier_overrides(None, {})
    assert r["model"] == "deepseek" and r["_ai_tier"] == "normal", r
    ok("default=normal 应用成功")

    # 2) ai_tier=premium
    r = client._apply_tier_overrides(None, {"ai_tier": "premium"})
    assert r["model"] == "gpt-4o" and r["max_tokens"] == 1200, r
    ok("premium 档应用成功")

    # 3) 显式 strategy_overrides 优先（setdefault 语义）
    r = client._apply_tier_overrides(
        {"model": "custom-model"}, {"ai_tier": "premium"}
    )
    assert r["model"] == "custom-model"  # 不被覆盖
    assert r["temperature"] == 0.6        # tier 补齐
    ok("显式 overrides 不被 tier 覆盖")

    # 4) 未知 tier → fallback 到 default
    r = client._apply_tier_overrides(None, {"ai_tier": "ghost"})
    assert r["model"] == "deepseek"
    ok("未知 tier fallback default")

    # 5) disabled → 直接返回原值
    client._tiers_enabled = False
    r = client._apply_tier_overrides({"x": 1}, {"ai_tier": "premium"})
    assert r == {"x": 1}
    ok("disabled 不生效")


def test_runner_tier_classify():
    """Runner._classify_ai_tier 按 credit + 关键词分档。"""
    print("\n=== test_runner_tier_classify ===")
    from src.integrations.messenger_rpa.runner import MessengerRpaRunner
    runner = MessengerRpaRunner.__new__(MessengerRpaRunner)
    runner._cfg = {"ai": {"tiers": {"enabled": True}}}

    # low 分支：credit < 40
    r = runner._classify_ai_tier("c1", "Hi there", {"credit": {"credit": 30}})
    assert r == "low", r
    ok("credit<40 → low")

    # low 分支：极短 + low_keyword
    r = runner._classify_ai_tier("c1", "ok", {"credit": {"credit": 80}})
    assert r == "low", r
    ok("短 + ok → low")

    # premium 分支：高信用 + money keyword
    r = runner._classify_ai_tier(
        "c1", "How much is the price?", {"credit": {"credit": 90}},
    )
    assert r == "premium", r
    ok("高信用 + money keyword → premium")

    # normal 兜底
    r = runner._classify_ai_tier(
        "c1", "Nice weather today, what have you been up to lately?",
        {"credit": {"credit": 70}},
    )
    assert r == "normal", r
    ok("默认 → normal")

    # disabled
    runner._cfg = {"ai": {"tiers": {"enabled": False}}}
    r = runner._classify_ai_tier("c1", "anything", {})
    assert r is None
    ok("disabled 返回 None")


if __name__ == "__main__":
    test_account_registry_compat()
    test_account_registry_multi()
    test_account_pool_concurrency()
    test_credit_invariants()
    test_risk_state_monotonic()
    test_pace_does_not_crash()
    test_chaos_adb_mock()
    test_ai_tier_apply()
    test_runner_tier_classify()
    print(f"\n=== TOTAL: {ok_cnt} OK / {fail_cnt} FAIL ===")
    sys.exit(0 if fail_cnt == 0 else 1)
