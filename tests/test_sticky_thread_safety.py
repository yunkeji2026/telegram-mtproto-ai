"""P3-B 粘性会话安全网测试：覆盖 P2-A hash diff + P3-A runaway 熔断器。

防回归目标：
  P2-A：发送后必须更新 sticky hash baseline，避免自方气泡触发 hash diff
        "changed" 误识 → 死循环疯狂回复（曾经发生过的事故）。
  P3-A：同一 chat 短窗口内 sent 次数过多 → 自动熔断（最后底线防御）。

不依赖真机/Vision/LLM；纯单元 + 行为级测试。
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.integrations.messenger_rpa.runner import MessengerRpaRunner


def _runner(cfg: Dict[str, Any]) -> MessengerRpaRunner:
    """object.__new__ 跳过 __init__，手工注入最小依赖。"""
    r = object.__new__(MessengerRpaRunner)
    r._cfg = cfg
    r._self_skip_until = {}
    return r


def _make_test_png(path: Path, color: tuple = (200, 200, 200), w: int = 720, h: int = 1600) -> None:
    """生成一张纯色测试 PNG（用于 hash 对比测试）。"""
    from PIL import Image
    im = Image.new("RGB", (w, h), color)
    im.save(str(path))


# ════════════════════════════════════════════════════════════════════
#  P2-A: hash diff 行为
# ════════════════════════════════════════════════════════════════════

class TestStickyHashDiff:
    def test_first_call_returns_changed_true(self, tmp_path):
        r = _runner({"sticky_thread": {"hash_diff_enabled": True}})
        png = tmp_path / "f1.png"
        _make_test_png(png)
        result: Dict[str, Any] = {}
        changed = r._check_sticky_thread_changed(str(png), "Alice", result)
        assert changed is True, "首次调用必须返回 True（无 baseline 时按变化处理）"
        assert "sticky_hash" in result

    def test_same_image_second_call_returns_idle(self, tmp_path):
        r = _runner({"sticky_thread": {"hash_diff_enabled": True}})
        png = tmp_path / "same.png"
        _make_test_png(png)
        # 第 1 次（建立 baseline）
        r._check_sticky_thread_changed(str(png), "Alice", {})
        # 第 2 次（相同图）— 必须 idle
        result2: Dict[str, Any] = {}
        changed = r._check_sticky_thread_changed(str(png), "Alice", result2)
        assert changed is False
        assert result2.get("sticky_idle_count") == 1

    def test_different_image_returns_changed(self, tmp_path):
        r = _runner({"sticky_thread": {"hash_diff_enabled": True}})
        png1 = tmp_path / "f1.png"
        png2 = tmp_path / "f2.png"
        _make_test_png(png1, color=(100, 100, 100))
        _make_test_png(png2, color=(200, 50, 50))
        # 第 1 次建立 baseline
        r._check_sticky_thread_changed(str(png1), "Alice", {})
        # 第 2 次不同图 → 必须 changed
        result2: Dict[str, Any] = {}
        changed = r._check_sticky_thread_changed(str(png2), "Alice", result2)
        assert changed is True
        assert "sticky_thread_changed" in result2.get("hints", [])

    def test_full_check_after_n_idle_forces_changed(self, tmp_path):
        """关键防回归：连续 N 次 idle 后必须强制走 vision，防 hash 假阴性。"""
        r = _runner({
            "sticky_thread": {
                "hash_diff_enabled": True,
                "full_check_after_n_idle": 3,  # 3 次后强制 vision
            },
        })
        png = tmp_path / "f.png"
        _make_test_png(png)
        # 建立 baseline
        r._check_sticky_thread_changed(str(png), "Alice", {})
        # 第 2、3 次 idle
        result2: Dict[str, Any] = {}
        result3: Dict[str, Any] = {}
        assert r._check_sticky_thread_changed(str(png), "Alice", result2) is False
        assert r._check_sticky_thread_changed(str(png), "Alice", result3) is False
        # 第 4 次必须强制 changed
        result4: Dict[str, Any] = {}
        changed4 = r._check_sticky_thread_changed(str(png), "Alice", result4)
        assert changed4 is True, "连续 N 次 idle 后必须强制走 vision"
        any_force_hint = any(
            "sticky_force_full" in h for h in (result4.get("hints") or [])
        )
        assert any_force_hint

    def test_disabled_returns_changed_default(self, tmp_path):
        """hash_diff_enabled=False 时永远返回 True（不影响原流程）。"""
        r = _runner({"sticky_thread": {"hash_diff_enabled": False}})
        png = tmp_path / "f.png"
        _make_test_png(png)
        # 即使第 2 次相同图也应 True
        r._check_sticky_thread_changed(str(png), "Alice", {})
        result2: Dict[str, Any] = {}
        assert r._check_sticky_thread_changed(str(png), "Alice", result2) is True

    def test_baseline_update_avoids_runaway(self, tmp_path):
        """**关键防回归**：模拟 send → 重新 screencap → baseline 更新链路。
        曾发生事故：发送后没更新 baseline → 自方气泡让 hash 变 → 误识 peer
        新消息 → 又发 → 死循环刷屏客户。
        """
        r = _runner({"sticky_thread": {"hash_diff_enabled": True}})
        # T0: 进 thread 看到 png_a
        png_a = tmp_path / "before_send.png"
        _make_test_png(png_a, color=(100, 100, 100))
        r._check_sticky_thread_changed(str(png_a), "Alice", {})
        # T1: main.py 发了一条回复 → thread 截图变了（多了自方气泡）
        png_b = tmp_path / "after_send.png"
        _make_test_png(png_b, color=(150, 150, 150))
        # 模拟 P2-A bugfix：sticky_thread_kept 后强制更新 baseline 到 png_b
        baseline_result: Dict[str, Any] = {}
        r._check_sticky_thread_changed(str(png_b), "Alice", baseline_result)
        # T2: 下一轮 hash diff 仍以 png_b 看（peer 没新消息）→ 必须 idle
        result_t2: Dict[str, Any] = {}
        changed = r._check_sticky_thread_changed(str(png_b), "Alice", result_t2)
        assert changed is False, (
            "防回归：发送后 baseline 已更新，下轮 hash diff 必须识别为 idle"
            " 不再触发回复（避免疯狂连发刷屏客户）"
        )


# ════════════════════════════════════════════════════════════════════
#  P3-A: 疯狂回复熔断器
# ════════════════════════════════════════════════════════════════════

class TestRunawayCircuit:
    def test_normal_send_no_trip(self):
        """正常发送（< max）不触发熔断。"""
        r = _runner({
            "runaway_guard": {
                "enabled": True,
                "window_sec": 300,
                "max_sends_per_window": 3,
                "cooldown_sec": 1800,
            },
        })
        r._record_chat_send("Alice")
        result: Dict[str, Any] = {}
        tripped = r._check_runaway_circuit("Alice", result)
        assert tripped is False

    def test_burst_trips_circuit(self):
        """短窗口内连续 sent ≥ max → 触发熔断。"""
        r = _runner({
            "runaway_guard": {
                "enabled": True,
                "window_sec": 300,
                "max_sends_per_window": 3,
                "cooldown_sec": 1800,
            },
        })
        # 模拟 3 次连续 send
        for _ in range(3):
            r._record_chat_send("Alice")
        result: Dict[str, Any] = {}
        tripped = r._check_runaway_circuit("Alice", result)
        assert tripped is True
        # cooldown 必须被设置
        from src.integrations.messenger_rpa.runner import _self_skip_norm_key
        norm_key = _self_skip_norm_key("Alice")
        assert norm_key in r._self_skip_until
        assert any(
            "runaway_circuit_tripped" in h
            for h in (result.get("hints") or [])
        )

    def test_old_records_pruned_by_window(self):
        """超出 window 的旧记录会被清理 → 不再触发熔断。"""
        r = _runner({
            "runaway_guard": {
                "enabled": True,
                "window_sec": 1,           # 1 秒窗口
                "max_sends_per_window": 3,
                "cooldown_sec": 60,
            },
        })
        # 2 次旧 record
        r._record_chat_send("Alice")
        r._record_chat_send("Alice")
        # 等 > 1 秒让旧记录过期
        time.sleep(1.2)
        # 再 record 1 次（窗口内只有这 1 次）
        r._record_chat_send("Alice")
        result: Dict[str, Any] = {}
        tripped = r._check_runaway_circuit("Alice", result)
        assert tripped is False, "窗口外的旧记录应该被清理，不应触发熔断"

    def test_disabled_soft_gate_passes_below_hard_ceiling(self):
        """P0-C 语义变化：enabled=false 关闭软门，但保留硬天花板（默认 10）。
        软门下（< 10 次）不 trip。"""
        r = _runner({"runaway_guard": {"enabled": False}})
        for _ in range(5):
            r._record_chat_send("Alice")
        result: Dict[str, Any] = {}
        assert r._check_runaway_circuit("Alice", result) is False

    def test_disabled_still_trips_at_hard_ceiling(self):
        """P0-C 终极底线：即使 enabled=false，到 hard_ceiling 仍熔断。"""
        r = _runner({"runaway_guard": {"enabled": False}})
        for _ in range(10):
            r._record_chat_send("Alice")
        result: Dict[str, Any] = {}
        assert r._check_runaway_circuit("Alice", result) is True
        # cooldown 仍写入 _self_skip_until
        from src.integrations.messenger_rpa.runner import _self_skip_norm_key
        assert r._self_skip_until.get(_self_skip_norm_key("Alice"), 0) > 0

    def test_hard_ceiling_can_be_explicitly_disabled(self):
        """运维仍可通过 hard_ceiling_sends=0 显式关闭硬天花板。"""
        r = _runner({
            "runaway_guard": {"enabled": False, "hard_ceiling_sends": 0},
        })
        for _ in range(20):
            r._record_chat_send("Alice")
        result: Dict[str, Any] = {}
        assert r._check_runaway_circuit("Alice", result) is False

    def test_per_chat_isolation(self):
        """不同 chat 的窗口独立，A 触发不影响 B。"""
        r = _runner({
            "runaway_guard": {
                "enabled": True,
                "window_sec": 300,
                "max_sends_per_window": 3,
                "cooldown_sec": 60,
            },
        })
        for _ in range(3):
            r._record_chat_send("Alice")
        # Alice 触发
        assert r._check_runaway_circuit("Alice", {}) is True
        # Bob 没有任何记录 → 不应触发
        assert r._check_runaway_circuit("Bob", {}) is False


# ════════════════════════════════════════════════════════════════════
#  P3-X: 统一 send gate（架构重构后的核心防御）
# ════════════════════════════════════════════════════════════════════

class _MockState:
    """简易 state_store mock。"""
    def __init__(self, last_sent_at: float = 0.0):
        self._states = {}
        self._last_sent_at = last_sent_at

    def get_chat_state(self, chat_key: str) -> Dict[str, Any]:
        return self._states.get(chat_key, {"last_sent_at": self._last_sent_at})

    def is_skipped_chat(self, chat_key: str) -> bool:
        return False

    def set_last_sent(self, chat_key: str, ts: float):
        self._states.setdefault(chat_key, {})["last_sent_at"] = ts


def _runner_with_state(cfg: Dict[str, Any], last_sent_ago_sec: float = 0.0):
    """构造带 mock state_store 的 runner。"""
    r = object.__new__(MessengerRpaRunner)
    r._cfg = cfg
    r._self_skip_until = {}
    r._chat_key_prefix = "test"
    last_sent = time.time() - last_sent_ago_sec if last_sent_ago_sec > 0 else 0
    r._state = _MockState(last_sent_at=last_sent)
    return r


class TestSendGate:
    def test_no_history_passes(self):
        """没任何 cooldown / sent history → 通过。"""
        r = _runner_with_state({"sticky_thread": {"post_send_cooldown_sec": 90}})
        result = r._should_skip_send("Alice", source="test")
        assert result is None

    def test_self_skip_until_blocks(self):
        """self_skip_until cooldown 内 → 拒绝。"""
        r = _runner_with_state({"sticky_thread": {"post_send_cooldown_sec": 90}})
        # 设 60 秒后过期
        from src.integrations.messenger_rpa.runner import _self_skip_norm_key
        r._self_skip_until[_self_skip_norm_key("Alice")] = (
            __import__("time").monotonic() + 60
        )
        result = r._should_skip_send("Alice", source="test")
        assert result is not None
        assert "self_skip_cooldown" in result

    def test_last_sent_within_cooldown_blocks(self):
        """sent 后 30 秒内（< post_send_cooldown_sec=90s）→ 拒绝。"""
        r = _runner_with_state(
            {"sticky_thread": {"post_send_cooldown_sec": 90}},
            last_sent_ago_sec=30,
        )
        result = r._should_skip_send("Alice", source="test")
        assert result is not None
        assert "last_sent_cooldown" in result

    def test_last_sent_outside_cooldown_passes(self):
        """sent 后 100 秒（> post_send_cooldown_sec=90s）→ 通过。"""
        r = _runner_with_state(
            {"sticky_thread": {"post_send_cooldown_sec": 90}},
            last_sent_ago_sec=100,
        )
        result = r._should_skip_send("Alice", source="test")
        assert result is None

    def test_empty_chat_name_passes(self):
        """空 chat_name 不应抛错。"""
        r = _runner_with_state({})
        assert r._should_skip_send("", source="test") is None

    def test_critical_sticky_path_no_bypass(self):
        """🔴 关键防回归：模拟 sticky 路径绕过 inbox 检查的场景。
        即使 sticky 路径不经 inbox，gate 也必须拦下 cooldown 内的回复。
        这是 4 次疯狂事件根本原因的回归测试。
        """
        r = _runner_with_state(
            {"sticky_thread": {"post_send_cooldown_sec": 90}},
            last_sent_ago_sec=10,  # 10 秒前刚发过
        )
        # 模拟 sticky hash changed 后调用
        result = r._should_skip_send("Victor Zan", source="sticky_hash_changed")
        assert result is not None, (
            "sticky 路径必须能识别 cooldown 内的状态，"
            "不能让任何 send 路径绕过 cooldown（防回归 4 次疯狂事件）"
        )
