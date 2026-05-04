"""P1-E1 守卫 reason metrics 测试。

验证 MessengerRpaMetrics.observe_run 正确累加：
  - step 维度（白名单内）
  - hints 维度（精确匹配 + 前缀匹配）
  - dump() 暴露 guard_skips
  - reset() 清空
  - 维度爆炸防护（白名单外不累加）
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.integrations.messenger_rpa.metrics import (  # noqa: E402
    MessengerRpaMetrics,
)


def _make_result(**kw: Any) -> Dict[str, Any]:
    """补全 observe_run 需要的最小 result 结构。"""
    base = {
        "ok": False,
        "step": "",
        "error": "",
        "total_ms": 100.0,
        "phase_ms": {},
        "hints": [],
    }
    base.update(kw)
    return base


class TestGuardSkipsByStep:
    def test_inbox_self_sent_skip_counted(self):
        m = MessengerRpaMetrics()
        m.observe_run(_make_result(step="inbox_self_sent_skip", ok=True))
        d = m.dump()
        assert d["guard_skips"]["inbox_self_sent_skip"] == 1

    def test_thread_self_skip_hard_gap_counted(self):
        m = MessengerRpaMetrics()
        m.observe_run(_make_result(step="thread_self_skip_hard_gap", ok=True))
        assert m.dump()["guard_skips"]["thread_self_skip_hard_gap"] == 1

    def test_runaway_paused_counted(self):
        m = MessengerRpaMetrics()
        m.observe_run(_make_result(step="runaway_paused", error="x"))
        assert m.dump()["guard_skips"]["runaway_paused"] == 1

    def test_unrelated_step_not_counted(self):
        """白名单外的 step 不累加（防维度爆炸）。"""
        m = MessengerRpaMetrics()
        m.observe_run(_make_result(step="some_random_step_xyz", ok=True))
        assert m.dump()["guard_skips"] == {}

    def test_step_repeated_accumulates(self):
        m = MessengerRpaMetrics()
        for _ in range(5):
            m.observe_run(_make_result(step="reply_cooldown_skip", ok=True))
        assert m.dump()["guard_skips"]["reply_cooldown_skip"] == 5

    def test_multiple_steps_independent(self):
        m = MessengerRpaMetrics()
        m.observe_run(_make_result(step="duplicate_skip", ok=True))
        m.observe_run(_make_result(step="duplicate_skip", ok=True))
        m.observe_run(_make_result(step="sticky_idle", ok=True))
        skips = m.dump()["guard_skips"]
        assert skips["duplicate_skip"] == 2
        assert skips["sticky_idle"] == 1


class TestGuardSkipsByHint:
    def test_exact_hint_counted(self):
        m = MessengerRpaMetrics()
        m.observe_run(_make_result(
            step="ok", ok=True, hints=["thread_xml_bubble_guard:self"],
        ))
        assert m.dump()["guard_skips"]["thread_xml_bubble_guard:self"] == 1

    def test_prefix_hint_normalized(self):
        """前缀匹配的 hint 累加到前缀（去除变量部分），避免维度爆炸。"""
        m = MessengerRpaMetrics()
        m.observe_run(_make_result(
            step="runaway_paused", error="x",
            hints=["runaway_circuit_tripped:count_n=3/300s"],
        ))
        d = m.dump()
        # step 计数 + 前缀化的 hint 计数
        assert d["guard_skips"]["runaway_paused"] == 1
        assert d["guard_skips"]["runaway_circuit_tripped"] == 1

    def test_prefix_hint_with_different_variable_aggregates(self):
        """不同变量后缀的同前缀 hint 应当聚合到同一个 metrics key。"""
        m = MessengerRpaMetrics()
        m.observe_run(_make_result(
            step="runaway_paused", error="x",
            hints=["runaway_circuit_tripped:count_n=3/300s"],
        ))
        m.observe_run(_make_result(
            step="runaway_paused", error="x",
            hints=["runaway_circuit_tripped:sequence_n=2"],
        ))
        d = m.dump()
        # 同前缀 → 聚合 = 2，不是各自独立
        assert d["guard_skips"]["runaway_circuit_tripped"] == 2

    def test_unrelated_hint_not_counted(self):
        m = MessengerRpaMetrics()
        m.observe_run(_make_result(
            step="ok", ok=True, hints=["some_unrelated_hint_xyz"],
        ))
        assert m.dump()["guard_skips"] == {}

    def test_multiple_hints_in_same_run(self):
        m = MessengerRpaMetrics()
        m.observe_run(_make_result(
            step="ok", ok=True,
            hints=[
                "thread_xml_bubble_guard:self",
                "self_media_xml_guard",
                "irrelevant_hint",
            ],
        ))
        d = m.dump()["guard_skips"]
        assert d["thread_xml_bubble_guard:self"] == 1
        assert d["self_media_xml_guard"] == 1
        assert "irrelevant_hint" not in d


class TestGuardSkipsLifecycle:
    def test_dump_includes_guard_skips_key(self):
        """dump() 始终含 guard_skips 键（即使空）。"""
        m = MessengerRpaMetrics()
        d = m.dump()
        assert "guard_skips" in d
        assert d["guard_skips"] == {}

    def test_reset_clears_guard_skips(self):
        m = MessengerRpaMetrics()
        m.observe_run(_make_result(step="duplicate_skip", ok=True))
        assert m.dump()["guard_skips"] == {"duplicate_skip": 1}
        m.reset()
        assert m.dump()["guard_skips"] == {}

    def test_invalid_hints_does_not_crash(self):
        """hints 非 list 或含非 str 元素不能让 observe_run 抛错。"""
        m = MessengerRpaMetrics()
        m.observe_run(_make_result(
            step="duplicate_skip", ok=True, hints=None,
        ))
        m.observe_run(_make_result(
            step="duplicate_skip", ok=True, hints="bad_string_not_list",
        ))
        m.observe_run(_make_result(
            step="duplicate_skip", ok=True, hints=[123, None, "ok_str"],
        ))
        # 不应抛异常；duplicate_skip step 累加到 3
        assert m.dump()["guard_skips"]["duplicate_skip"] == 3


class TestRealLoopReproduction:
    """模拟 03:40 死循环 8 条爆炸现场，验证 metrics 正确反映现状。"""

    def test_eight_self_loops_in_window(self):
        m = MessengerRpaMetrics()
        # 假设 P0-A L1 在每一轮都把死循环拦下
        for _ in range(8):
            m.observe_run(_make_result(
                step="inbox_self_sent_skip", ok=True,
            ))
        # runaway 硬天花板兜底（理论上 P0-A 已拦完，runaway 不会再 trip）
        # 但如果 P0-A 关掉，runaway 在第 10 次硬熔断
        d = m.dump()
        assert d["guard_skips"]["inbox_self_sent_skip"] == 8
        assert "runaway_paused" not in d["guard_skips"]

    def test_p0a_disabled_runaway_takes_over(self):
        """模拟 P0-A 全关 + 真实死循环 → runaway 在第 10 次 trip。"""
        m = MessengerRpaMetrics()
        # 1-9 次正常 sent
        for _ in range(9):
            m.observe_run(_make_result(
                step="sent", ok=True, reply_text="hi",
            ))
        # 第 10 次 runaway hard ceiling trip
        m.observe_run(_make_result(
            step="runaway_paused", error="x",
            hints=["runaway_hard_ceiling:count_n=10/300s"],
        ))
        d = m.dump()
        assert d["guard_skips"]["runaway_paused"] == 1
        assert d["guard_skips"]["runaway_hard_ceiling"] == 1


class TestP16GuardCounters:
    """P16 反空转守卫的 metrics 计数。"""

    def test_skipped_peer_text_short_circuit_counted(self):
        m = MessengerRpaMetrics()
        m.observe_run(_make_result(
            step="self_message_skip", ok=True,
            hints=["skipped_peer_text_short_circuit"],
        ))
        d = m.dump()
        assert d["guard_skips"]["skipped_peer_text_short_circuit"] == 1
        assert d["guard_skips"]["self_message_skip"] == 1

    def test_chat_overlap_skip_cooldown_counted(self):
        m = MessengerRpaMetrics()
        m.observe_run(_make_result(
            step="self_message_skip", ok=True,
            hints=["chat_overlap_skip_cooldown"],
        ))
        assert m.dump()["guard_skips"]["chat_overlap_skip_cooldown"] == 1

    def test_chat_overlap_inbox_skip_counted(self):
        """IL 层：inbox 阶段提前拦截（result['step'] 不为 self_message_skip，
        而是其他常见 step；hint 独立累计）。"""
        m = MessengerRpaMetrics()
        m.observe_run(_make_result(
            step="all_unread_skipped", ok=True,
            hints=["chat_overlap_inbox_skip"],
        ))
        assert m.dump()["guard_skips"]["chat_overlap_inbox_skip"] == 1

    def test_bubble_self_confirms_overlap_counted(self):
        m = MessengerRpaMetrics()
        m.observe_run(_make_result(
            step="self_message_skip", ok=True,
            hints=["bubble_self_confirms_overlap"],
        ))
        assert m.dump()["guard_skips"]["bubble_self_confirms_overlap"] == 1

    def test_chat_overlap_long_cooldown_prefix_counted(self):
        """C 层长冷却 hint 格式：chat_overlap_long_cooldown:600s:streak=3
        前缀去 ':' 作 metrics key。"""
        m = MessengerRpaMetrics()
        m.observe_run(_make_result(
            step="self_message_skip", ok=True,
            hints=["chat_overlap_long_cooldown:600s:streak=3"],
        ))
        # 前缀匹配后 key 应为去掉尾部冒号的形式
        d = m.dump()
        assert d["guard_skips"]["chat_overlap_long_cooldown"] == 1

    def test_p16_combined_burst_counts_independently(self):
        """模拟一次完整 P16 触发链：3 次 self_message_skip 后第 4 次进 inbox 被拦。"""
        m = MessengerRpaMetrics()
        # 1~3 次：thread 内 skip，前 2 次仅 streak，第 3 次触发长冷却
        for _ in range(2):
            m.observe_run(_make_result(
                step="self_message_skip", ok=True,
            ))
        m.observe_run(_make_result(
            step="self_message_skip", ok=True,
            hints=["chat_overlap_long_cooldown:600s:streak=3"],
        ))
        # 第 4 次：inbox 阶段被 IL 拦截
        m.observe_run(_make_result(
            step="all_unread_skipped", ok=True,
            hints=["chat_overlap_inbox_skip"],
        ))
        d = m.dump()
        assert d["guard_skips"]["self_message_skip"] == 3
        assert d["guard_skips"]["chat_overlap_long_cooldown"] == 1
        assert d["guard_skips"]["chat_overlap_inbox_skip"] == 1
