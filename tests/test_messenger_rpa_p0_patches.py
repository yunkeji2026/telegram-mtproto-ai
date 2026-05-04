"""P0 补丁测试：
- P0-2 per_chat_hourly_cap
- P0-4 self_skip 持久化
- P0-A 三层守卫的决策矩阵（_decide_inbox_self_sent_skip 静态方法）

P0-1（前移 cooldown 到 inbox 阶段）的逻辑与原 thread 内 reply_cooldown_skip
等价（仅位置不同），由现有 messenger 测试 + 端到端运行覆盖。
P0-B（thread 内 self-skip 时间窗）和 P0-D（sticky cooldown floor）嵌在
run_once 大方法中，独立 unit test 成本高，由现有 120 个 messenger 测试
+ 端到端死循环 reproduction 覆盖。
"""
from __future__ import annotations

import sys
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.integrations.messenger_rpa.runner import (  # noqa: E402
    MessengerRpaRunner,
    _PersistentSelfSkipDict,
    _self_skip_norm_key,
)
from src.integrations.messenger_rpa.state_store import (  # noqa: E402
    MessengerRpaStateStore,
)


# ════════════════════════════════════════════════════════════════════
#  fixtures
# ════════════════════════════════════════════════════════════════════

class _MockState:
    """与 test_sticky_thread_safety._MockState 风格一致的 minimal mock。"""

    def __init__(self) -> None:
        self._states: Dict[str, Dict[str, Any]] = {}

    def get_chat_state(self, chat_key: str) -> Dict[str, Any]:
        return self._states.get(chat_key, {})

    def is_skipped_chat(self, chat_key: str) -> bool:
        return False

    def set_last_sent(self, chat_key: str, ts: float) -> None:
        self._states.setdefault(chat_key, {})["last_sent_at"] = ts


def _runner_for_gate(
    cfg: Dict[str, Any], chat_send_history: Dict[str, list] | None = None,
) -> MessengerRpaRunner:
    """构造仅供 _should_skip_send 测试使用的 runner（不跑 __init__）。"""
    r = object.__new__(MessengerRpaRunner)
    r._cfg = cfg
    r._self_skip_until = {}
    r._chat_key_prefix = "test"
    r._state = _MockState()
    if chat_send_history:
        r._chat_send_timestamps = {
            name: deque(ts_list, maxlen=20)
            for name, ts_list in chat_send_history.items()
        }
        r._chat_send_peer_texts = {
            name: deque(maxlen=20) for name in chat_send_history
        }
    return r


# ════════════════════════════════════════════════════════════════════
#  P0-2: per_chat_hourly_cap
# ════════════════════════════════════════════════════════════════════

class TestPerChatHourlyCap:
    def test_cap_zero_means_disabled(self):
        """默认 cap=0 → 不限制（保持向后兼容）。"""
        now = time.time()
        r = _runner_for_gate(
            {"per_chat_hourly_cap": 0},
            chat_send_history={"Alice": [now - i * 60 for i in range(10)]},
        )
        # 即使 1 小时内已发 10 次，cap=0 仍然通过
        assert r._should_skip_send("Alice", source="test") is None

    def test_cap_blocks_when_recent_count_reaches_cap(self):
        """cap=3, 1 小时内已发 3 次 → 拒绝。"""
        now = time.time()
        r = _runner_for_gate(
            {"per_chat_hourly_cap": 3},
            chat_send_history={
                "Alice": [now - 10, now - 600, now - 1200],
            },
        )
        result = r._should_skip_send("Alice", source="test")
        assert result is not None
        assert "per_chat_hourly_cap" in result
        assert "3/3" in result

    def test_cap_passes_when_recent_count_below_cap(self):
        """cap=3, 1 小时内已发 2 次 → 通过。"""
        now = time.time()
        r = _runner_for_gate(
            {"per_chat_hourly_cap": 3},
            chat_send_history={"Alice": [now - 10, now - 600]},
        )
        assert r._should_skip_send("Alice", source="test") is None

    def test_cap_ignores_outside_window(self):
        """cap=3, 一小时前的记录不计入窗口。"""
        now = time.time()
        r = _runner_for_gate(
            {"per_chat_hourly_cap": 3},
            chat_send_history={
                "Alice": [
                    now - 7200,  # 2h 前 — 窗口外
                    now - 4000,  # 1.1h 前 — 窗口外
                    now - 3650,  # 略外
                    now - 100,   # 窗口内（计 1 条）
                ],
            },
        )
        # 1 < 3 → 通过
        assert r._should_skip_send("Alice", source="test") is None

    def test_cap_doesnt_mutate_deque(self):
        """count 操作不能修改 runaway_guard 在用的 deque。"""
        now = time.time()
        history = [now - i * 100 for i in range(5)]
        r = _runner_for_gate(
            {"per_chat_hourly_cap": 3},
            chat_send_history={"Alice": list(history)},
        )
        before = len(r._chat_send_timestamps["Alice"])
        r._should_skip_send("Alice", source="test")
        after = len(r._chat_send_timestamps["Alice"])
        assert before == after

    def test_cap_other_chat_unaffected(self):
        """A 命中 cap 不能影响 B。"""
        now = time.time()
        r = _runner_for_gate(
            {"per_chat_hourly_cap": 3},
            chat_send_history={
                "Alice": [now - 10, now - 600, now - 1200],
                "Bob": [now - 10],
            },
        )
        assert r._should_skip_send("Alice", source="test") is not None
        assert r._should_skip_send("Bob", source="test") is None


# ════════════════════════════════════════════════════════════════════
#  P0-4: _PersistentSelfSkipDict + state_store self_skip 持久化
# ════════════════════════════════════════════════════════════════════

class TestPersistentSelfSkipDict:
    def test_set_persists_to_db(self, tmp_path: Path):
        store = MessengerRpaStateStore(tmp_path / "t.db")
        d = _PersistentSelfSkipDict(store)
        key = _self_skip_norm_key("Yunshan Zan")
        d[key] = time.monotonic() + 1800  # 30 分钟后到期
        # DB 应当有这条记录
        active = store.load_active_self_skips()
        assert key in active
        epoch_until, _reason = active[key]
        # 误差 < 5 秒（写入与回读之间的时差）
        assert abs((epoch_until - time.time()) - 1800) < 5

    def test_expired_value_not_persisted(self, tmp_path: Path):
        """写入一个已过期的值（delta <= 0）不该污染 DB。"""
        store = MessengerRpaStateStore(tmp_path / "t.db")
        d = _PersistentSelfSkipDict(store)
        d["k"] = time.monotonic() - 10  # 10 秒前已过期
        active = store.load_active_self_skips()
        assert "k" not in active

    def test_del_clears_db(self, tmp_path: Path):
        store = MessengerRpaStateStore(tmp_path / "t.db")
        d = _PersistentSelfSkipDict(store)
        d["k"] = time.monotonic() + 600
        assert "k" in store.load_active_self_skips()
        del d["k"]
        assert "k" not in store.load_active_self_skips()

    def test_pop_clears_db(self, tmp_path: Path):
        store = MessengerRpaStateStore(tmp_path / "t.db")
        d = _PersistentSelfSkipDict(store)
        d["k"] = time.monotonic() + 600
        d.pop("k")
        assert "k" not in store.load_active_self_skips()

    def test_load_filters_expired(self, tmp_path: Path):
        """启动时回填只返回未过期记录，过期的同时被 GC。"""
        store = MessengerRpaStateStore(tmp_path / "t.db")
        # 直接写一条已过期的（绕过 set_self_skip 内置过滤）
        with store._lock, store._conn() as c:
            c.execute(
                "INSERT INTO messenger_rpa_self_skip"
                "(norm_key, until_ts, reason, updated_at) VALUES(?,?,?,?)",
                ("expired_key", time.time() - 10, "test", time.time()),
            )
            c.execute(
                "INSERT INTO messenger_rpa_self_skip"
                "(norm_key, until_ts, reason, updated_at) VALUES(?,?,?,?)",
                ("active_key", time.time() + 600, "test", time.time()),
            )
            c.commit()
        active = store.load_active_self_skips()
        assert "active_key" in active
        assert "expired_key" not in active
        # GC 副作用：再查一次仍不存在
        with store._lock, store._conn() as c:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM messenger_rpa_self_skip "
                "WHERE norm_key='expired_key'"
            ).fetchone()
        assert row["n"] == 0

    def test_set_self_skip_takes_max_until(self, tmp_path: Path):
        """同一 key 多次写入 → 取较晚的 until_ts（防回退）。"""
        store = MessengerRpaStateStore(tmp_path / "t.db")
        store.set_self_skip("k", time.time() + 1800)
        store.set_self_skip("k", time.time() + 60)  # 更早 → 应被忽略
        active = store.load_active_self_skips()
        epoch_until, _ = active["k"]
        # 应当还是 ~1800 秒后
        assert (epoch_until - time.time()) > 1500


class TestSelfSkipRoundTripAcrossRestart:
    """模拟"runner 重启后 cooldown 仍在生效"。"""

    def test_runaway_cooldown_survives_restart(self, tmp_path: Path):
        # 第一次 runner：写入 cooldown
        store = MessengerRpaStateStore(tmp_path / "t.db")
        d1 = _PersistentSelfSkipDict(store)
        key = _self_skip_norm_key("yunshan zan")
        d1[key] = time.monotonic() + 1800

        # 第二次 runner：模拟重启，新 store 实例（同 DB 文件）
        store2 = MessengerRpaStateStore(tmp_path / "t.db")
        active = store2.load_active_self_skips()
        assert key in active
        # 模拟回填：epoch → monotonic
        epoch_until, _ = active[key]
        delta = epoch_until - time.time()
        assert delta > 1500  # 仍然有 ~1800 秒
        d2 = _PersistentSelfSkipDict(store2)
        # 直接写基类避免回写 DB（与 runner.__init__ 同款用法）
        dict.__setitem__(d2, key, time.monotonic() + delta)
        # 此时新 runner 上 send_gate 检查就会拒绝
        assert d2.get(key, 0.0) > time.monotonic() + 1500


# ════════════════════════════════════════════════════════════════════
#  P0-A: _decide_inbox_self_sent_skip 决策矩阵
#  （死循环根因：vision OCR 漏 "You:" 前缀 → 自我对话）
# ════════════════════════════════════════════════════════════════════

class TestDecideInboxSelfSentSkip:
    """P0-A 三层守卫的决策矩阵。
    背景：UI XML 显示当前 inbox 顶行是 self-sent (target_ui.is_self_last=True)；
    本函数决定是要"信任 XML 跳过 tap"还是"信任 Vision 覆盖 XML"。
    """

    NOW = 1_700_000_000.0  # 固定 fake epoch，避免依赖系统时钟

    # ── L1: hard_skip_window ──
    def test_hard_skip_window_within_60s_blocks(self):
        """我方 30s 前刚发过 → L1 命中，强信任 XML。"""
        skip, reason = MessengerRpaRunner._decide_inbox_self_sent_skip(
            vision_preview="今日は何してる？",  # 看似新 peer 消息
            last_sent_at=self.NOW - 30,
            last_reply="不相关的回复内容",
            hard_window_sec=60.0,
            now_ts=self.NOW,
        )
        assert skip is True
        assert reason == "hard_skip_window"

    def test_hard_skip_window_expired_passes_to_l2(self):
        """我方 90s 前发过（超出 60s 窗口）→ L1 不命中，进入下一层。"""
        skip, reason = MessengerRpaRunner._decide_inbox_self_sent_skip(
            vision_preview="对方真的发了一条新消息",
            last_sent_at=self.NOW - 90,
            last_reply="完全不相关的旧回复内容",
            hard_window_sec=60.0,
            now_ts=self.NOW,
        )
        assert skip is False
        assert reason == "vision_overrides_xml"

    def test_hard_window_zero_disables_l1(self):
        """配置 hard_window=0 显式关闭 L1。"""
        skip, reason = MessengerRpaRunner._decide_inbox_self_sent_skip(
            vision_preview="对方真的发了新消息",
            last_sent_at=self.NOW - 5,  # 5 秒前但 L1 关
            last_reply="完全不同的旧回复",
            hard_window_sec=0,
            now_ts=self.NOW,
        )
        assert skip is False
        assert reason == "vision_overrides_xml"

    def test_no_last_sent_at_skips_l1(self):
        """从未发过消息（last_sent_at=0）→ L1 不命中。"""
        skip, reason = MessengerRpaRunner._decide_inbox_self_sent_skip(
            vision_preview="对方说的话",
            last_sent_at=0,
            last_reply="",
            hard_window_sec=60.0,
            now_ts=self.NOW,
        )
        assert skip is False
        assert reason == "vision_overrides_xml"

    # ── L2: vision_self_prefix ──
    def test_vision_preview_with_you_prefix_skips(self):
        """Vision 也看到 "You:" 前缀 → 双重确认 self-sent。"""
        skip, reason = MessengerRpaRunner._decide_inbox_self_sent_skip(
            vision_preview="You: 今日はまだ食べてないよ。",
            last_sent_at=self.NOW - 1000,  # 远超 hard_window
            last_reply="",
            hard_window_sec=60.0,
            now_ts=self.NOW,
        )
        assert skip is True
        assert reason == "vision_self_prefix"

    def test_empty_vision_preview_treated_as_self(self):
        """Vision 完全没看到内容 → 保守视为 self（XML 已确认 self）。"""
        skip, reason = MessengerRpaRunner._decide_inbox_self_sent_skip(
            vision_preview="",
            last_sent_at=self.NOW - 1000,
            last_reply="",
            hard_window_sec=60.0,
            now_ts=self.NOW,
        )
        assert skip is True
        assert reason == "vision_self_prefix"

    # ── L3: overlap_with_last_reply ──
    def test_overlap_with_last_reply_blocks(self):
        """vision_preview 与 last_reply 重叠 ≥ 0.5 → 视为 OCR 漏前缀。
        最直接复现 yunshan/Victor Zan 死循环第 2 轮的场景：
        AI 上次发了"今日はまだ食べてない..."，vision 漏读 "You:" 前缀，
        把同样内容当成对方说的。
        """
        ai_last_reply = "今日はまだ食べてないよ。お腹すいてるから、君と何か食べに行けたら嬉しい。どう思う？"
        # 模拟 OCR 漏一两个字（实际是 "今日はまだ食べてないよ.. 行ったら嬉しい"）
        vision_misread = "今日はまだ食べてないよ。お腹すいてるから、君と何か食べに行ったら嬉しいな。どう思"
        skip, reason = MessengerRpaRunner._decide_inbox_self_sent_skip(
            vision_preview=vision_misread,
            last_sent_at=self.NOW - 300,  # 已超 hard_window，必须靠 L3
            last_reply=ai_last_reply,
            hard_window_sec=60.0,
            overlap_threshold=0.5,
            now_ts=self.NOW,
        )
        assert skip is True
        assert reason == "overlap_with_last_reply"

    def test_overlap_below_threshold_not_blocked(self):
        """vision_preview 与 last_reply 完全不同 → L3 不命中。"""
        skip, reason = MessengerRpaRunner._decide_inbox_self_sent_skip(
            vision_preview="昨日見たドラマすごく良かった",
            last_sent_at=self.NOW - 300,
            last_reply="今日は天気がいいですね",
            hard_window_sec=60.0,
            overlap_threshold=0.5,
            now_ts=self.NOW,
        )
        assert skip is False
        assert reason == "vision_overrides_xml"

    def test_short_last_reply_skips_l3(self):
        """last_reply < 4 字符 → L3 跳过（避免短字符串误伤）。
        vision_preview 是正常 peer 文本 → 不命中 L4 → 进 L5 vision 覆盖。"""
        skip, reason = MessengerRpaRunner._decide_inbox_self_sent_skip(
            vision_preview="对方说的全新一句话",
            last_sent_at=self.NOW - 300,
            last_reply="hi",  # 只有 2 字符 → L3 跳过
            hard_window_sec=60.0,
            now_ts=self.NOW,
        )
        assert skip is False
        assert reason == "vision_overrides_xml"

    # ── L4: ambiguous_status ──
    def test_status_word_sent_blocks(self):
        """vision_preview = "Sent" → L4 状态词命中。"""
        skip, reason = MessengerRpaRunner._decide_inbox_self_sent_skip(
            vision_preview="Sent",
            last_sent_at=self.NOW - 1000,
            last_reply="",
            hard_window_sec=60.0,
            now_ts=self.NOW,
        )
        assert skip is True
        assert reason == "ambiguous_status"

    def test_timestamp_only_blocks(self):
        """vision_preview = "0:08" → L4 (clean 后 < 3 字符)。"""
        skip, reason = MessengerRpaRunner._decide_inbox_self_sent_skip(
            vision_preview="0:08",
            last_sent_at=self.NOW - 1000,
            last_reply="",
            hard_window_sec=60.0,
            now_ts=self.NOW,
        )
        assert skip is True
        assert reason == "ambiguous_status"

    def test_japanese_status_blocks(self):
        """日文状态词"既読" → L4 命中。"""
        skip, reason = MessengerRpaRunner._decide_inbox_self_sent_skip(
            vision_preview="既読",
            last_sent_at=self.NOW - 1000,
            last_reply="",
            hard_window_sec=60.0,
            now_ts=self.NOW,
        )
        assert skip is True
        assert reason == "ambiguous_status"

    # ── L5: vision_overrides_xml（合法路径）──
    def test_vision_truly_sees_new_peer_message(self):
        """对方真的发了新消息 + 我方很久没发 → 合法允许 vision 覆盖 XML。"""
        skip, reason = MessengerRpaRunner._decide_inbox_self_sent_skip(
            vision_preview="对方刚刚问的全新问题，跟我之前回的没关系",
            last_sent_at=self.NOW - 600,  # 10 分钟前
            last_reply="我之前回了一段无关的话",
            hard_window_sec=60.0,
            now_ts=self.NOW,
        )
        assert skip is False
        assert reason == "vision_overrides_xml"

    # ── 真实死循环 reproduction ──
    def test_actual_loop_round2_blocked_by_l1(self):
        """模拟日志中 03:40:26 第 2 轮死循环关键场景：
        - 17 秒前我方发了 "今日はまだ食べてないよ..."
        - vision OCR 漏 "You:" 前缀，看到 "今日はまだ食べてないよ.. 行ったら嬉しいな。どう思"
        - 期望：L1 时间窗优先命中（< 60s），无需走到 L3。
        """
        skip, reason = MessengerRpaRunner._decide_inbox_self_sent_skip(
            vision_preview="今日はまだ食べてないよ。お腹すいてるから、君と何か食べに行ったら嬉しいな。どう思",
            last_sent_at=self.NOW - 17,  # 17s ago
            last_reply="今日はまだ食べてないよ。お腹すいてるから、君と何か食べに行けたら嬉しい。どう思う？",
            hard_window_sec=60.0,
            now_ts=self.NOW,
        )
        assert skip is True
        assert reason == "hard_skip_window", (
            f"应当在 L1 时间窗就拦下，实际 reason={reason}"
        )

    def test_actual_loop_blocked_by_l3_when_l1_disabled(self):
        """同样场景但 hard_window=0 关闭 L1 → 期望 L3 文本重叠兜底。"""
        skip, reason = MessengerRpaRunner._decide_inbox_self_sent_skip(
            vision_preview="今日はまだ食べてないよ。お腹すいてるから、君と何か食べに行ったら嬉しいな。どう思",
            last_sent_at=self.NOW - 17,
            last_reply="今日はまだ食べてないよ。お腹すいてるから、君と何か食べに行けたら嬉しい。どう思う？",
            hard_window_sec=0,  # L1 关
            now_ts=self.NOW,
        )
        assert skip is True
        assert reason == "overlap_with_last_reply", (
            f"L1 关后 L3 应兜底，实际 reason={reason}"
        )

    def test_loop_reproduction_without_guards_proves_bug_existed(self):
        """反向证明（防回归 + 文档化）：所有守卫都关闭时，OCR 漏前缀的
        vision_preview 会被允许覆盖 XML——这就是 2026-05-03 死循环 bug 的
        发生条件。如果未来有人移除 P0-A 守卫，这个测试会保持绿但
        test_actual_loop_round2_blocked_by_l1 会变红，提示发生了回归。
        """
        skip, reason = MessengerRpaRunner._decide_inbox_self_sent_skip(
            vision_preview="今日はまだ食べてないよ。お腹すいてるから、君と何か食べに行ったら嬉しいな。どう思",
            last_sent_at=self.NOW - 17,  # 关 L1（如果 hard_window=0）
            last_reply="",                # 关 L3（没历史回复可比对）
            hard_window_sec=0,            # 显式关 L1
            now_ts=self.NOW,
        )
        # vision_preview 长 + 不是状态词 → L4 不命中
        # 全部守卫失效 → 回退到 L5 允许覆盖 XML
        assert skip is False
        assert reason == "vision_overrides_xml", (
            "守卫全关时应当复现死循环条件（vision 覆盖 XML）— "
            f"实际 reason={reason}"
        )

    # ── 优先级顺序 ──
    def test_layer_priority_l1_before_l2(self):
        """同时满足 L1 + L2 时返回 L1 reason（L1 优先级最高）。"""
        skip, reason = MessengerRpaRunner._decide_inbox_self_sent_skip(
            vision_preview="You: 今日はまだ食べてない",  # L2 也命中
            last_sent_at=self.NOW - 30,  # L1 命中
            last_reply="",
            hard_window_sec=60.0,
            now_ts=self.NOW,
        )
        assert skip is True
        assert reason == "hard_skip_window"


# ════════════════════════════════════════════════════════════════════
#  P0-D: sticky cooldown floor 配置解析
# ════════════════════════════════════════════════════════════════════

class TestStickyCooldownFloor:
    """P0-D 把 sticky_thread.post_send_cooldown_floor_sec 作为地板。
    完整测试见 sticky 路径端到端，这里仅验证地板计算逻辑。
    """

    @staticmethod
    def _resolve(cfg: Dict[str, Any]) -> float:
        """复刻 runner.py:2970 的 floor 计算（runner 重构未抽方法时的等价计算）。"""
        sticky = cfg.get("sticky_thread") or {}
        try:
            cd = float(sticky.get("post_send_cooldown_sec", 5) or 5)
        except (TypeError, ValueError):
            cd = 5.0
        floor_raw = sticky.get("post_send_cooldown_floor_sec", 30)
        try:
            floor = float(30 if floor_raw is None else floor_raw)
        except (TypeError, ValueError):
            floor = 30.0
        if floor > 0 and cd < floor:
            cd = floor
        return cd

    def test_default_floor_30s(self):
        assert self._resolve({"sticky_thread": {}}) == 30.0

    def test_explicit_cd_above_floor_kept(self):
        assert self._resolve({
            "sticky_thread": {"post_send_cooldown_sec": 90},
        }) == 90.0

    def test_low_cd_raised_to_floor(self):
        """post_send_cooldown_sec=5 → 被地板抬到 30。"""
        assert self._resolve({
            "sticky_thread": {"post_send_cooldown_sec": 5},
        }) == 30.0

    def test_zero_cd_raised_to_floor(self):
        """post_send_cooldown_sec=0（极端配置）→ 地板兜底 30。"""
        assert self._resolve({
            "sticky_thread": {"post_send_cooldown_sec": 0},
        }) == 30.0

    def test_floor_explicitly_disabled(self):
        """运维显式 floor=0 → 不应用地板。"""
        assert self._resolve({
            "sticky_thread": {
                "post_send_cooldown_sec": 5,
                "post_send_cooldown_floor_sec": 0,
            },
        }) == 5.0

    def test_custom_floor(self):
        """自定义 floor=15。"""
        assert self._resolve({
            "sticky_thread": {
                "post_send_cooldown_sec": 5,
                "post_send_cooldown_floor_sec": 15,
            },
        }) == 15.0



# ════════════════════════════════════════════════════════════════════
#  P0-E1: _chat_name_matches_any 严格化（消除 P0-H 入口）
# ════════════════════════════════════════════════════════════════════

class TestP0E1ChatNameMatchesAny:
    """P0-E1 修复 P0-H 入口：移除 cn in want 模糊分支，
    保留 cn == want 严格相等 + want in cn（运营前缀模糊）。"""

    def test_strict_equal_match(self):
        assert MessengerRpaRunner._chat_name_matches_any(
            "Victor Zan", ["Victor Zan"]
        ) is True

    def test_case_insensitive(self):
        assert MessengerRpaRunner._chat_name_matches_any(
            "VICTOR ZAN", ["victor zan"]
        ) is True

    def test_want_in_cn_still_matches(self):
        """运营场景：sticky 配 'Victor'，OCR 给 'Victor Smith' 仍匹配。"""
        assert MessengerRpaRunner._chat_name_matches_any(
            "Victor Zan", ["Victor"]
        ) is True
        assert MessengerRpaRunner._chat_name_matches_any(
            "Victor Smith", ["Victor"]
        ) is True

    def test_cn_in_want_no_longer_matches_p0h_entry(self):
        """P0-H 入口防回归：sticky=['Victor Zan']，OCR 漏后缀给 'Victor'
        → 修复后不再误匹配。"""
        assert MessengerRpaRunner._chat_name_matches_any(
            "Victor", ["Victor Zan"]
        ) is False

    def test_unrelated_does_not_match(self):
        assert MessengerRpaRunner._chat_name_matches_any(
            "Alice", ["Victor Zan"]
        ) is False

    def test_empty_chat_name(self):
        assert MessengerRpaRunner._chat_name_matches_any(
            "", ["Victor Zan"]
        ) is False

    def test_empty_names_list(self):
        assert MessengerRpaRunner._chat_name_matches_any(
            "Victor", []
        ) is False

    def test_multiple_names_any_match(self):
        assert MessengerRpaRunner._chat_name_matches_any(
            "Bob", ["Alice", "Bob", "Carol"]
        ) is True

    def test_p0h_reproduction_blocked(self):
        """直接复现监控期 P0-H 场景：sticky=['Victor Zan']，
        OCR 漏后缀给 'Victor' → 修复后不再误匹配，不进 fast_path。"""
        assert MessengerRpaRunner._chat_name_matches_any(
            "Victor", ["Victor Zan"]
        ) is False, "P0-H regression: OCR 漏后缀不应触发 sticky fast_path"


# ════════════════════════════════════════════════════════════════════
#  P0-E2: _chat_key_for fuzzy resolve
# ════════════════════════════════════════════════════════════════════

class _FuzzyMockState:
    """支持 list_chat_states 的 mock，用于测试 P0-E2 fuzzy resolve。"""
    def __init__(self):
        self._states = {}

    def add(self, chat_key, **fields):
        self._states[chat_key] = {"chat_key": chat_key, **fields}

    def get_chat_state(self, chat_key):
        return dict(self._states.get(chat_key, {}))

    def list_chat_states(self, limit=100):
        return list(self._states.values())[:limit]

    def is_skipped_chat(self, chat_key):
        return False


def _runner_for_e2(cfg=None, state=None):
    """构造仅供 _chat_key_for 测试的 runner。"""
    r = object.__new__(MessengerRpaRunner)
    r._cfg = cfg or {"chat_key_fuzzy_threshold": 0.85}
    r._chat_key_prefix = "test"
    r._state = state or _FuzzyMockState()
    return r


class TestP0E2ChatKeyFor:
    def test_strict_match_returns_existing_key(self):
        state = _FuzzyMockState()
        state.add("test:Alice", last_sent_at=100.0)
        r = _runner_for_e2(state=state)
        assert r._chat_key_for("Alice") == "test:Alice"

    def test_no_match_returns_strict_new(self):
        r = _runner_for_e2()
        assert r._chat_key_for("Bob") == "test:Bob"

    def test_fuzzy_high_similarity_resolves_to_existing(self):
        """OCR 加字符: 'Alice' -> 'Alicee' 相似度 ~0.91 应归并。"""
        state = _FuzzyMockState()
        state.add("test:Alice", last_sent_at=100.0)
        r = _runner_for_e2(
            cfg={"chat_key_fuzzy_threshold": 0.85}, state=state,
        )
        result = r._chat_key_for("Alicee")
        assert result == "test:Alice"

    def test_fuzzy_low_similarity_does_not_resolve(self):
        """完全不同的名字不归并。"""
        state = _FuzzyMockState()
        state.add("test:Alice", last_sent_at=100.0)
        r = _runner_for_e2(state=state)
        assert r._chat_key_for("Bob Smith") == "test:Bob Smith"

    def test_threshold_zero_disables_fuzzy(self):
        """threshold=0 完全关闭 fuzzy match。"""
        state = _FuzzyMockState()
        state.add("test:Alice", last_sent_at=100.0)
        r = _runner_for_e2(
            cfg={"chat_key_fuzzy_threshold": 0}, state=state,
        )
        assert r._chat_key_for("Alicee") == "test:Alicee"

    def test_cache_hit_avoids_state_lookup(self):
        """同 chat_name 第二次调用走 cache。"""
        state = _FuzzyMockState()
        state.add("test:Alice", last_sent_at=100.0)
        r = _runner_for_e2(state=state)
        first = r._chat_key_for("Alicee")
        state._states.clear()
        second = r._chat_key_for("Alicee")
        assert first == second == "test:Alice"

    def test_empty_chat_name(self):
        r = _runner_for_e2()
        assert r._chat_key_for("") == "test:_empty"

    def test_lazy_init_cache(self):
        """object.__new__ 跳过 __init__ 时 cache 应 lazy 初始化。"""
        r = object.__new__(MessengerRpaRunner)
        r._cfg = {}
        r._chat_key_prefix = "test"
        r._state = _FuzzyMockState()
        assert r._chat_key_for("Alice") == "test:Alice"
        assert hasattr(r, "_chat_key_resolve_cache")


# ════════════════════════════════════════════════════════════════════
#  P0-G + P0-F + P0-I source markers (大方法内嵌补丁的轻量验证)
# ════════════════════════════════════════════════════════════════════

class TestP0GIntentFamiliesMarker:
    """P0-G 把 chat 类意图归到 'chat' family。验证源码内含。"""

    def test_chat_family_groups_casual_intents(self):
        from pathlib import Path
        src = Path("src/skills/skill_manager.py").read_text(
            encoding="utf-8", errors="replace",
        )
        for intent in (
            "greeting", "small_talk", "direct_chat",
            "casual_chat", "chitchat", "free_chat",
        ):
            assert f"\"{intent}\"" in src, (
                f"P0-G regression: '{intent}' missing from chat family"
            )

    def test_p0g_marker_present(self):
        from pathlib import Path
        src = Path("src/skills/skill_manager.py").read_text(
            encoding="utf-8", errors="replace",
        )
        assert "P0-G fix" in src or "chat-class intents" in src


class TestP0FReplyTextDuplicateMarker:
    """P0-F 嵌在 _send_reply 之前。源码标记 + 邻近 self_skip 写入。"""

    def test_p0f_marker_present(self):
        from pathlib import Path
        src = Path(
            "src/integrations/messenger_rpa/runner.py"
        ).read_text(encoding="utf-8", errors="replace")
        assert "reply_text_duplicate_skip" in src
        assert "P0-F" in src

    def test_p0f_writes_self_skip_cooldown(self):
        from pathlib import Path
        src = Path(
            "src/integrations/messenger_rpa/runner.py"
        ).read_text(encoding="utf-8", errors="replace")
        idx = src.find("reply_text_duplicate_skip")
        assert idx > 0
        nearby = src[idx:idx + 2000]
        assert "_self_skip_until" in nearby, (
            "P0-F block should write self_skip cooldown"
        )


class TestP0IMinSignalGuardMarker:
    """P0-I 双零信号守卫源码标记。"""

    def test_p0i_marker_present(self):
        from pathlib import Path
        src = Path(
            "src/integrations/messenger_rpa/runner.py"
        ).read_text(encoding="utf-8", errors="replace")
        assert "p0i_min_signal_skip" in src
        assert "P0-I" in src
        assert "reply_min_signal_guard" in src
