"""LINE RPA 后台服务：主进程托管的长期运行循环，供 Web 后台 start/stop/pause。

职责：
- 在 main.py 启动时按配置自动拉起轮询循环（可开关）
- 暴露 start/stop/pause/resume/trigger_once/status 控制面，供 Web 路由调用
- 复用 main.py 已有的 SkillManager / AIClient，而非另起一份（省内存、共享人设/KB）
- 与 LineRpaStateStore 组合，记录每次 run 的结果
- 自适应轮询：收到有效对方消息则下一轮更快拉取；连续空跑则指数退避
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional

from src.integrations.line_rpa.runner import LineRpaRunner
from src.integrations.line_rpa.state_store import (
    LineRpaStateStore,
    default_state_db_path,
    migrate_from_legacy_json,
)

logger = logging.getLogger(__name__)


class LineRpaService:
    """
    长期后台服务；只能被创建一次并由 main.py 生命周期管理。
    对外线程安全的接口：status / pause_for / resume / trigger_once / reconfigure。
    """

    def __init__(
        self,
        *,
        config_manager: Any,
        skill_manager: Any,
        line_rpa_cfg: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._cm = config_manager
        self._sm = skill_manager
        self._cfg: Dict[str, Any] = dict(line_rpa_cfg or {})
        self._merged_cfg: Dict[str, Any] = self._merged()

        cfg_dir = Path(self._cm.config_path).parent
        db_path = default_state_db_path(self._cm.config_path)
        self._state = LineRpaStateStore(
            db_path,
            max_runs_kept=int(self._merged_cfg.get("recent_runs_buffer", 500) or 500),
        )
        # 一次性迁移旧 json（若存在）
        migrate_from_legacy_json(self._state, cfg_dir / "line_rpa_state.json")

        self._runner = LineRpaRunner(
            config_manager=self._cm,
            skill_manager=self._sm,
            line_rpa_cfg=self._merged_cfg,
            state_store=self._state,
        )
        # W4-Runner：ContactHooks 由 main.py 后置注入
        self._contact_hooks: Optional[Any] = None
        self._task: Optional[asyncio.Task] = None
        self._stop_evt = asyncio.Event()
        self._trigger_evt = asyncio.Event()
        self._pause_until: float = 0.0
        self._started_at: float = 0.0
        self._last_run: Dict[str, Any] = {}
        self._consecutive_fail: int = 0
        # 自适应轮询状态
        self._last_had_peer_ts: float = 0.0
        self._last_tick_ts: float = 0.0
        # P4-5：告警闭环 - 连续 possibly_missed 计数 + 下次检查时刻
        self._consecutive_missed: int = 0
        self._next_health_check_ts: float = 0.0
        # P5-2：多类 streak 计数器
        self._send_fail_streak: int = 0
        self._skill_error_times: list = []   # 近 1h 内 skill_error 时间戳

    # ── 默认与合并 ───────────────────────────────────────
    def _defaults(self) -> Dict[str, Any]:
        return {
            "enabled": False,
            "line_package": "jp.naver.line.android",
            "splash_activity": "jp.naver.line.android/.activity.SplashActivity",
            "dump_remote_path": "/sdcard/line_rpa_dump.xml",
            "peer_left_ratio": 0.42,
            "chat_key": "line_rpa:default",
            "default_reply_lang": "zh",
            # P4-3：回复模式 — auto=直接发送；approve=进入待审核队列；off=永不发
            "reply_mode": "auto",
            "approve_max_deliver_per_cycle": 3,
            # P5-1：审批条目防陈旧 + TTL 自动清理
            "approve_stale_check": {"enabled": True},
            "approve_pending_ttl_hours": 24.0,  # 0=不清理
            # P5-2：多类告警阈值
            "alert_thresholds": {
                "send_fail_streak": 3,          # 连续 N 次发送失败触发 send_fail_streak
                "skill_error_burst": 5,         # 近 1h 内 ≥N 次 skill 错误触发 skill_error_burst
                "skill_error_window_sec": 3600, # 滑动窗口秒
                "adb_lost": True,               # no_adb_device 立即触发 adb_lost
                "ime_lost": True,               # P6-A3: AdbKeyboard 广播失败立即触发 ime_lost
            },
            # P6: vision 驱动的列表扫描（OOM 机型回退）
            "vision_scan": {
                "enabled": False,               # 需要同时启用 vision_read_fallback.enabled
                "scan_budget_sec": 30.0,        # 两次 vision 列表扫描最小间隔
                "max_pages": 5,                 # 最多扫描页数
                "list_prompt_override": "",     # 空=使用内置 LIST_VISION_PROMPT
            },
            # P4-5：告警闭环（dumpsys notification 定期对账）
            "health_check": {
                "enabled": True,
                "interval_sec": 300.0,       # 每 5 分钟一次（不阻塞主循环）
                "miss_streak_alert": 3,      # 连续 N 次 possibly_missed 即告警
                "alert_dedup_window_sec": 1800.0,  # 30 分钟内同 kind 不重复告警
            },
            # P2-4：群聊 @我 提权 / 回复策略
            "self_names": [],                 # 本账号昵称（支持多个别名）
            "group_reply_policy": "all",      # all | mention_only | never
            "reply_style_hint": "",
            "reply_style_hint_mentioned": "", # 群内被 @ 时使用的替代风格
            # P3-3：连续对方气泡聚合（默认开启，显著改善长消息上下文）
            "peer_multi_bubble": {
                "enabled": True,
                "max_gap_px": 220,
                "max_count": 6,
                "left_cx_tol_px": 140,
                "joiner": "\n",
                # P4-1：给最新一条加前缀（AI 时序感知）
                "mark_latest": True,
                "latest_tag": "[最新] ",
            },
            "use_adb_keyboard": True,
            "adb_keyboard_ime": "com.android.adbkeyboard/.AdbIME",
            "adb_keyboard_prefer_b64": True,
            "adb_keyboard_package": "com.android.adbkeyboard",
            "redump_before_send": True,
            "read_fallback": "none",
            "screenshot_ocr": {
                "enabled": False,
                "crop_bottom_ratio": 0.42,
                "peer_left_strip_ratio": 0.58,
                "tesseract_lang": "chi_sim+eng",
                "skip_if_unchanged": True,
            },
            "vision_read_fallback": {"enabled": False},
            "use_backend_persona": True,
            # 多会话导航（MVP）：开启后 runner 会自动回到聊天列表→扫未读→逐个回
            "navigation": {
                "enabled": False,
                "max_chats_per_run": 3,       # 单轮最多处理 N 个未读
                "max_scan_rows": 10,          # 扫列表时最多取前 N 行
                "max_scroll_attempts": 1,     # 屏幕无可处理未读时最多向下滑动几次揭示更多（0=不滚动）
                "scroll_to_top_attempts": 1,  # P4-2：每轮开始时最多向上滑几次回到列表顶（0=关闭）
                "cycle_budget_sec": 60.0,     # 单轮预算（超时就 break）
                "after_tap_sleep_sec": 0.8,
                "between_chats_ms": [900, 2200],
                "chat_list_tab_tap": [],      # 可选：人工标定 '聊天' tab 坐标
                "allow_list": [],             # 名称白名单（子串匹配）；为空=不限
                "deny_list": [],              # 名称黑名单（子串匹配）
                "red_dot_fallback": {
                    "enabled": False,
                    "min_red_ratio": 0.06,
                    "right_strip_ratio": 0.2,
                },
            },
            "failure_shots": {
                "enabled": False,
                "dir": "logs/line_rpa/failures",
                "max_files": 200,
                "on_steps": [
                    "open_fail", "no_peer_text", "skill_error",
                    "send_failed", "no_xml_in_room",
                ],
            },
            # 服务自管参数
            "service": {
                "autostart": True,            # enabled=true 时随主程序启动
                "interval_sec": 15.0,         # 基准轮询
                "fast_interval_sec": 4.0,     # 刚收到新消息的短间隔
                "fast_window_sec": 60.0,      # 进入快轮询的窗口
                "slow_interval_sec": 30.0,    # 连续空跑进入慢轮询
                "slow_after_empty": 6,        # 连续 N 轮空则进入慢轮询
                "jitter_pct": 0.25,           # ±25% 随机抖动
                "max_consecutive_fail": 20,   # 连续失败（非空跑）达到则暂停
                "auto_pause_sec_on_fail": 900,
            },
            # 拟人节奏
            "human_pacing": {
                "enabled": True,
                "read_pause_ms": [800, 2000],
                "per_char_ms": [40, 80],
                "slow_type": False,
                "split_mode": "sentence",
                "split_max_chars": 80,
                "split_max_parts": 3,
                "inter_msg_ms": [700, 1800],
            },
            "recent_runs_buffer": 500,
        }

    @staticmethod
    def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(base)
        for k, v in (override or {}).items():
            if isinstance(v, dict) and isinstance(out.get(k), dict):
                out[k] = LineRpaService._deep_merge(out[k], v)
            else:
                out[k] = v
        return out

    def _merged(self) -> Dict[str, Any]:
        return self._deep_merge(self._defaults(), self._cfg)

    # ── 生命周期 ─────────────────────────────────────────
    async def start(self) -> bool:
        """被 main.py 调用：按配置决定是否真的拉起循环。"""
        if self._task and not self._task.done():
            return True
        svc_cfg = self._merged_cfg.get("service", {}) or {}
        if not self._merged_cfg.get("enabled") or not svc_cfg.get("autostart", True):
            logger.info("LineRpaService 未启用或 autostart=false，跳过自动启动")
            return False
        self._stop_evt.clear()
        self._trigger_evt.clear()
        self._started_at = time.time()
        self._task = asyncio.create_task(self._loop(), name="line_rpa_service_loop")
        logger.info("LineRpaService 已启动（每轮基准 %.1fs）", float(svc_cfg.get("interval_sec", 15.0)))
        return True

    async def stop(self) -> None:
        self._stop_evt.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=8.0)
            except asyncio.TimeoutError:
                self._task.cancel()
            except Exception:
                pass
        self._task = None
        try:
            self._state.close()
        except Exception:
            pass

    async def _loop(self) -> None:
        svc = self._merged_cfg.get("service", {}) or {}
        empty_streak = 0
        while not self._stop_evt.is_set():
            # pause 检查
            now = time.time()
            if self._pause_until > now:
                remain = self._pause_until - now
                try:
                    await asyncio.wait_for(
                        self._stop_evt.wait(),
                        timeout=min(remain, 60.0),
                    )
                except asyncio.TimeoutError:
                    pass
                continue

            self._last_tick_ts = time.time()
            try:
                result = await self._runner.run_once()
                self._last_run = result
                step = result.get("step", "")
                ok = bool(result.get("ok"))
                peer = (result.get("peer_text") or "").strip()
                if peer:
                    self._last_had_peer_ts = time.time()
                    empty_streak = 0
                elif step in ("no_peer_text", "duplicate_peer_skipped", "screen_unchanged_skipped"):
                    empty_streak += 1
                # 失败计数仅统计非"空跑"类 step
                success_like = ok or step in (
                    "no_peer_text", "duplicate_peer_skipped",
                    "empty_reply", "screen_unchanged_skipped",
                    "dry_run_done", "sent",
                    "reply_disabled", "awaiting_approval",
                )
                if success_like:
                    self._consecutive_fail = 0
                else:
                    self._consecutive_fail += 1
                    limit = int(svc.get("max_consecutive_fail", 20) or 20)
                    if limit > 0 and self._consecutive_fail >= limit:
                        pause_sec = float(svc.get("auto_pause_sec_on_fail", 900) or 900)
                        self._pause_until = time.time() + pause_sec
                        logger.warning(
                            "LineRpaService 连续失败 %s 次，自动暂停 %.0fs（step=%s error=%s）",
                            self._consecutive_fail, pause_sec, step, result.get("error"),
                        )
                        self._consecutive_fail = 0

                # P5-2：多类告警 —— 基于本轮 step 即时判定
                try:
                    self._classify_alerts(step, result)
                except Exception:
                    logger.debug("classify_alerts 失败", exc_info=True)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("LineRpaService run_once 异常: %s", e)
                self._consecutive_fail += 1
                self._last_run = {"ok": False, "step": "loop_exception", "error": str(e)}

            # P4-3：投递已审批 pending（优先级高：让审批过的回复尽快送到对方）
            reply_mode = str(self._merged_cfg.get("reply_mode", "auto") or "auto").lower()
            if reply_mode == "approve":
                try:
                    deliver_res = await self._runner.run_pending_deliveries(
                        max_deliver=int(self._merged_cfg.get("approve_max_deliver_per_cycle", 3) or 3),
                    )
                    if deliver_res.get("delivered") or deliver_res.get("failed"):
                        self._last_run["pending_deliver"] = deliver_res
                        logger.info(
                            "pending_deliver delivered=%s failed=%s",
                            deliver_res.get("delivered"), deliver_res.get("failed"),
                        )
                except Exception:
                    logger.debug("run_pending_deliveries 失败", exc_info=True)

            # P4-5：后台健康检查（节流：按 interval_sec 控制）
            try:
                await self._maybe_run_health_check()
            except Exception:
                logger.debug("health_check 失败", exc_info=True)

            # 自适应 + 手动触发感知
            interval = self._compute_next_interval(empty_streak)
            try:
                # 等待被触发或 stop 或超时
                tg_task = asyncio.create_task(self._trigger_evt.wait())
                st_task = asyncio.create_task(self._stop_evt.wait())
                done, pending = await asyncio.wait(
                    [tg_task, st_task],
                    timeout=interval,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in pending:
                    t.cancel()
                if self._trigger_evt.is_set():
                    self._trigger_evt.clear()
            except asyncio.CancelledError:
                raise

    def _compute_next_interval(self, empty_streak: int) -> float:
        svc = self._merged_cfg.get("service", {}) or {}
        base = float(svc.get("interval_sec", 15.0) or 15.0)
        fast = float(svc.get("fast_interval_sec", 4.0) or 4.0)
        slow = float(svc.get("slow_interval_sec", 30.0) or 30.0)
        fast_window = float(svc.get("fast_window_sec", 60.0) or 60.0)
        slow_after = int(svc.get("slow_after_empty", 6) or 6)
        jitter = float(svc.get("jitter_pct", 0.25) or 0.0)

        # 最近对方消息在 fast_window 内 → 快轮询
        if self._last_had_peer_ts and (time.time() - self._last_had_peer_ts) < fast_window:
            interval = fast
        elif slow_after > 0 and empty_streak >= slow_after:
            interval = slow
        else:
            interval = base

        if jitter > 0:
            import random
            interval = interval * (1.0 + random.uniform(-jitter, jitter))
        return max(1.0, interval)

    # ── 控制面（线程安全 / 协程安全）──────────────────────
    def status(self) -> Dict[str, Any]:
        svc = self._merged_cfg.get("service", {}) or {}
        running = bool(self._task and not self._task.done())
        now = time.time()
        stats_24 = self._state.run_stats(24.0)
        stats_1 = self._state.run_stats(1.0)
        nav_cfg = self._merged_cfg.get("navigation", {}) or {}
        return {
            "enabled_cfg": bool(self._merged_cfg.get("enabled")),
            "running": running,
            "paused": self._pause_until > now,
            "pause_until_ts": self._pause_until if self._pause_until > now else 0,
            "pause_remaining_sec": max(0, int(self._pause_until - now)),
            "started_at": int(self._started_at) if self._started_at else 0,
            "uptime_sec": int(now - self._started_at) if self._started_at else 0,
            "last_tick_ts": int(self._last_tick_ts) if self._last_tick_ts else 0,
            "last_run": self._last_run,
            "consecutive_fail": self._consecutive_fail,
            "service_cfg": {
                "interval_sec": svc.get("interval_sec"),
                "fast_interval_sec": svc.get("fast_interval_sec"),
                "slow_interval_sec": svc.get("slow_interval_sec"),
                "max_consecutive_fail": svc.get("max_consecutive_fail"),
            },
            "navigation": {
                "enabled": bool(nav_cfg.get("enabled")),
                "max_chats_per_run": int(nav_cfg.get("max_chats_per_run", 3) or 3),
                "allow_list": list(nav_cfg.get("allow_list") or []),
                "deny_list": list(nav_cfg.get("deny_list") or []),
            },
            # P5-6：运行模式 + 审批队列简况（供顶栏徽章使用）
            "reply_mode": str(self._merged_cfg.get("reply_mode", "auto") or "auto"),
            "pending_stats": self._state.pending_stats(),
            "alerts_unacked": self._state.alerts_count_unacked(),
            # P7-2：未确认的 IME 告警数（与 vision 列表扫描元数据）
            "ime_lost_alerts_unacked": self._state.alerts_count_unacked(kind="ime_lost"),
            "vision_scan": {
                "list_enabled": bool((self._merged_cfg.get("vision_scan") or {}).get("enabled")),
                "read_fallback_enabled": bool(
                    (self._merged_cfg.get("vision_read_fallback") or {}).get("enabled")
                ),
                "last_list_scan": (self._last_run or {}).get("vision_list_scan"),
            },
            "last_run_extras": {
                "nav_state": self._last_run.get("nav_state"),
                "chats_processed": self._last_run.get("chats_processed"),
                "unread_count": self._last_run.get("unread_count"),
                "per_chat_results": self._last_run.get("per_chat_results") or [],
            },
            "stats_24h": stats_24,
            "stats_1h": stats_1,
        }

    def pause_for(self, seconds: float) -> None:
        secs = max(0.0, float(seconds))
        self._pause_until = time.time() + secs
        logger.info("LineRpaService 手动暂停 %.0fs", secs)

    def resume(self) -> None:
        self._pause_until = 0.0
        self._trigger_evt.set()
        logger.info("LineRpaService 已恢复")

    def trigger_once(self) -> None:
        """请求立刻进入下一轮（不等待当前 interval）。"""
        self._trigger_evt.set()

    def reconfigure(self, new_line_rpa_cfg: Dict[str, Any]) -> None:
        """热更新 line_rpa 配置段（不重启循环）。"""
        self._cfg = dict(new_line_rpa_cfg or {})
        self._merged_cfg = self._merged()
        try:
            self._runner.reconfigure(self._merged_cfg)
        except Exception:
            logger.debug("runner.reconfigure 失败", exc_info=True)

    def set_contact_hooks(self, hooks: Optional[Any]) -> None:
        """main.py 在 contacts 子系统 bootstrap 后调用。W4-Runner 接入 hook。"""
        self._contact_hooks = hooks
        try:
            self._runner.set_contact_hooks(hooks)
        except Exception:
            logger.debug("runner.set_contact_hooks 失败", exc_info=True)

    # ── 只读视图：供 Web 路由 ─────────────────────────────
    def recent_runs(self, limit: int = 50, *, only_with_peer: bool = False) -> list:
        return self._state.recent_runs(limit, only_with_peer=only_with_peer)

    def list_chats(self, limit: int = 30) -> list:
        return self._state.list_chats(limit)

    def effective_config(self) -> Dict[str, Any]:
        return dict(self._merged_cfg)

    # ── P4-5：告警闭环 ──────────────────────────────────

    def _classify_alerts(self, step: str, result: Dict[str, Any]) -> None:
        """P5-2：按 step 即时生成 send_fail_streak / adb_lost / skill_error_burst 告警。"""
        thr = self._merged_cfg.get("alert_thresholds") or {}
        if not isinstance(thr, dict):
            return
        dedup = int((self._merged_cfg.get("health_check") or {}).get(
            "alert_dedup_window_sec", 900,
        ) or 0)
        # 1) adb_lost
        if step == "no_adb_device" and thr.get("adb_lost", True):
            self._state.insert_alert(
                kind="adb_lost",
                severity="critical",
                message="ADB 设备丢失（no_adb_device）",
                detail={"step": step, "error": result.get("error")},
                dedup_window_sec=dedup,
            )

        # 2) send_fail_streak
        send_fail_steps = {"send_failed", "open_fail", "send_verify_failed"}
        if step in send_fail_steps:
            self._send_fail_streak += 1
            limit = int(thr.get("send_fail_streak", 3) or 0)
            if limit > 0 and self._send_fail_streak >= limit:
                self._state.insert_alert(
                    kind="send_fail_streak",
                    severity="warning",
                    message=f"连续 {self._send_fail_streak} 次发送失败（step={step}）",
                    detail={"step": step, "streak": self._send_fail_streak,
                            "error": result.get("error")},
                    dedup_window_sec=dedup,
                )
        elif step in ("sent", "dry_run_done"):
            self._send_fail_streak = 0

        # 3) ime_lost：IME 广播失败（AdbKeyboard 不可用）
        ime_alert_enabled = thr.get("ime_lost", True)
        if ime_alert_enabled:
            ime_failed = False
            # 单会话结果
            send_info = result.get("send") or {}
            if isinstance(send_info, dict):
                parts = send_info.get("parts") or []
                ime_failed = any(
                    bool(p.get("ime_broadcast_failed")) for p in parts
                    if isinstance(p, dict)
                )
            # 多会话 per_chat_results
            if not ime_failed:
                for pr in result.get("per_chat_results") or []:
                    if not isinstance(pr, dict):
                        continue
                    si = pr.get("send") or {}
                    if not isinstance(si, dict):
                        continue
                    for p in (si.get("parts") or []):
                        if isinstance(p, dict) and p.get("ime_broadcast_failed"):
                            ime_failed = True
                            break
                    if ime_failed:
                        break
            if ime_failed:
                self._state.insert_alert(
                    kind="ime_lost",
                    severity="warning",
                    message="AdbKeyboard 广播失败，IME 可能已被切换或崩溃",
                    detail={"step": step},
                    dedup_window_sec=dedup,
                )

        # 4) skill_error_burst：滑动窗口内异常计数
        if step in ("skill_error", "llm_error", "ai_error"):
            now = time.time()
            window = float(thr.get("skill_error_window_sec", 3600) or 3600)
            self._skill_error_times.append(now)
            # 清理老时间戳
            self._skill_error_times = [
                t for t in self._skill_error_times if now - t <= window
            ]
            limit = int(thr.get("skill_error_burst", 5) or 0)
            if limit > 0 and len(self._skill_error_times) >= limit:
                self._state.insert_alert(
                    kind="skill_error_burst",
                    severity="warning",
                    message=f"{int(window/60)} 分钟内 {len(self._skill_error_times)} 次 AI/技能错误",
                    detail={"step": step, "count": len(self._skill_error_times),
                            "window_sec": int(window)},
                    dedup_window_sec=dedup,
                )

    async def _maybe_run_health_check(self) -> None:
        hc = self._merged_cfg.get("health_check") or {}
        if not isinstance(hc, dict) or not hc.get("enabled", True):
            return
        interval = float(hc.get("interval_sec", 300.0) or 300.0)
        if interval <= 0:
            return
        now = time.time()
        if now < self._next_health_check_ts:
            return
        self._next_health_check_ts = now + interval

        # P5-1：顺便清扫过期 pending
        ttl_hours = float(self._merged_cfg.get("approve_pending_ttl_hours", 24.0) or 0.0)
        if ttl_hours > 0:
            try:
                expired = self._state.sweep_stale_pending(
                    ttl_sec=ttl_hours * 3600.0, reason="ttl_expired",
                )
                if expired:
                    logger.info("pending TTL 过期清理 %d 条", len(expired))
            except Exception:
                logger.debug("sweep_stale_pending 失败", exc_info=True)

        try:
            snap = await self.notification_snapshot()
        except Exception:
            return
        verdict = str(snap.get("verdict") or "")
        notif_count = int((snap.get("snapshot") or {}).get("count") or 0)
        main_unread = int(snap.get("main_unread") or -1)

        if verdict == "possibly_missed":
            self._consecutive_missed += 1
        else:
            self._consecutive_missed = 0

        threshold = max(1, int(hc.get("miss_streak_alert", 3) or 3))
        if self._consecutive_missed >= threshold:
            dedup = float(hc.get("alert_dedup_window_sec", 1800.0) or 1800.0)
            self._state.insert_alert(
                kind="possibly_missed",
                severity="warn",
                message=(
                    f"连续 {self._consecutive_missed} 次通知栏对账发现疑似漏读："
                    f"通知栏 {notif_count} 条 / 主循环最近 {main_unread} 条"
                ),
                detail={
                    "notif_count": notif_count,
                    "main_unread": main_unread,
                    "streak": self._consecutive_missed,
                },
                dedup_window_sec=dedup,
            )

    def list_alerts(self, *, only_unacked: bool = True, limit: int = 50) -> list:
        return self._state.list_alerts(only_unacked=only_unacked, limit=limit)

    def timeline(self, *, minutes: int = 60, limit: int = 200) -> list:
        """P5-3：三源合并时间轴（runs / pending / alerts）"""
        return self._state.timeline(minutes=minutes, limit=limit)

    def list_audit(self, *, target_type: Optional[str] = None, limit: int = 100) -> list:
        """P5-5：审计日志查询"""
        return self._state.list_audit(target_type=target_type, limit=limit)

    def ack_alert(self, alert_id: int, *, by: str = "") -> Optional[Dict[str, Any]]:
        return self._state.ack_alert(alert_id, by=by)

    def ack_all_alerts(self, *, by: str = "") -> int:
        return self._state.ack_all_alerts(by=by)

    def alerts_count_unacked(self) -> int:
        return self._state.alerts_count_unacked()

    # ── P4-3：Human-in-the-Loop API for routes ────────────
    def list_pending(self, *, status: Optional[str] = None, limit: int = 50) -> list:
        return self._state.list_pending(status=status, limit=limit)

    def pending_stats(self) -> Dict[str, int]:
        return self._state.pending_stats()

    def resolve_pending(
        self, pending_id: int, *, action: str,
        final_reply: Optional[str] = None, by: str = "",
    ) -> Optional[Dict[str, Any]]:
        res = self._state.resolve_pending(
            pending_id, action=action, final_reply=final_reply, by=by,
        )
        # approve 后尽快触发下一轮，让 runner 去投递
        if res and str(res.get("status")) == "approved":
            try:
                self._trigger_evt.set()
            except Exception:
                pass
        return res

    # ── P3-1：通知栏快照（供 Web / 健康检查使用） ─────────────
    async def notification_snapshot(self) -> Dict[str, Any]:
        """异步拉取 `dumpsys notification --noredact` 并解析 LINE 条目。

        返回包含：`snapshot`（NotifSnapshot.to_dict）+ `verdict`（与主循环未读数对账后的健康标签）。
        若当前没有 serial 或 adb 失败，verdict 为 `unknown`。
        """
        from src.integrations.line_rpa import notification_check as nc

        serial = None
        try:
            serial = self._runner._resolve_serial()  # type: ignore[attr-defined]
        except Exception:
            serial = None
        line_pkg = str(self._merged_cfg.get("line_package", "jp.naver.line.android"))
        snap = await asyncio.to_thread(
            nc.fetch_line_notifications,
            serial, line_pkg=line_pkg,
        )
        last_run = getattr(self, "_last_run", {}) or {}
        try:
            main_unread = int(last_run.get("unread_count", -1) or -1)
        except (TypeError, ValueError):
            main_unread = -1
        verdict = nc.health_verdict(
            main_unread=main_unread,
            notif_count=snap.total(),
        )
        return {
            "snapshot": snap.to_dict(),
            "main_unread": main_unread,
            "verdict": verdict,
        }
