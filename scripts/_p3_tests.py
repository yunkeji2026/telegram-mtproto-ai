"""P3 阶段单元测试集：risk / metrics / replay / state_store.risk / episodic。"""
import asyncio
import json
import os
import sys
import tempfile
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ok_cnt = 0
fail_cnt = 0


def ok(msg: str) -> None:
    global ok_cnt
    ok_cnt += 1
    print(f"  [OK] {msg}")


def fail(msg: str) -> None:
    global fail_cnt
    fail_cnt += 1
    print(f"  [FAIL] {msg}")


# ── 1) RiskSignal 解析 & 白名单 ──────────────
def test_risk_parse():
    print("\n=== test_risk_parse ===")
    from src.integrations.messenger_rpa.combined_vision import _parse_risk_dict

    r = _parse_risk_dict({"hit": True, "severity": "block",
                           "reason": "Your account has been restricted"})
    assert r.hit and r.severity == "block", f"expected hit+block, got {r}"
    ok("block 命中正确")

    r = _parse_risk_dict({"hit": True, "severity": "warn", "reason": "Unusual activity detected"})
    assert r.hit and r.severity == "warn", f"expected warn, got {r}"
    ok("warn 命中正确")

    # 白名单过滤 — E2EE 不是风控
    r = _parse_risk_dict({"hit": True, "severity": "warn",
                           "reason": "Messages are now encrypted"})
    assert not r.hit, f"E2EE 应该被白名单过滤, got {r}"
    ok("E2EE 白名单过滤正确")

    # Active now 不是风控
    r = _parse_risk_dict({"hit": True, "severity": "block", "reason": "Active now"})
    assert not r.hit, f"Active now 应该被过滤, got {r}"
    ok("Active now 白名单过滤正确")

    # reason 太短 → 不信任
    r = _parse_risk_dict({"hit": True, "severity": "block", "reason": "abc"})
    assert not r.hit, f"短 reason 应被拒, got {r}"
    ok("短 reason 过滤正确")

    r = _parse_risk_dict(None)
    assert not r.hit
    ok("None 输入处理正常")


# ── 2) state_store risk 累计 + block ─────────
def test_state_risk():
    print("\n=== test_state_risk ===")
    from src.integrations.messenger_rpa.state_store import MessengerRpaStateStore

    tmp = tempfile.mkdtemp(prefix="p3test_")
    db = os.path.join(tmp, "t.db")
    store = MessengerRpaStateStore(db)

    st = store.get_risk_state()
    assert st["status"] == "normal" and st["hit_count"] == 0
    ok("初始状态 normal")

    # 第 1 次 warn → 还是 normal（require_consecutive=2）
    rec = store.record_risk_hit(severity="warn", reason="Unusual activity A",
                                 require_consecutive=2)
    assert rec["status"] == "normal" and rec["hit_count"] == 1, f"{rec}"
    ok("首次 warn 不升级")

    # 第 2 次 warn → warning_once
    rec = store.record_risk_hit(severity="warn", reason="Unusual activity B",
                                 require_consecutive=2)
    assert rec["status"] == "warning_once" and rec.get("just_warned"), f"{rec}"
    ok("连续 warn 升级 warning_once")

    # 现在开始 block → 再需 2 次才 block
    rec = store.record_risk_hit(severity="block", reason="Account restricted A",
                                 require_consecutive=2)
    assert not rec.get("just_blocked"), f"{rec}"
    ok("首次 block 不立即 pause")

    rec = store.record_risk_hit(severity="block", reason="Account restricted B",
                                 require_consecutive=2, block_duration_sec=3600)
    assert rec.get("just_blocked") and rec["status"] == "blocked", f"{rec}"
    ok("连续 block 升级 blocked")

    blocked, until = store.is_risk_blocked_now()
    assert blocked and until > time.time(), f"blocked={blocked} until={until}"
    ok("is_risk_blocked_now 返回 True")

    # clear_risk 在 blocked 下应保持 blocked
    store.clear_risk()
    assert store.get_risk_state()["status"] == "blocked"
    ok("blocked 下 clear_risk 保持 blocked")

    # store 没有 close —— 在 Windows 上不删 tmp 目录即可


# ── 3) metrics 汇总 ────────────────────
def test_metrics():
    print("\n=== test_metrics ===")
    from src.integrations.messenger_rpa.metrics import get_metrics, MessengerRpaMetrics

    m = MessengerRpaMetrics()
    m.observe_run({
        "ok": True, "total_ms": 3500,
        "phase_ms": {"inbox_vision": 1200, "thread_vision": 1500, "llm": 800},
        "step": "done", "reply_text": "hi",
        "caption_source": "prefetch",
    })
    m.observe_run({
        "ok": True, "total_ms": 6000,
        "phase_ms": {"inbox_vision": 2000, "thread_vision": 2000, "llm": 2000},
        "step": "done", "reply_text": "hello",
        "caption_source": "sync",
    })
    m.observe_run({"ok": False, "error": "skill_error:Foo", "step": "reply_failed",
                    "total_ms": 500})

    d = m.dump()
    assert d["run_duration"]["count"] == 3, f"count={d['run_duration']['count']}"
    assert d["run_outcomes"]["ok"] == 2
    assert d["run_outcomes"]["error"] == 1
    assert d["caption_sources"]["prefetch"] == 1
    assert d["caption_sources"]["sync"] == 1
    assert d["phase_duration"]["llm"]["count"] == 2
    ok(f"metrics dump 正确: runs={d['run_duration']['count']} outcomes={d['run_outcomes']}")

    # 单例
    a = get_metrics()
    b = get_metrics()
    assert a is b
    ok("get_metrics 是单例")


# ── 4) replay.maybe_pack_run ────────────
def test_replay_pack():
    print("\n=== test_replay_pack ===")
    from src.integrations.messenger_rpa.replay import (
        maybe_pack_run, list_replays, _classify_error, _rate_allow,
    )
    tmp = tempfile.mkdtemp(prefix="p3replay_")
    cfg = {"debug_screenshot_dir": tmp, "messenger_rpa": {}}

    # 1) no error → no pack
    r = maybe_pack_run({"ok": True, "error": ""}, cfg)
    assert r is None
    ok("no error 不打包")

    # 2) risk_blocked_until → skip
    r = maybe_pack_run({"ok": False, "error": "risk_blocked_until=1234"}, cfg)
    assert r is None
    ok("risk_blocked_until 跳过打包")

    # 3) skill_error → pack
    result = {
        "ok": False,
        "error": "skill_error:ValueError:bad",
        "step": "reply_failed",
        "run_id": "deadbeef",
        "chat_key": "test_chat",
        "peer_text": "hi",
        "reply_text": "",
        "total_ms": 3456,
        "screenshot_path": "",
    }
    zip_path = maybe_pack_run(result, cfg)
    assert zip_path and os.path.exists(zip_path), f"zip_path={zip_path}"
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        assert "run_result.json" in names
        assert "meta.json" in names
        d = json.loads(zf.read("run_result.json"))
        assert d["run_id"] == "deadbeef"
        m = json.loads(zf.read("meta.json"))
        assert m["error_class"] == "skill_error"
    ok(f"打包成功: {os.path.basename(zip_path)}")

    items, base = list_replays(cfg)
    assert len(items) >= 1
    ok(f"list_replays 列出 {len(items)} 个包")

    # 4) rate limit：同类 error 打 5 次应该限到 3 次（当前已用 1，再打 2 次应 OK，第 3/4/5 次应被限）
    successes = 0
    for i in range(5):
        r = maybe_pack_run(
            {**result, "run_id": f"r{i}", "error": f"skill_error:Bar{i}"}, cfg,
        )
        if r:
            successes += 1
    # 上面第 3 中已经 +1，这里再打 5 次中只能成功 2 次（3-1=2）
    assert successes == 2, f"rate-limited 期望 2 次成功，实际 {successes}"
    ok(f"rate limit 正确: 5 次中只成功 {successes} 次")


# ── 5) episodic summary 配置链路（不触 LLM）─
def test_episodic_plumbing():
    print("\n=== test_episodic_plumbing ===")
    # 只验配置读取 + 触发阈值判断
    from src.integrations.messenger_rpa.runner import MessengerRpaRunner
    # 构造一个 mock runner 不实际运行
    class FakeCtx(dict):
        pass
    class FakeCS:
        def __init__(self, ctx): self._c = ctx
        def get(self, k): return self._c
        def mark_dirty(self, k): self._c["__dirty"] = True
        def flush(self, k): pass
    class FakeAI:
        async def summarize_conversation(self, hist, max_chars=200, timeout_sec=12):
            return "（摘要占位）用户问候并确认订单，助手已回复稍后跟进。"
    class FakeSM:
        def __init__(self, cs, ai):
            self._context_store = cs
            self._ai_client = ai

    # 构造带 12 轮 history 的 context
    hist = []
    for i in range(12):
        hist.append({"role": "user", "content": f"msg{i}"})
        hist.append({"role": "assistant", "content": f"reply{i}"})
    ctx = FakeCtx({"_conversation_history": hist})
    cs = FakeCS(ctx)
    ai = FakeAI()
    sm = FakeSM(cs, ai)

    # 手工构造一个 runner 实例（绕过真正 __init__）
    runner = object.__new__(MessengerRpaRunner)
    runner._sm = sm
    runner._cfg = {
        "episodic_memory": {
            "enabled": True, "threshold_rounds": 12,
            "cooldown_rounds": 5, "keep_tail_rounds": 3, "max_chars": 200,
        }
    }

    async def _run():
        runner._dispatch_episodic_summary("chat_xyz")
        # 让 bg task 运行
        await asyncio.sleep(0.2)
    asyncio.run(_run())

    assert ctx.get("_conversation_summary", "").startswith("（摘要占位）"), \
        f"summary 未写: {ctx}"
    assert ctx.get("_last_summary_rounds") == 12
    # tail 裁剪：keep_tail_rounds=3 → 6 条消息
    assert len(ctx.get("_conversation_history", [])) == 6, \
        f"tail 裁剪失败: {len(ctx.get('_conversation_history'))}"
    ok("episodic summary 完整链路（触发+写回+裁剪）")


if __name__ == "__main__":
    test_risk_parse()
    test_state_risk()
    test_metrics()
    test_replay_pack()
    test_episodic_plumbing()
    print(f"\n=== TOTAL: {ok_cnt} OK / {fail_cnt} FAIL ===")
    sys.exit(0 if fail_cnt == 0 else 1)
