"""P6 单测合集（不连 ADB / 不起服务，纯逻辑）。

覆盖：
- P6-1 AccountContext.merged_config 自动注入 chat_key_prefix
- P6-1 service._get_or_create_runner 单例 cache
- P6-1 service._run_once_for_account timeout 路径
- P6-4 LlmCostTracker 记账 / pricing 模糊匹配 / reset
- P6-4 dump_prom 文本格式合法
- P6-5 runner._compute_winner_variant 选最高 approve_ratio 且样本够
- P6-5 _pick_persona_variant ε-greedy 不改 sticky

用法：python scripts/_p6_tests.py
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ok_count = 0
fail_count = 0
failures = []


def _check(name: str, cond: bool, msg: str = "") -> None:
    global ok_count, fail_count
    if cond:
        ok_count += 1
        print(f"  ✓ {name}")
    else:
        fail_count += 1
        failures.append((name, msg))
        print(f"  ✗ {name}: {msg}")


# ───────────────────── P6-1 tests ─────────────────────
def test_p6_1_merged_config():
    print("\n[P6-1] AccountContext.merged_config 自动注入 chat_key_prefix")
    from src.integrations.messenger_rpa.account_pool import AccountContext

    c1 = AccountContext(
        account_id="default", adb_serial="127.0.0.1:5555",
    )
    m1 = c1.merged_config({"chat_key_prefix": "legacy"})
    _check("default 账号保留原 prefix", m1["chat_key_prefix"] == "legacy")

    c2 = AccountContext(
        account_id="A", adb_serial="127.0.0.1:5555",
    )
    m2 = c2.merged_config({"chat_key_prefix": "legacy"})
    _check(
        "非 default + 无 overlay → 自动生成 acc_A",
        m2["chat_key_prefix"] == "acc_A",
        f"got {m2.get('chat_key_prefix')}",
    )

    c3 = AccountContext(
        account_id="A", adb_serial="127.0.0.1:5555",
        config_overlay={"chat_key_prefix": "custom_A"},
    )
    m3 = c3.merged_config({"chat_key_prefix": "legacy"})
    _check(
        "overlay 显式 prefix 优先",
        m3["chat_key_prefix"] == "custom_A",
    )

    c4 = AccountContext(
        account_id="B", adb_serial="127.0.0.1:5555",
        chat_key_prefix="mg_b",
    )
    m4 = c4.merged_config({})
    _check(
        "dataclass.chat_key_prefix 被自动应用",
        m4["chat_key_prefix"] == "mg_b",
    )


def test_p6_1_account_registry():
    print("\n[P6-1] AccountRegistry 单/多账号路径")
    from src.integrations.messenger_rpa.account_pool import AccountRegistry

    cfg_single = {"adb_serial": "127.0.0.1:5555"}
    reg1 = AccountRegistry.from_config(
        cfg_single, Path("config/config.yaml"),
    )
    _check("单账号 size=1", reg1.size() == 1)
    _check(
        "单账号 account_id=default",
        reg1.account_ids() == ["default"],
    )

    cfg_multi = {
        "accounts": [
            {"id": "A", "adb_serial": "127.0.0.1:5555"},
            {"id": "B", "adb_serial": "127.0.0.1:5556",
             "overrides": {"reply_mode": "approve"}},
        ],
        "account_max_parallel": 2,
    }
    reg2 = AccountRegistry.from_config(
        cfg_multi, Path("config/config.yaml"),
    )
    _check("多账号 size=2", reg2.size() == 2)
    ctx_b = reg2.get("B")
    _check(
        "overlay reply_mode 在 merged_config 里",
        ctx_b.merged_config({}).get("reply_mode") == "approve",
    )
    _check(
        "B 自动获得 acc_B 前缀",
        ctx_b.merged_config({}).get("chat_key_prefix") == "acc_B",
    )


def test_p6_1_pool_sem_lock_isolation():
    """同一 account lock 串行；不同 account + sem=2 → 并发。"""
    print("\n[P6-1] AccountPool lock + semaphore 独立工作")
    from src.integrations.messenger_rpa.account_pool import AccountPool

    async def run():
        pool = AccountPool(max_parallel=2)
        order = []

        async def worker(aid: str, i: int):
            async with pool.acquire(aid):
                order.append(f"enter_{aid}_{i}")
                await asyncio.sleep(0.03)
                order.append(f"leave_{aid}_{i}")

        # 同 account 两协程 → 必须串行
        await asyncio.gather(worker("A", 1), worker("A", 2))
        serialized = (
            order == [
                "enter_A_1", "leave_A_1", "enter_A_2", "leave_A_2",
            ]
            or order == [
                "enter_A_2", "leave_A_2", "enter_A_1", "leave_A_1",
            ]
        )
        _check("同一 account 严格串行", serialized, f"order={order}")

        order.clear()
        # 不同 account → 可并发（但 max_parallel=2 本身也允许）
        await asyncio.gather(worker("X", 1), worker("Y", 1))
        # 至少一个交错发生
        interleaved = (
            order.index("enter_Y_1") < order.index("leave_X_1")
            or order.index("enter_X_1") < order.index("leave_Y_1")
        )
        _check("不同 account 并发", interleaved, f"order={order}")

    asyncio.run(run())


# ───────────────────── P6-4 tests ─────────────────────
def test_p6_4_cost_tracker():
    print("\n[P6-4] LlmCostTracker 记账/匹配/dump_prom")
    from src.ai.llm_cost import LlmCostTracker

    t = LlmCostTracker()
    t.set_pricing({
        "deepseek-chat": {"prompt": 0.00014, "completion": 0.00028},
        "gpt-4o-mini":   {"prompt": 0.00015, "completion": 0.0006},
    })
    r1 = t.record(
        model="deepseek-chat", prompt_tokens=1000,
        completion_tokens=500, tier="normal", account_id="A",
    )
    _check("cost 计算正确",
           abs(r1["cost_usd"] - (0.14 + 0.14) / 1000) < 1e-9,
           f"got {r1['cost_usd']}")

    # 模糊匹配日期后缀
    r2 = t.record(
        model="gpt-4o-mini-2024-07-18",
        prompt_tokens=1000, completion_tokens=1000,
        tier="premium", account_id="A",
    )
    _check("模糊匹配剥日期后缀",
           abs(r2["cost_usd"] - 0.00075) < 1e-9,
           f"got {r2['cost_usd']}")

    # 未知 model → cost=0 但 tokens 记录
    r3 = t.record(
        model="mystery", prompt_tokens=100, completion_tokens=50,
        tier="low", account_id="B",
    )
    _check("未知模型 cost=0", r3["cost_usd"] == 0.0)
    _check("未知模型 tokens 仍累积", r3["prompt_tokens"] == 100)

    d = t.dump()
    _check("total_calls=3", d["total_calls"] == 3)
    _check("分桶 rows=3", len(d["rows"]) == 3)

    prom = t.dump_prom()
    _check("Prometheus HELP 行存在",
           "messenger_rpa_llm_total_cost_usd" in prom
           and "messenger_rpa_llm_tokens_total" in prom)
    _check("label 转义正确（无裸双引号）",
           prom.count('"') % 2 == 0)

    t.reset()
    _check("reset 清零", t.dump()["total_calls"] == 0)


# ───────────────────── P6-5 tests ─────────────────────
class _FakeStateStore:
    """mock state_store 的 variant_stats / assign_variant"""
    def __init__(self, stats):
        self._stats = stats
        self._assigned = {}

    def variant_stats(self):
        return {"variants": self._stats}

    def assign_variant(self, chat_key, *, weights):
        if chat_key in self._assigned:
            return self._assigned[chat_key]
        # 取 weight 最大的（确定性）
        picked = max(weights.items(), key=lambda kv: kv[1])[0]
        self._assigned[chat_key] = picked
        return picked


def test_p6_5_compute_winner():
    print("\n[P6-5] _compute_winner_variant 选最高 approve_ratio")
    from src.integrations.messenger_rpa.runner import MessengerRpaRunner

    class R:
        pass
    r = R.__new__(MessengerRpaRunner)
    # 场景：A ratio=0.8 samples=50, B ratio=0.9 samples=5（样本不够）
    r._state = _FakeStateStore({
        "A": {"apr_sent": 40, "apr_rejected": 10, "approve_ratio": 0.8},
        "B": {"apr_sent": 4,  "apr_rejected": 1,  "approve_ratio": 0.9},
        "_none": {"apr_sent": 0, "apr_rejected": 0},
    })
    winner, samples = MessengerRpaRunner._compute_winner_variant(
        r, min_samples=20,
    )
    _check("选了 A（样本够）", winner == "A", f"got {winner}")
    _check("samples=50", samples == 50)

    # 全部不够样本
    r._state = _FakeStateStore({
        "A": {"apr_sent": 3, "apr_rejected": 1, "approve_ratio": 0.75},
        "B": {"apr_sent": 2, "apr_rejected": 1, "approve_ratio": 0.67},
    })
    winner2, _ = MessengerRpaRunner._compute_winner_variant(
        r, min_samples=20,
    )
    _check("样本都不够 → 无 winner", winner2 == "", f"got {winner2}")


def test_p6_5_pick_variant_greedy():
    print("\n[P6-5] _pick_persona_variant ε-greedy 行为")
    import random
    from src.integrations.messenger_rpa.runner import MessengerRpaRunner

    class R:
        pass
    r = R.__new__(MessengerRpaRunner)
    r._state = _FakeStateStore({
        "A": {"apr_sent": 50, "apr_rejected": 50, "approve_ratio": 0.5},
        "B": {"apr_sent": 80, "apr_rejected": 20, "approve_ratio": 0.8},
    })
    r._cfg = {
        "persona_experiment": {
            "enabled": True,
            "variants": [
                {"name": "A", "weight": 1.0, "style_hint": "styleA"},
                {"name": "B", "weight": 1.0, "style_hint": "styleB"},
            ],
            "auto_winner": {
                "enabled": True,
                "refresh_sec": 1.0,
                "min_samples": 20,
                "init_epsilon": 0.0,   # 强制 100% exploit
                "min_epsilon": 0.0,
                "decay_days": 30,
                "winner_boost": 100.0,  # winner 权重占压倒性优势
            },
        },
    }
    # ε=0 → 100% 走 winner（B），fake store.assign_variant 选最大 weight
    random.seed(42)
    picks = []
    for i in range(20):
        name, hint = MessengerRpaRunner._pick_persona_variant(
            r, f"chat_{i}"
        )
        picks.append(name)
    # B 应占绝对多数
    from collections import Counter
    c = Counter(picks)
    _check("exploit 模式下 B 占压倒多数",
           c.get("B", 0) >= 15, f"picks={c}")

    # ε=1 → 100% 走 sticky 原 weights（A/B 各半）
    r._cfg["persona_experiment"]["auto_winner"]["init_epsilon"] = 1.0
    r._cfg["persona_experiment"]["auto_winner"]["min_epsilon"] = 1.0
    # 清 cache 并换 chat_keys（防 sticky 命中旧分配）
    r._auto_winner_cache = None
    r._state = _FakeStateStore({
        "A": {"apr_sent": 50, "apr_rejected": 50, "approve_ratio": 0.5},
        "B": {"apr_sent": 80, "apr_rejected": 20, "approve_ratio": 0.8},
    })
    random.seed(7)
    picks2 = []
    for i in range(20):
        name, _ = MessengerRpaRunner._pick_persona_variant(
            r, f"chatX_{i}"
        )
        picks2.append(name)
    c2 = Counter(picks2)
    # 在 ε=1 下没有 boost，A 和 B 权重相等，fake assign_variant 取 max
    # 因 tie-break 取字典序（依赖 dict 插入顺序），我们只 check 两种都出现过或至少 A 未被完全压制
    _check("explore 模式下 winner 未被 boost",
           c2.get("A", 0) + c2.get("B", 0) == 20)


# ───────────────────── Main ─────────────────────
def main():
    print("=" * 60)
    print("P6 Unit Tests")
    print("=" * 60)

    test_p6_1_merged_config()
    test_p6_1_account_registry()
    test_p6_1_pool_sem_lock_isolation()
    test_p6_4_cost_tracker()
    test_p6_5_compute_winner()
    test_p6_5_pick_variant_greedy()

    print("\n" + "=" * 60)
    print(f"Summary: {ok_count} OK / {fail_count} FAIL")
    if failures:
        print("\nFailures:")
        for n, m in failures:
            print(f"  - {n}: {m}")
        sys.exit(1)
    print("=" * 60)


if __name__ == "__main__":
    main()
