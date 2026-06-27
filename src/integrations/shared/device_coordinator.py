"""多平台设备协调器（Device Coordinator）。

每台物理手机运行一个 DeviceCoordinator，替代各平台独立的 Service 循环：
  1. 回到 Android 主屏
  2. 读取 Launcher 角标（LINE/WhatsApp/Messenger 等）
  3. 有角标 → 调对应 runner.run_once()；无角标 → 跳过
  4. 回到主屏，等待下一轮

优点：
  - 避免各平台 runner 争抢同一设备
  - 只在有真实未读时才进入 App，减少无效切换
  - 一个设备循环，开销更低

Config 示例（device_coordinator.devices[i]）：
  serial: IJ8HZLORS485PJWW
  label: "IJ8-主机"
  enabled: true
  poll_interval_sec: 15        # 有角标时的检测间隔
  idle_poll_interval_sec: 30   # 无角标时的检测间隔
  force_check_interval_sec: 120 # 不管角标，强制每 N 秒检查一遍（防漏消息）
  platforms:
    - type: line
      account_id: line_ij8
    - type: whatsapp
      account_id: wa_ij8
    - type: messenger
      account_id: msg_ij8
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any, Callable, Dict, List, Optional

from src.integrations.line_rpa import adb_helpers as adb
from src.integrations.line_rpa.adb_helpers import get_device_lock, prepare_device_for_rpa
from src.integrations.shared.launcher_scanner import (
    _PLATFORM_PKGS,
    has_badge,
    parse_dumpsys_notification,
    parse_launcher_badges,
)

logger = logging.getLogger(__name__)

_LAUNCHER_DUMP_REMOTE = "/sdcard/dc_launcher.xml"


class PlatformRunner:
    """一个平台 runner 的封装（type + runner 对象）。"""

    def __init__(self, platform_type: str, runner: Any, account_id: str = "") -> None:
        self.platform_type = platform_type  # "line" / "whatsapp" / "messenger"
        self.runner = runner
        self.account_id = account_id
        self.last_run_ts: float = 0.0
        self.last_result: Dict[str, Any] = {}
        self.last_step: str = ""             # 上一轮 step（用于「设备缺失」稳态日志降噪）
        # 熔断与统计
        self.consecutive_fail: int = 0       # 连续失败次数
        self.skip_until: float = 0.0         # 熔断：在此时刻前跳过
        self.total_replies: int = 0          # 本次进程累计回复数
        self.last_reply_ts: float = 0.0      # 最近一次成功回复时刻
        self.total_runs: int = 0             # 本次进程累计 run_once 次数
        self.last_no_action_badge: int = -1  # 上次运行"无有效操作"时的 badge 数量
        # 运行历史环形缓冲（最新在前）
        self.run_history: deque = deque(maxlen=20)
        # 熔断告警追踪
        self.last_alert_ts: float = 0.0      # 上次告警时间（冷却用）
        self.was_circuit_open: bool = False   # 上一轮是否在熔断中


class DeviceCoordinator:
    """单设备多平台协调器；由 DeviceCoordinatorService 持有并启动。"""

    def __init__(
        self,
        serial: str,
        platform_runners: List[PlatformRunner],
        *,
        label: str = "",
        poll_interval_sec: float = 15.0,
        idle_poll_interval_sec: float = 30.0,
        force_check_interval_sec: float = 120.0,
        home_settle_sec: float = 1.5,
        priority_by_badge: bool = True,
        run_timeout_sec: float = 120.0,
        circuit_breaker_threshold: int = 5,
        on_circuit_open: Optional[Callable[[str, str, int], None]] = None,
        on_recovery: Optional[Callable[[str, str, int], None]] = None,
        alert_cooldown_sec: float = 600.0,
    ) -> None:
        self._serial = serial
        self._label = label or serial[:8]
        self._platforms = platform_runners
        self._poll_interval = poll_interval_sec
        self._idle_poll_interval = idle_poll_interval_sec
        self._force_check_interval = force_check_interval_sec
        self._home_settle = home_settle_sec
        self._priority_by_badge = priority_by_badge
        self._run_timeout = run_timeout_sec            # 单个 run_once 最长时间
        self._cb_threshold = circuit_breaker_threshold # 连续失败 N 次触发熔断
        self._on_circuit_open = on_circuit_open        # callback(serial, platform_type, consecutive_fail)
        self._on_recovery = on_recovery                  # callback(serial, platform_type, prev_fail_count)
        self._alert_cooldown = alert_cooldown_sec         # 同设备同平台告警冷却
        # 所有平台的 Android 包名（用于通知/竖屏准备）
        self._packages: List[str] = [
            pkg
            for p in platform_runners
            for pkg in _PLATFORM_PKGS.get(p.platform_type, [])
        ]

        self._task: Optional[asyncio.Task] = None
        self._stop_evt = asyncio.Event()
        self._last_badge_check_ts: float = 0.0
        self._last_run_ts: float = 0.0          # 上次有效运行时刻
        self._last_force_check_ts: float = 0.0  # 上次强制全量检查时刻（保留，向后兼容）
        self._cycle_count: int = 0
        # S2-P0B: 每个平台独立记录上次强制检查时刻（防止 Messenger 独占饿死 WA/LINE）
        self._platform_last_force_ts: Dict[str, float] = {}

    # ── 生命周期 ──────────────────────────────────────────

    async def start(self) -> bool:
        if self._task and not self._task.done():
            return False
        self._stop_evt.clear()
        self._task = asyncio.create_task(
            self._loop(), name=f"device_coordinator_{self._label}"
        )
        logger.warning(
            "[DeviceCoordinator] %s 已启动，平台: %s",
            self._label,
            [p.platform_type for p in self._platforms],
        )
        return True

    async def stop(self) -> None:
        self._stop_evt.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    def status(self) -> Dict[str, Any]:
        now = time.time()
        return {
            "serial": self._serial,
            "label": self._label,
            "cycle_count": self._cycle_count,
            "last_run_ts": self._last_run_ts,
            "last_run_ago_sec": round(now - self._last_run_ts) if self._last_run_ts else None,
            "platforms": [
                {
                    "type": p.platform_type,
                    "account_id": p.account_id,
                    "last_run_ts": p.last_run_ts,
                    "last_run_ago_sec": round(now - p.last_run_ts) if p.last_run_ts else None,
                    "last_step": (p.last_result or {}).get("step", ""),
                    "last_ok": (p.last_result or {}).get("ok", False),
                    "consecutive_fail": p.consecutive_fail,
                    "circuit_open": now < p.skip_until,
                    "total_runs": p.total_runs,
                    "total_replies": p.total_replies,
                    "last_reply_ts": p.last_reply_ts or None,
                    "run_history": list(p.run_history),
                }
                for p in self._platforms
            ],
        }

    # ── 主循环 ──────────────────────────────────────────

    async def _loop(self) -> None:
        logger.warning("[DeviceCoordinator] %s 循环启动", self._label)
        while not self._stop_evt.is_set():
            try:
                await self._run_cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "[DeviceCoordinator] %s 单轮异常", self._label
                )

            interval = self._poll_interval
            try:
                await asyncio.wait_for(self._stop_evt.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

        logger.warning("[DeviceCoordinator] %s 循环退出", self._label)

    async def _run_cycle(self) -> None:
        self._cycle_count += 1
        t0 = time.time()

        async with get_device_lock(self._serial):
            # 0. 设备准备：竖屏 + 通知/角标开启 + 关闭勿扰
            await asyncio.to_thread(
                prepare_device_for_rpa, self._serial, self._packages
            )

            # 1. 读通知
            badges = await self._scan_badges()
            logger.debug("[DeviceCoordinator] %s badges=%s", self._label, badges)

            # 2. 决定要运行哪些平台
            now = time.time()
            force = (now - self._last_force_check_ts) >= self._force_check_interval
            platforms_to_run = []
            for p in self._platforms:
                # 熔断跳过
                if now < p.skip_until:
                    logger.debug(
                        "[DeviceCoordinator] %s %s 熔断中，跳过（%.0fs 后恢复）",
                        self._label, p.platform_type, p.skip_until - now,
                    )
                    continue
                # 自动恢复：刚从熔断恢复，先做设备健康检查
                if p.was_circuit_open and p.consecutive_fail >= self._cb_threshold:
                    try:
                        from src.integrations.messenger_rpa.device_health import ensure_device_ready
                        healthy, _info = await asyncio.to_thread(
                            ensure_device_ready, self._serial,
                            try_reconnect=True, try_wake=True, try_unlock_swipe=True,
                            max_attempts=2, hard_restart_on_fail=False,
                        )
                        logger.info(
                            "[DeviceCoordinator] %s %s 熔断恢复前健康检查: %s (%.0fms)",
                            self._label, p.platform_type, "OK" if healthy else "FAIL",
                            _info.get("total_ms", 0),
                        )
                    except Exception:
                        logger.debug("[DeviceCoordinator] 健康检查异常", exc_info=True)
                badge_now = badges.get(p.platform_type, 0)
                # 同 badge 数量 + 上次已无操作 → 跳过（系统通知/已读消息残留角标）
                _idle_since = now - p.last_run_ts
                if (
                    badge_now > 0
                    and badge_now == p.last_no_action_badge
                    and _idle_since < 300.0
                ):
                    logger.debug(
                        "[DeviceCoordinator] %s %s badge=%d 同上次无操作（%.0fs 前），跳过",
                        self._label, p.platform_type, badge_now, _idle_since,
                    )
                    continue
                # S2-P0B: per-platform 独立 force——各平台最多等 force_check_interval 秒
                _last_force_p = self._platform_last_force_ts.get(p.platform_type, 0.0)
                _per_platform_force = (now - _last_force_p) >= self._force_check_interval
                if _per_platform_force or has_badge(badges, p.platform_type):
                    platforms_to_run.append(p)
                    if _per_platform_force:
                        self._platform_last_force_ts[p.platform_type] = now

            # S2-P0B: 优先级：有 badge 的优先，同 badge 状态下最久未跑的优先（防饿死）
            if self._priority_by_badge and badges:
                platforms_to_run.sort(
                    key=lambda p: (
                        0 if badges.get(p.platform_type, 0) > 0 else 1,  # badge 先
                        p.last_run_ts,  # 越小=越久未跑=越优先
                    )
                )

            if force:
                self._last_force_check_ts = now

            if not platforms_to_run:
                logger.debug("[DeviceCoordinator] %s 无需运行", self._label)
                return

            # 3. 依次运行各平台（带超时 + 熔断计数）
            for pr in platforms_to_run:
                if self._stop_evt.is_set():
                    break
                badge_count = badges.get(pr.platform_type, 0)
                # 设备缺失（no_adb_device）是已知稳态：上一轮已是该 step 则降到 DEBUG，
                # 避免无设备时每个轮询周期都刷 WARNING（首次/恢复仍按 WARNING 记）。
                _absent_repeat = pr.last_step == "no_adb_device"
                (logger.debug if _absent_repeat else logger.warning)(
                    "[DeviceCoordinator] %s → %s (badge=%d, fail_streak=%d)",
                    self._label, pr.platform_type, badge_count, pr.consecutive_fail,
                )
                pr.total_runs += 1
                _pr_t0 = time.time()
                try:
                    result = await asyncio.wait_for(
                        pr.runner.run_once(), timeout=self._run_timeout
                    )
                    pr.last_result = result
                    pr.last_run_ts = time.time()
                    step = result.get("step", "")
                    ok = result.get("ok", False)
                    # 记录运行历史
                    elapsed_ms = (pr.last_run_ts - _pr_t0) * 1000
                    pr.run_history.appendleft({
                        "ts": pr.last_run_ts,
                        "step": step,
                        "ok": ok,
                        "badge": badges.get(pr.platform_type, 0),
                    })
                    # 统计聚合
                    _reply_steps_check = {"sent", "multi_sent", "replied", "send_ok"}
                    _is_reply = ok and any(s in step for s in _reply_steps_check)
                    try:
                        from src.integrations.shared.device_stats import get_device_stats
                        get_device_stats().record(
                            self._serial, pr.platform_type, pr.account_id,
                            ok=ok, is_reply=_is_reply, elapsed_ms=elapsed_ms,
                        )
                    except Exception:
                        pass

                    # 重置熔断 / 统计回复 / 同 badge 无操作记录
                    _no_action_steps = {
                        "no_unread", "no_peer_message", "inbox_idle",
                        "skill_no_reply", "no_message",
                    }
                    current_badge = badges.get(pr.platform_type, 0)
                    if ok:
                        # 恢复检测：上一轮在熔断中，本轮成功 → 推恢复通知
                        prev_fail = pr.consecutive_fail
                        if pr.was_circuit_open and self._on_recovery:
                            try:
                                self._on_recovery(self._serial, pr.platform_type, prev_fail)
                            except Exception:
                                logger.debug("on_recovery callback 异常", exc_info=True)
                        pr.was_circuit_open = False
                        pr.consecutive_fail = 0
                        _reply_steps = {"sent", "multi_sent", "replied", "send_ok"}
                        if any(s in step for s in _reply_steps):
                            pr.total_replies += 1
                            pr.last_reply_ts = time.time()
                            pr.last_no_action_badge = -1  # 有回复 → 重置
                        elif step in _no_action_steps or step.startswith("no_"):
                            pr.last_no_action_badge = current_badge  # 记录无操作 badge 值
                    else:
                        pr.consecutive_fail += 1
                        self._maybe_trip_breaker(pr)

                    # no_adb_device 稳态降噪：连续相同则 DEBUG，转入/恢复仍 WARNING
                    _absent_repeat_post = (step == "no_adb_device" and _absent_repeat)
                    (logger.debug if _absent_repeat_post else logger.warning)(
                        "[DeviceCoordinator] %s %s step=%s ok=%s replies=%d elapsed=%.1fs",
                        self._label, pr.platform_type, step, ok,
                        pr.total_replies, time.time() - t0,
                    )
                    pr.last_step = step
                except asyncio.TimeoutError:
                    pr.consecutive_fail += 1
                    self._maybe_trip_breaker(pr)
                    logger.warning(
                        "[DeviceCoordinator] %s %s run_once 超时（%.0fs）",
                        self._label, pr.platform_type, self._run_timeout,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pr.consecutive_fail += 1
                    self._maybe_trip_breaker(pr)
                    logger.exception(
                        "[DeviceCoordinator] %s %s run_once 异常",
                        self._label, pr.platform_type,
                    )

                # 平台切换间回主屏
                if len(platforms_to_run) > 1 and not self._stop_evt.is_set():
                    await self._go_home()

        self._last_run_ts = time.time()

    def _maybe_trip_breaker(self, pr: PlatformRunner) -> None:
        """连续失败达阈值时触发熔断，指数退避 skip_until。"""
        if pr.consecutive_fail >= self._cb_threshold:
            now = time.time()
            backoff = min(300.0, 30.0 * (2 ** (pr.consecutive_fail - self._cb_threshold)))
            pr.skip_until = now + backoff
            # 仅在「首次跳闸」记 WARNING；已处熔断态的重复跳闸降到 DEBUG（无设备时
            # 每个 backoff 周期都会重新失败跳闸，否则照样刷屏）。健康告警另有冷却。
            _first_trip = not pr.was_circuit_open
            pr.was_circuit_open = True
            (logger.warning if _first_trip else logger.debug)(
                "[DeviceCoordinator] %s %s 熔断触发（连续失败 %d 次），%.0fs 后重试",
                self._label, pr.platform_type, pr.consecutive_fail, backoff,
            )
            # 触发健康告警回调（带冷却：同平台 N 秒内不重复告警）
            if self._on_circuit_open and (now - pr.last_alert_ts) >= self._alert_cooldown:
                pr.last_alert_ts = now
                try:
                    self._on_circuit_open(self._serial, pr.platform_type, pr.consecutive_fail)
                except Exception:
                    logger.debug("on_circuit_open callback 异常", exc_info=True)

    # ── ADB 工具 ──────────────────────────────────────

    async def _go_home(self) -> None:
        """按 HOME 键回到 Launcher。"""
        await asyncio.to_thread(
            adb.input_keyevent, self._serial, "KEYCODE_HOME"
        )
        await asyncio.sleep(self._home_settle)

    async def _scan_badges(self) -> Dict[str, int]:
        """检测有通知的平台。

        优先使用 dumpsys notification（不依赖 App 在主屏的位置）；
        若失败则回退到 Launcher XML 角标解析。
        """
        # ── 方式 1：dumpsys notification（推荐）──────────────────────────────
        try:
            r = await asyncio.to_thread(
                adb.run_adb,
                ["shell", "dumpsys notification --noredact 2>/dev/null"],
                serial=self._serial,
                timeout=10.0,
            )
            if r.returncode == 0 and r.stdout:
                text = r.stdout if isinstance(r.stdout, str) else r.stdout.decode("utf-8", errors="replace")
                badges, senders = parse_dumpsys_notification(text, return_senders=True)  # type: ignore[misc]
                sys_senders = senders.get("line_system", [])
                if sys_senders:
                    logger.warning(
                        "[DeviceCoordinator] %s LINE 系统通知已过滤（不触发 run）: %s",
                        self._label, sys_senders,
                    )
                self._last_badge_check_ts = time.time()
                return badges
        except Exception:
            logger.debug(
                "[DeviceCoordinator] %s dumpsys scan 失败，尝试 Launcher XML",
                self._label, exc_info=True,
            )

        # ── 方式 2：Launcher XML（回退）──────────────────────────────────────
        try:
            r = await asyncio.to_thread(
                adb.dump_ui_hierarchy_xml, self._serial, _LAUNCHER_DUMP_REMOTE
            )
            if r.returncode != 0 or not r.stdout:
                return {}
            raw: bytes = r.stdout if isinstance(r.stdout, bytes) else r.stdout.encode()
            idx = raw.find(b"<?xml")
            if idx < 0:
                idx = raw.find(b"<hierarchy")
            if idx > 0:
                raw = raw[idx:]
            self._last_badge_check_ts = time.time()
            return parse_launcher_badges(raw)
        except Exception:
            logger.debug(
                "[DeviceCoordinator] %s Launcher XML scan 失败", self._label, exc_info=True
            )
            return {}
