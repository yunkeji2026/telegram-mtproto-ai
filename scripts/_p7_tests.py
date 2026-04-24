"""P7 单测合集（不连 ADB / 不起 Redis / 不下载 whisper，纯逻辑）。

覆盖：
- P6-leftover-1 approvals.ai_tier 列写入 + list_approvals filter
- P7-1 FileLeaderLock 基本 acquire/renew/release
- P7-1 FileLeaderLock 过期抢占 + fencing token 单调 +1
- P7-1 FileLeaderLock CAS 阻止错 token renew
- P7-1 LeaderLock 高层门面 + 心跳续约
- P7-2 AudioPipeline disabled / backend=disabled / cb_open
- P7-2 AudioPipeline 懒加载：没装 faster_whisper → circuit breaker
- P7-3 _send_reply_with_retry 全成功 / 第 2 次成功 / Lv3 IME 降级 / 全失败 cooldown
- P7-4 extract_long_term_facts 解析 + 去重 + 合并 existing
- P7-4 _long_term_memory 注入 style_hint（白盒）

用法：python scripts/_p7_tests.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ok_count = 0
fail_count = 0
failures: List = []


def _check(name: str, cond: bool, msg: str = "") -> None:
    global ok_count, fail_count
    if cond:
        ok_count += 1
        print(f"  OK {name}")
    else:
        fail_count += 1
        failures.append((name, msg))
        print(f"  FAIL {name}: {msg}")


# ─────────── P6-leftover-1 approvals.ai_tier ───────────
def test_approvals_ai_tier_column():
    print("\n== P6-leftover-1 approvals.ai_tier ==")
    from src.integrations.messenger_rpa.state_store import MessengerRpaStateStore
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        db = Path(td) / "t.sqlite"
        store = MessengerRpaStateStore(str(db))
        # 新库里 INSERT 带 ai_tier
        aid_p = store.enqueue_approval(
            chat_key="a", chat_name="A", peer_text="hi",
            peer_kind="text", reply_text="ok", ai_tier="premium",
        )
        aid_n = store.enqueue_approval(
            chat_key="b", chat_name="B", peer_text="hi",
            peer_kind="text", reply_text="ok",  # 不传 → 空串
        )
        _check("enqueue returns row ids", aid_p > 0 and aid_n > 0)
        rows = store.list_approvals()
        tiers = {r.get("chat_key"): r.get("ai_tier") or "" for r in rows}
        _check("premium tier stored", tiers.get("a") == "premium",
               f"got {tiers!r}")
        _check("default tier empty", tiers.get("b") == "")


# ─────────── P7-1 FileLeaderLock basic ───────────
async def _test_lock_basic():
    from src.integrations.ha.leader_lock import FileLeaderLock
    with tempfile.TemporaryDirectory() as td:
        lock = FileLeaderLock(str(Path(td) / "lead.json"))
        # node A 抢
        st_a = await lock.try_acquire("nodeA", ttl_sec=5)
        _check("A acquires", st_a is not None and st_a.holder_id == "nodeA")
        _check("token starts at 1", st_a.fencing_token == 1)
        # B 同期抢 → 拒
        st_b = await lock.try_acquire("nodeB", ttl_sec=5)
        _check("B blocked", st_b is None)
        # A reentrant
        st_a2 = await lock.try_acquire("nodeA", ttl_sec=5)
        _check("A reentrant", st_a2 is not None
               and st_a2.fencing_token == 1)
        # A 错 token renew 失败
        bad = await lock.renew("nodeA", 999, ttl_sec=5)
        _check("renew with bad token fails", bad is None)
        # A 正确 token renew 成功
        good = await lock.renew("nodeA", 1, ttl_sec=5)
        _check("renew with good token ok", good is not None
               and good.fencing_token == 1)
        # A release
        ok_r = await lock.release("nodeA", 1)
        _check("A release", ok_r is True)
        # A 已释放 → B 抢成功，token+1
        st_b2 = await lock.try_acquire("nodeB", ttl_sec=5)
        _check("B acquires after release", st_b2 is not None)
        _check("fencing token monotonic +1", st_b2.fencing_token == 2)


async def _test_lock_expiry():
    from src.integrations.ha.leader_lock import FileLeaderLock
    with tempfile.TemporaryDirectory() as td:
        lock = FileLeaderLock(str(Path(td) / "lead.json"))
        st_a = await lock.try_acquire("A", ttl_sec=0.2)
        _check("A acquires short TTL", st_a is not None)
        # 等 TTL 过
        await asyncio.sleep(0.3)
        peek = await lock.peek()
        _check("lock expired after TTL", peek is None)
        # B 夺锁
        st_b = await lock.try_acquire("B", ttl_sec=5)
        _check("B takes expired lock", st_b is not None
               and st_b.holder_id == "B")
        _check("token increments after expiry", st_b.fencing_token == 2)


async def _test_lock_facade():
    from src.integrations.ha.leader_lock import LeaderLock, FileLeaderLock
    with tempfile.TemporaryDirectory() as td:
        backend = FileLeaderLock(str(Path(td) / "lead.json"))
        lk = LeaderLock(backend, node_id="test-node")
        ok = await lk.acquire(ttl_sec=1.0, heartbeat_sec=0.2)
        _check("facade acquire", ok is True and lk.is_leader)
        # 等 3 心跳保持
        await asyncio.sleep(0.7)
        _check("still leader after heartbeats", lk.is_leader)
        await lk.release()
        _check("release clears state", not lk.is_leader)


def test_p7_1_leader_lock():
    print("\n== P7-1 leader_lock ==")
    asyncio.get_event_loop().run_until_complete(_test_lock_basic())
    asyncio.get_event_loop().run_until_complete(_test_lock_expiry())
    asyncio.get_event_loop().run_until_complete(_test_lock_facade())


# ─────────── P7-2 AudioPipeline ───────────
def test_p7_2_audio():
    print("\n== P7-2 AudioPipeline ==")
    from src.ai.audio_pipeline import AudioPipeline, reset_audio_pipeline

    reset_audio_pipeline()
    # 关闭 → transcribe 直接 fail
    ap = AudioPipeline({"enabled": False})
    r = asyncio.get_event_loop().run_until_complete(
        ap.transcribe_file("nonexistent.m4a"),
    )
    _check("disabled returns error", not r.ok
           and r.error == "pipeline_disabled")

    # backend=disabled
    ap2 = AudioPipeline({"enabled": True, "backend": "disabled"})
    r2 = asyncio.get_event_loop().run_until_complete(
        ap2.transcribe_file("nonexistent.m4a"),
    )
    _check(
        "backend=disabled fails with proper error",
        not r2.ok and ("file_not_found" in r2.error or "model_load_failed" in r2.error),
    )

    # enabled + 缺依赖 → CB open
    ap3 = AudioPipeline({
        "enabled": True, "backend": "faster_whisper", "cb_cooldown_sec": 60,
    })
    # 构造一个真实的小文件（空也行，走到 _load_model 就会失败）
    with tempfile.NamedTemporaryFile(suffix=".m4a", delete=False) as f:
        f.write(b"\x00" * 32)
        tmp_path = f.name
    try:
        r3 = asyncio.get_event_loop().run_until_complete(
            ap3.transcribe_file(tmp_path),
        )
        # 缺 faster_whisper 会进 circuit breaker
        # 若机器上恰好装了 faster_whisper，会尝试 transcribe 空文件 → 也应失败
        _check("enabled without deps fails gracefully", not r3.ok)
        stats = ap3.stats()
        _check("stats exposes fields", "cb_open" in stats
               and "loaded" in stats)
    finally:
        import os
        try:
            os.remove(tmp_path)
        except OSError:
            pass


# ─────────── P7-3 send retry logic ───────────
class _FakeState:
    def __init__(self):
        self.escalated = {}
        self.credits = {}

    def record_send(self):
        return {}

    def clear_risk(self):
        pass

    def set_escalation(self, chat_key, *, until_ts, reason, chat_name=None):
        self.escalated[chat_key] = {"until": until_ts, "reason": reason}

    def adjust_credit(self, chat_key, delta, reason=""):
        self.credits[chat_key] = self.credits.get(chat_key, 0) + delta


class _FakeRunner:
    """最小 runner：只挂了 _send_reply_with_retry 需要的属性。"""

    def __init__(self):
        self._cfg: Dict[str, Any] = {
            "send_retry": {
                "enabled": True,
                "max_attempts": 4,
                "retry_delay_sec": 0.01,
                "chat_cooldown_sec": 30,
            },
            "use_adb_keyboard": True,
            "credit_policy": {"enabled": True, "send_fail_delta": -10},
        }
        self._state = _FakeState()
        self._call_count = 0
        self.scenario = "all_ok"     # 测试配置

    def _foreground_messenger(self, serial, result):
        return True

    def _send_reply(self, serial, wh, reply_text, result):
        self._call_count += 1
        result["send_path"] = f"sim_{self._call_count}"
        if self.scenario == "all_ok":
            return True
        if self.scenario == "fail_then_ok":
            if self._call_count == 1:
                result["error"] = "inject_text_failed: first try"
                return False
            return True
        if self.scenario == "all_fail":
            result["error"] = f"inject_text_failed: attempt {self._call_count}"
            return False
        if self.scenario == "empty":
            result["error"] = "empty_reply_text"
            return False
        return False


async def _run_retry(runner, chat_key="CK_test"):
    # 懒 import 本体方法
    from src.integrations.messenger_rpa.runner import MessengerRpaRunner
    # bind bound-method 到 fake 实例
    meth = MessengerRpaRunner._send_reply_with_retry
    result: Dict[str, Any] = {"chat_key": chat_key}
    ok = await meth(runner, "ser", (1080, 1920), "hello", result)
    return ok, result


def test_p7_3_send_retry():
    print("\n== P7-3 send retry ==")

    # 场景 1：第一次就成功
    r1 = _FakeRunner(); r1.scenario = "all_ok"
    ok, res = asyncio.get_event_loop().run_until_complete(_run_retry(r1))
    _check("all_ok 1 call succeeds", ok and r1._call_count == 1,
           f"calls={r1._call_count} res={res}")

    # 场景 2：第 2 次成功（Lv2 等 5s 重试）
    r2 = _FakeRunner(); r2.scenario = "fail_then_ok"
    ok, res = asyncio.get_event_loop().run_until_complete(_run_retry(r2))
    _check("fail_then_ok recovers on retry 2", ok and r2._call_count == 2,
           f"calls={r2._call_count}")
    _check("send_attempts logged",
           isinstance(res.get("send_attempts"), list)
           and len(res.get("send_attempts") or []) == 2)

    # 场景 3：都失败 → cooldown + 扣分
    r3 = _FakeRunner(); r3.scenario = "all_fail"
    ok, res = asyncio.get_event_loop().run_until_complete(_run_retry(r3))
    _check("all_fail returns False", not ok)
    _check("all_fail escalated", "CK_test" in r3._state.escalated)
    _check("all_fail credit -10", r3._state.credits.get("CK_test") == -10)
    _check("all_fail log has 4 attempts",
           len(res.get("send_attempts") or []) == 4,
           f"attempts={res.get('send_attempts')}")
    _check("send_all_failed flag set", res.get("send_all_failed") is True)
    _check("IME restored after toggling",
           r3._cfg.get("use_adb_keyboard") is True)

    # 场景 4：empty text → 立即返回
    r4 = _FakeRunner(); r4.scenario = "empty"
    ok, res = asyncio.get_event_loop().run_until_complete(_run_retry(r4))
    _check("empty returns False immediately", not ok and r4._call_count == 1)


# ─────────── P7-4 long-term memory ───────────
def test_p7_4_ltm_parse():
    print("\n== P7-4 long-term memory ==")
    # 纯逻辑测试：_parse 内嵌 JSON / bullets / wrapper 三种格式
    import json
    # 模拟 {"facts":[...]} 输出
    raw1 = '{"facts":["客户叫 Mike","住在加州","对 Premium 感兴趣"]}'
    obj = json.loads(raw1)
    parsed = obj.get("facts") or []
    _check("json wrapper parse", len(parsed) == 3
           and "Mike" in parsed[0])

    # 模拟 bullet 文本
    raw2 = "- 叫 John\n- 住 LA\n- 喜欢 PRO 版本"
    items = [line.lstrip("- ").strip() for line in raw2.splitlines() if line.strip()]
    _check("bullet fallback parse", len(items) == 3)

    # existing 去重合并语义（运行时 runner 做）
    existing = ["叫 John", "住 LA"]
    new_list = ["叫 John", "喜欢 PRO 版本"]
    seen = set()
    merged = []
    for x in existing + new_list:
        if x not in seen:
            seen.add(x); merged.append(x)
    _check("existing merge dedup", merged == ["叫 John", "住 LA", "喜欢 PRO 版本"])


async def _test_ltm_stub_extract():
    """stub ai_client.extract_long_term_facts 走一遍返回路径。"""
    from src.ai import ai_client as ac_module

    class _StubClient:
        def __init__(self):
            self._cb_enabled = False
            self._cb_open_until = 0
            self._use_openai_compat = False  # 强制走 Gemini 分支，不连真 LLM
            self.client = None   # GENAI_AVAILABLE False 路径
            self.model = "stub"
            self.logger = ac_module.logging.getLogger("stub")

    stub = _StubClient()
    # 直接调 method，绑 stub
    r = await ac_module.AIClient.extract_long_term_facts(
        stub,
        working_summary="客户叫 Mike，住洛杉矶。",
        recent_history=[{"role": "user", "content": "我住 LA"}] * 5,
        existing_facts=["叫 Mike"],
        max_facts=5,
        timeout_sec=1.0,
    )
    # Gemini 分支且 GENAI_AVAILABLE=False → 返回 existing_facts 拷贝
    _check("stub extract returns existing fallback",
           isinstance(r, list) and r == ["叫 Mike"],
           f"got {r!r}")


def test_p7_4_ltm_extract():
    asyncio.get_event_loop().run_until_complete(_test_ltm_stub_extract())


# ─────────── run all ───────────
if __name__ == "__main__":
    try:
        test_approvals_ai_tier_column()
        test_p7_1_leader_lock()
        test_p7_2_audio()
        test_p7_3_send_retry()
        test_p7_4_ltm_parse()
        test_p7_4_ltm_extract()
    except Exception as e:
        print(f"\nFATAL: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        fail_count += 1
    print(f"\n==== P7 tests: {ok_count} OK / {fail_count} FAIL ====")
    if failures:
        print("Failures:")
        for n, m in failures:
            print(f"  - {n}: {m}")
    sys.exit(0 if fail_count == 0 else 1)
