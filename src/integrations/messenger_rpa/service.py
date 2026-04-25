"""Messenger RPA 后台服务：主进程托管的长期运行循环。

职责：
- main.py 启动时按配置自动拉起轮询循环（可开关）
- 暴露 start/stop/pause/resume/trigger_once/status，供 Web 路由调用
- 复用主进程 SkillManager / AIClient
- 与 MessengerRpaStateStore 组合，记录每次 run 的结果
- 自适应轮询：有未读则缩短下一次间隔；连续空跑则指数退避

注：这是 v0.1 脚手架，先把骨架立住，复杂特性（健康检查、审批、告警）参考 line_rpa.service 后续逐步引入。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.integrations.messenger_rpa.account_pool import AccountRegistry
from src.integrations.messenger_rpa.runner import MessengerRpaRunner
from src.integrations.messenger_rpa.state_store import (
    MessengerRpaStateStore,
    default_state_db_path,
)

logger = logging.getLogger(__name__)


class MessengerRpaService:
    """长期后台服务；只能被创建一次并由 main.py 生命周期管理。"""

    def __init__(
        self,
        *,
        config_manager: Any,
        skill_manager: Any,
        messenger_rpa_cfg: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._cm = config_manager
        self._sm = skill_manager
        self._cfg: Dict[str, Any] = dict(messenger_rpa_cfg or {})
        self._merged_cfg: Dict[str, Any] = self._merged()

        cfg_dir = Path(self._cm.config_path).parent
        # ★ P5-1：AccountRegistry 读 accounts 配置；无配置则回到单账号 "default"
        self._account_registry: AccountRegistry = AccountRegistry.from_config(
            self._merged_cfg, self._cm.config_path,
        )
        primary = self._resolve_primary_account()
        self._primary_account_id: str = primary.account_id
        # 主 account 的 state store 作为 self._state（完全兼容旧调用者）
        self._state: MessengerRpaStateStore = primary.state_store()
        # ★ _telegram_client 必须在 _get_or_create_runner 之前赋初值（它会读）
        self._telegram_client: Optional[Any] = None
        # ★ P6-1：Runner factory —— 按 account_id 懒加载 + 缓存
        self._runners: Dict[str, MessengerRpaRunner] = {}
        self._runner = self._get_or_create_runner(primary.account_id)
        # 把 registry/pool 也挂到 runner 上，给未来 P6 的 acquire() 用
        try:
            self._runner._account_registry = self._account_registry
            self._runner._account_id = primary.account_id
        except Exception:
            pass
        self._log_startup_adb_account_warnings()
        # ★ P6-1：per-account 自适应节奏与退避状态
        self._consecutive_empty_map: Dict[str, int] = {}
        self._cur_iv_map: Dict[str, float] = {}
        self._last_run_map: Dict[str, Dict[str, Any]] = {}
        self._task: Optional[asyncio.Task] = None
        self._notif_task: Optional[asyncio.Task] = None
        # P7-1：leader lock + standby 等待 task
        self._leader_lock: Optional[Any] = None
        self._standby_task: Optional[asyncio.Task] = None
        self._stop_evt = asyncio.Event()
        self._trigger_evt = asyncio.Event()
        self._pause_until: float = 0.0
        self._started_at: float = 0.0
        self._last_run: Dict[str, Any] = {}
        self._consecutive_empty: int = 0
        self._last_tick_ts: float = 0.0
        self._last_notif_event_ts: float = 0.0
        self._notif_event_count: int = 0
        # P0-4: 设备掉线告警追踪
        self._consecutive_unhealthy: int = 0
        self._last_unhealthy_alert_ts: float = 0.0
        self._unhealthy_alert_sent_total: int = 0
        # 惰性初始化 WebhookNotifier（只有真的要告警时才构造）
        self._webhook_notifier: Optional[Any] = None
        # ★ P2-4：telegram_client 在 main.py 创建顺序上晚于本 service，
        # 通过 bind_telegram_client() 后置注入；此处已在上方初始化为 None。
        # ★ P2-6：SLA 监督循环状态
        self._sla_task: Optional[asyncio.Task] = None
        # 已推送过的超时 approval id（避免同一条重复告警）
        self._sla_alerted_ids: set = set()
        self._sla_alert_total: int = 0

    def configured_adb_serials(self) -> List[str]:
        """所有 account 中显式绑定的串号，有序去重（运维 /devices 用）。"""
        out: List[str] = []
        for ctx in self._account_registry.all_contexts():
            s = (ctx.adb_serial or "").strip()
            if s and s not in out:
                out.append(s)
        return out

    def adb_status_snapshot(self) -> Dict[str, Any]:
        """与启动告警、/status 共用的 ADB 视图；失败时 ``ok: false``。"""
        try:
            from src.integrations.line_rpa import adb_helpers as adb

            rows = adb.list_adb_device_rows()
        except Exception as ex:
            return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}
        state_by_serial = {s: st for s, st in rows}
        online_serials = [s for s, st in rows if st == "device"]
        accounts_out: List[Dict[str, Any]] = []
        for ctx in self._account_registry.all_contexts():
            serial = (ctx.adb_serial or "").strip()
            if not serial:
                accounts_out.append(
                    {
                        "account_id": ctx.account_id,
                        "label": ctx.label,
                        "adb_serial": "",
                        "adb_state": "auto" if online_serials else "no_device",
                        "ok": bool(online_serials),
                    }
                )
            else:
                st = state_by_serial.get(serial)
                accounts_out.append(
                    {
                        "account_id": ctx.account_id,
                        "label": ctx.label,
                        "adb_serial": serial,
                        "adb_state": st or "not_listed",
                        "ok": st == "device",
                    }
                )
        return {
            "ok": True,
            "rows": [{"serial": s, "state": st} for s, st in rows],
            "device_serials": online_serials,
            "accounts": accounts_out,
        }

    def _log_startup_adb_account_warnings(self) -> None:
        """检查各 account 的 ``adb_serial`` 在 ``adb devices`` 中是否为 *device*。"""
        snap = self.adb_status_snapshot()
        if not snap.get("ok"):
            logger.warning(
                "[messenger_rpa] ADB 启动检查失败: %s",
                snap.get("error", "unknown"),
            )
            return
        for row in snap.get("accounts", []):
            serial = (row.get("adb_serial") or "").strip()
            if not serial:
                continue
            if row.get("ok"):
                continue
            st = row.get("adb_state", "")
            aid = row.get("account_id", "")
            if st == "not_listed":
                logger.warning(
                    "[messenger_rpa] ADB: account=%s 绑定 serial=%s 未出现在 adb devices",
                    aid,
                    serial,
                )
            else:
                logger.warning(
                    "[messenger_rpa] ADB: account=%s serial=%s 状态=%s（需为 device；"
                    "unauthorized 请在手机上允许 USB 调试）",
                    aid,
                    serial,
                    st,
                )
        for row in snap.get("accounts", []):
            if (not (row.get("adb_serial") or "").strip()) and not row.get(
                "ok"
            ):
                logger.warning(
                    "[messenger_rpa] ADB: 当前无 state=device 的机子，"
                    "且存在未固定 adb_serial 的账号，自动选设备将失败",
                )
                break

    def _get_or_create_runner(self, account_id: str) -> MessengerRpaRunner:
        """按 account_id 懒加载 Runner；不同账号各持独立 state_store + cfg。

        P6-1：单进程内 N-account 真并发的核心入口。同一 account_id 只构造一次，
        后续 gather 复用。Runner 内部持有自己的 _chat_key_prefix / _calib_cache /
        _screen_wh_cache，跨账号不会互相覆盖。
        """
        r = self._runners.get(account_id)
        if r is not None:
            return r
        ctx = self._account_registry.get(account_id)
        if ctx is None:
            raise KeyError(f"unknown account_id: {account_id!r}")
        store = ctx.state_store()
        runner = MessengerRpaRunner(
            config_manager=self._cm,
            skill_manager=self._sm,
            messenger_rpa_cfg=ctx.merged_config(self._merged_cfg),
            state_store=store,
        )
        try:
            runner._account_registry = self._account_registry
            runner._account_id = ctx.account_id
        except Exception:
            pass
        if self._telegram_client is not None:
            try:
                runner.bind_telegram_client(self._telegram_client)
            except Exception:
                logger.debug("runner.bind_telegram_client 失败", exc_info=True)
        # W4-Runner：若 service 已持有 ContactHooks，同步给新建 runner
        _hooks = getattr(self, "_contact_hooks", None)
        if _hooks is not None:
            try:
                runner.set_contact_hooks(_hooks)
            except Exception:
                logger.debug("runner.set_contact_hooks 失败", exc_info=True)
        # Phase 1：若已构建 PortraitExtractor，同步给新建 runner
        _ext = getattr(self, "_portrait_extractor", None)
        if _ext is not None:
            try:
                runner.set_portrait_extractor(_ext)
            except Exception:
                logger.debug(
                    "runner.set_portrait_extractor 失败", exc_info=True
                )
        self._runners[account_id] = runner
        logger.info(
            "[messenger_rpa] Runner 为 account=%s 初始化完成 "
            "(serial=%s, chat_key_prefix=%s)",
            account_id, ctx.adb_serial or "(none)",
            ctx.merged_config(self._merged_cfg).get("chat_key_prefix"),
        )
        return runner

    async def _run_once_for_account(
        self, account_id: str, *, acquire_timeout: float = 180.0,
    ) -> Dict[str, Any]:
        """P6-1：跑单账号一轮，外层加 AccountPool.acquire 保证 adb 不冲突。

        - `acquire_timeout`：防止死锁；超时视为本轮 skip（不是错误）
        - 异常统一兜底，返回带 step 的 result dict
        """
        try:
            runner = self._get_or_create_runner(account_id)
        except Exception as ex:
            return {
                "ok": False, "step": "runner_init_failed",
                "error": f"{type(ex).__name__}: {ex}",
                "account_id": account_id,
            }
        pool = self._account_registry.pool
        try:
            async with pool.acquire(account_id, timeout=acquire_timeout):
                r = await runner.run_once()
        except asyncio.TimeoutError:
            return {
                "ok": False, "step": "pool_acquire_timeout",
                "error": f"timeout {acquire_timeout}s",
                "account_id": account_id,
            }
        except Exception as ex:
            logger.exception(
                "[messenger_rpa] account=%s run_once 异常", account_id,
            )
            return {
                "ok": False, "step": "run_once_exception",
                "error": f"{type(ex).__name__}: {ex}",
                "account_id": account_id,
            }
        r.setdefault("account_id", account_id)
        return r

    def _resolve_primary_account(self):
        """选 primary account（用于兼容单账号 runner 实例）。

        优先级：
        1. 第一个在 accounts 列表中的非 default（显式多账号配置时）
        2. default（未配 accounts 时）
        """
        ctxs = self._account_registry.all_contexts()
        if not ctxs:
            raise RuntimeError("AccountRegistry 为空（代码 bug）")
        non_default = [c for c in ctxs if c.account_id != "default"]
        return non_default[0] if non_default else ctxs[0]

    def bind_telegram_client(self, tg_client: Any) -> None:
        """由 main.py 在 telegram_client 构建完成后调用，把客户端注入所有 runner。

        P6-1：广播到 `_runners` 里的所有实例，后续新建的 runner 也会在
        `_get_or_create_runner` 里自动绑定。
        """
        self._telegram_client = tg_client
        for aid, r in list(self._runners.items()):
            try:
                r.bind_telegram_client(tg_client)
            except Exception:
                logger.debug(
                    "bind_telegram_client 到 runner(%s) 失败", aid, exc_info=True,
                )

    def set_contact_hooks(self, hooks: Optional[Any]) -> None:
        """main.py 在 contacts 子系统 bootstrap 后调用，把 hooks 广播给所有 runner。

        W4-Runner：保持 hooks 的引用，新建 runner 时也会自动继承。
        Phase 1：在 hooks 设置时尝试构建 PortraitExtractor（需要 store + ai_client）。
        """
        self._contact_hooks = hooks
        # Phase 1：基于 GatewayContactHooks 的 store 自动建 extractor
        try:
            self._maybe_build_portrait_extractor(hooks)
        except Exception:
            logger.debug("[messenger_rpa] portrait extractor 构建失败", exc_info=True)
        for aid, r in list(self._runners.items()):
            try:
                r.set_contact_hooks(hooks)
            except Exception:
                logger.debug(
                    "set_contact_hooks 到 runner(%s) 失败", aid, exc_info=True,
                )
            # Phase 1：联动同步 extractor
            ext = getattr(self, "_portrait_extractor", None)
            if ext is not None:
                try:
                    r.set_portrait_extractor(ext)
                except Exception:
                    logger.debug(
                        "set_portrait_extractor 到 runner(%s) 失败", aid,
                        exc_info=True,
                    )

    def _maybe_build_portrait_extractor(self, hooks: Optional[Any]) -> None:
        """Phase 1：从 GatewayContactHooks 取 store + 从 SkillManager 取 ai_client，
        构建 PortraitExtractor。任一依赖缺失则置 None（runner 跳过画像抽取）。

        config 项 messenger_rpa.portrait（可选）：
            enabled: true
            refresh_every_n_inbound: 5
            refresh_after_hours: 24
            max_inbound_messages_for_extract: 12
        """
        pcfg = (self._merged_cfg.get("portrait") or {})
        if not bool(pcfg.get("enabled", True)):
            self._portrait_extractor = None
            return
        gw = getattr(hooks, "_gw", None) if hooks is not None else None
        store = getattr(gw, "_store", None) if gw is not None else None
        ai = getattr(self._sm, "ai_client", None) if self._sm is not None else None
        if store is None or ai is None:
            self._portrait_extractor = None
            return
        try:
            from src.contacts.portrait_extractor import PortraitExtractor
            self._portrait_extractor = PortraitExtractor(
                store=store,
                ai_client=ai,
                refresh_every_n_inbound=int(
                    pcfg.get("refresh_every_n_inbound", 5) or 5
                ),
                refresh_after_hours=float(
                    pcfg.get("refresh_after_hours", 24) or 24
                ),
                max_inbound_messages_for_extract=int(
                    pcfg.get("max_inbound_messages_for_extract", 12) or 12
                ),
                ai_max_tokens=int(pcfg.get("ai_max_tokens", 400) or 400),
            )
            logger.info(
                "[messenger_rpa] PortraitExtractor 已就绪 (refresh_every_n=%d, "
                "refresh_after_hours=%.1f)",
                self._portrait_extractor._n,
                self._portrait_extractor._refresh_after_sec / 3600.0,
            )
        except Exception:
            logger.warning(
                "[messenger_rpa] PortraitExtractor 构建异常", exc_info=True
            )
            self._portrait_extractor = None

    # ── 默认与合并 ───────────────────────────────────────
    def _defaults(self) -> Dict[str, Any]:
        return {
            "enabled": False,
            "autostart": True,
            "adb_serial": "",
            "messenger_package": "com.facebook.orca",
            # 自适应轮询
            "interval_sec": 30.0,         # 基础间隔
            "min_interval_sec": 8.0,      # 有未读时下一轮最小间隔
            "max_interval_sec": 300.0,    # 连续空跑后最大间隔
            "backoff_multiplier": 1.5,    # 每次空跑递增倍率
            # 单次 run
            "max_inbox_per_run": 1,       # 一次只处理 N 条未读
            "send_to_chat_inbox_row_cap": 16,
            "send_to_chat_inbox_scroll_attempts": 4,
            # send_to_chat_name：上滑收件箱列表（相对屏高比例 + 时长 ms）
            "send_to_chat_scroll_y1_ratio": 0.66,
            "send_to_chat_scroll_y2_ratio": 0.44,
            "send_to_chat_scroll_duration_ms": 380,
            # send_to_chat_name：预览子串弱匹配（0=关，默认关；误触会开错人）
            "send_to_chat_preview_match_min_len": 0,
            # run_once：首屏 0 未读时上滑收件箱再 Vision（0=关闭）
            "run_once_inbox_scroll_if_zero_unread_attempts": 3,
            "reply_mode": "auto",         # auto | approve | off
            # AdbKeyboard
            "use_adb_keyboard": True,
            "adb_keyboard_ime": "com.android.adbkeyboard/.AdbIME",
            "adb_keyboard_package": "com.android.adbkeyboard",
            # 调试
            "debug_screenshot_dir": "tmp_messenger_rpa",
            "screencap": {
                "max_retries": 6,
                "heal_on_transient_fail": True,
                "allow_global_reconnect": True,
            },
            "recent_runs_buffer": 500,
            # chat_key 命名
            "chat_key_prefix": "messenger_rpa",
            # vision 覆盖（空则用全局 vision 段）
            "vision": {},
            # ★ 设备健康守护
            "auto_reconnect": True,
            "auto_wake": True,
            "auto_unlock_swipe": True,
            "device_max_attempts": 3,
            # ★ 通知监听（主动唤起）
            "notif_watch_enabled": True,
            "notif_poll_ms": 700,
            # ★ 自适应坐标校准
            "auto_calibrate": True,
        }

    def _merged(self) -> Dict[str, Any]:
        d = self._defaults()
        for k, v in (self._cfg or {}).items():
            if k == "screencap" and isinstance(v, dict) and isinstance(
                d.get("screencap"), dict,
            ):
                d[k] = {**d["screencap"], **v}
            else:
                d[k] = v
        return d

    # ── 生命周期 ────────────────────────────────────
    async def start(self) -> bool:
        if self._task and not self._task.done():
            logger.info("MessengerRpaService 已在运行，忽略 start()")
            return False
        if not self._merged_cfg.get("enabled"):
            logger.info("MessengerRpaService enabled=False，不启动")
            return False
        if not self._merged_cfg.get("autostart"):
            logger.info("MessengerRpaService autostart=False，等待外部 trigger")
            return False

        # ── P7-1：leader lock 接入 ────────────────────
        # 若 ha.enabled=true，必须先抢到锁才能进入真正的 RPA 循环。
        # 未抢到则进入 standby 等待循环（低频 peek，发现原 leader 断开立即抢占）。
        ha_cfg = (self._merged_cfg.get("ha") or {})
        self._leader_lock = None
        if bool(ha_cfg.get("enabled", False)):
            try:
                from src.integrations.ha import LeaderLock
                self._leader_lock = LeaderLock.from_config(ha_cfg)
                ttl = float(ha_cfg.get("ttl_sec", 30) or 30)
                hb = float(ha_cfg.get("heartbeat_sec", 10) or 10)
                ok = await self._leader_lock.acquire(
                    ttl_sec=ttl, heartbeat_sec=hb,
                    extra={"pid": os.getpid(), "startup_ts": time.time()},
                )
                if not ok:
                    logger.warning(
                        "[ha] P7-1 未抢到 leader lock, 进入 standby 等待循环"
                    )
                    self._standby_task = asyncio.create_task(
                        self._standby_loop(ttl, hb),
                        name="messenger_rpa_standby",
                    )
                    self._started_at = time.time()
                    return True  # standby 也算"已启动"，但不跑主循环
                logger.info(
                    "[ha] P7-1 leader acquired token=%d",
                    self._leader_lock.state.fencing_token,
                )
            except Exception as ex:
                logger.exception("[ha] P7-1 leader_lock 异常：降级为非 HA 启动 %s", ex)
                self._leader_lock = None

        self._stop_evt.clear()
        self._trigger_evt.clear()
        self._started_at = time.time()
        self._task = asyncio.create_task(self._loop(), name="messenger_rpa_loop")
        # ★ 主动唤起：通知监听器（与轮询并行）
        if bool(self._merged_cfg.get("notif_watch_enabled", True)):
            self._notif_task = asyncio.create_task(
                self._notif_loop(), name="messenger_rpa_notif"
            )
        # ★ P2-6：SLA 监督循环（只有开启时启动）
        _sla = (self._merged_cfg.get("approval_sla") or {})
        if bool(_sla.get("enabled", True)):
            self._sla_task = asyncio.create_task(
                self._sla_loop(), name="messenger_rpa_sla",
            )
        logger.info("MessengerRpaService 已启动")
        return True

    async def stop(self) -> None:
        self._stop_evt.set()
        self._trigger_evt.set()
        tasks = [self._task, self._notif_task, self._sla_task]
        if getattr(self, "_standby_task", None):
            tasks.append(self._standby_task)
        for t in tasks:
            if t:
                try:
                    await asyncio.wait_for(t, timeout=5.0)
                except asyncio.TimeoutError:
                    logger.warning("MessengerRpaService 任务停止超时，取消")
                    t.cancel()
                except Exception:
                    logger.exception("MessengerRpaService 任务停止异常")
        self._task = None
        self._notif_task = None
        self._sla_task = None
        self._standby_task = None
        # P7-1：释放 leader lock
        if getattr(self, "_leader_lock", None) is not None:
            try:
                await self._leader_lock.release()
            except Exception:
                logger.debug("[ha] release leader_lock failed", exc_info=True)
            self._leader_lock = None

    async def _standby_loop(self, ttl: float, hb: float) -> None:
        """P7-1：standby 节点的轻量等待循环。

        周期性尝试抢锁；一旦成功，自动启动真正的 RPA 主循环。
        """
        from src.integrations.ha import LeaderLock
        poll_interval = max(2.0, min(ttl / 2.0, 10.0))
        while not self._stop_evt.is_set():
            try:
                await asyncio.sleep(poll_interval)
                if self._leader_lock is None:
                    return
                ok = await self._leader_lock.acquire(
                    ttl_sec=ttl, heartbeat_sec=hb,
                    extra={"pid": os.getpid(), "promoted_ts": time.time()},
                )
                if ok:
                    logger.warning(
                        "[ha] P7-1 standby → LEADER promoted token=%d",
                        self._leader_lock.state.fencing_token,
                    )
                    # 启动真正的主循环
                    self._stop_evt.clear()
                    self._trigger_evt.clear()
                    self._task = asyncio.create_task(
                        self._loop(), name="messenger_rpa_loop",
                    )
                    if bool(self._merged_cfg.get("notif_watch_enabled", True)):
                        self._notif_task = asyncio.create_task(
                            self._notif_loop(), name="messenger_rpa_notif",
                        )
                    _sla = (self._merged_cfg.get("approval_sla") or {})
                    if bool(_sla.get("enabled", True)):
                        self._sla_task = asyncio.create_task(
                            self._sla_loop(), name="messenger_rpa_sla",
                        )
                    return
            except asyncio.CancelledError:
                return
            except Exception:
                logger.debug("[ha] standby loop iter 异常", exc_info=True)

    def pause_for(self, seconds: float) -> None:
        self._pause_until = max(self._pause_until, time.time() + max(seconds, 0))

    def resume(self) -> None:
        self._pause_until = 0.0
        self._trigger_evt.set()

    async def trigger_once(
        self, account_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """从 Web 立即触发一次 run（即使在 paused 也走）。

        P6-1：可传 ``account_id`` 精确触发某账号；为 None 时：
        - 单账号：走旧 self._runner（零回归）
        - 多账号：触发 primary
        """
        self._trigger_evt.set()
        try:
            if account_id is None:
                if self._account_registry.size() > 1 or (
                    self._account_registry.size() == 1
                    and self._primary_account_id != "default"
                ):
                    r = await self._run_once_for_account(
                        self._primary_account_id,
                    )
                else:
                    r = await self._runner.run_once()
            else:
                r = await self._run_once_for_account(account_id)
            self._last_run = r
            if account_id:
                self._last_run_map[account_id] = r
            return r
        except Exception as ex:
            logger.exception("trigger_once 异常")
            return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}

    def status(self) -> Dict[str, Any]:
        try:
            send_stats = self._state.get_send_stats()
        except Exception:
            send_stats = {}
        # ★ P2-6：SLA 统计（不影响响应延迟，一次 SELECT）
        sla_stats: Dict[str, Any] = {}
        try:
            _sla_cfg = (self._merged_cfg.get("approval_sla") or {})
            thr = int(_sla_cfg.get("threshold_sec", 600) or 600)
            sla_stats = self._state.pending_sla_stats(threshold_sec=thr)
        except Exception:
            logger.debug("status.pending_sla_stats 异常", exc_info=True)
        # ★ P3-1：风控状态
        risk_state: Dict[str, Any] = {}
        try:
            risk_state = self._state.get_risk_state()
        except Exception:
            logger.debug("status.get_risk_state 异常", exc_info=True)
        # ★ P4-3：节奏学习（每次 status 调用都算一次；数据量小可接受）
        pace: Dict[str, Any] = {}
        try:
            pl_cfg = (self._merged_cfg.get("pace_learning") or {})
            if pl_cfg.get("enabled", True):
                pace = self._state.pace_check(
                    min_samples=int(pl_cfg.get("min_samples", 20) or 20),
                    median_multiplier=float(
                        pl_cfg.get("throttle_multiplier", 1.5) or 1.5
                    ),
                    block_multiplier=float(
                        pl_cfg.get("block_multiplier", 2.5) or 2.5
                    ),
                )
        except Exception:
            logger.debug("status.pace_check 异常", exc_info=True)
        # ★ P4-7：信用分分布
        credit: Dict[str, Any] = {}
        try:
            credit = self._state.credit_stats()
        except Exception:
            logger.debug("status.credit_stats 异常", exc_info=True)
        adb_snap: Dict[str, Any] = {}
        try:
            adb_snap = self.adb_status_snapshot()
        except Exception:
            logger.debug("status.adb_status_snapshot 异常", exc_info=True)
        return {
            "running": bool(self._task and not self._task.done()),
            "notif_running": bool(
                self._notif_task and not self._notif_task.done()
            ),
            "sla_running": bool(
                self._sla_task and not self._sla_task.done()
            ),
            "notif_event_count": self._notif_event_count,
            "last_notif_event_ts": self._last_notif_event_ts,
            "started_at": self._started_at,
            "last_tick_ts": self._last_tick_ts,
            "paused_until": self._pause_until,
            "consecutive_empty": self._consecutive_empty,
            "consecutive_unhealthy": self._consecutive_unhealthy,
            "last_unhealthy_alert_ts": self._last_unhealthy_alert_ts,
            "unhealthy_alert_sent_total": self._unhealthy_alert_sent_total,
            "send_counters": send_stats,
            "approval_sla": sla_stats,
            "sla_alert_sent_total": self._sla_alert_total,
            "risk": risk_state,
            "pace": pace,
            "credit": credit,
            # ★ P5-1：account registry 概览（运维 UI 用）
            "accounts": self._account_registry.stats(),
            "last_run": dict(self._last_run) if self._last_run else {},
            # ★ P6-1：多账号 per-account 最近一次 run + 节奏
            "per_account": {
                aid: {
                    "last_run": dict(self._last_run_map.get(aid, {})),
                    "cur_iv_sec": self._cur_iv_map.get(aid, 0.0),
                    "consecutive_empty": self._consecutive_empty_map.get(aid, 0),
                }
                for aid in self._account_registry.account_ids()
            },
            "adb": adb_snap,
            "config": {
                k: v
                for k, v in self._merged_cfg.items()
                if k not in ("vision",)  # 避免泄露 api_key
            },
        }

    # ── P2-6：SLA 监督循环 ────────────────────────────
    async def _sla_loop(self) -> None:
        """周期扫描 pending 审批，超时单条推 TG（已推过 dedup 不重复）。"""
        sla_cfg = (self._merged_cfg.get("approval_sla") or {})
        poll_sec = max(float(sla_cfg.get("poll_sec", 60.0) or 60.0), 10.0)
        threshold = int(sla_cfg.get("threshold_sec", 600) or 600)
        logger.info(
            "[messenger_rpa] SLA 监督启动：threshold=%ss poll=%.1fs",
            threshold, poll_sec,
        )
        while not self._stop_evt.is_set():
            try:
                stats = self._state.pending_sla_stats(threshold_sec=threshold)
                overdue_ids = stats.get("overdue_ids") or []
                new_overdue = [
                    i for i in overdue_ids if i not in self._sla_alerted_ids
                ]
                if new_overdue:
                    # 清理掉已 decided 的旧 id，避免 set 无限增长
                    pending_set = set(overdue_ids)
                    self._sla_alerted_ids &= pending_set
                    await self._notify_sla_overdue(new_overdue, threshold)
                    self._sla_alerted_ids.update(new_overdue)
                    self._sla_alert_total += len(new_overdue)
            except Exception:
                logger.debug("SLA 扫描异常", exc_info=True)
            try:
                await asyncio.wait_for(self._stop_evt.wait(), timeout=poll_sec)
            except asyncio.TimeoutError:
                pass

    async def _notify_sla_overdue(
        self, ids: list, threshold_sec: int
    ) -> None:
        """单次推送超时 approval 到 TG + webhook。"""
        tg = self._telegram_client
        esc_cfg = (self._merged_cfg.get("escalation") or {})
        target_chat = str(
            esc_cfg.get("telegram_chat_id")
            or ((self._cm.config or {}).get("telegram", {}) or {}).get("admin_chat_id")
            or ""
        ).strip()

        for aid in ids[:10]:  # 一轮最多 10 条，避免刷屏
            try:
                item = self._state.get_approval(int(aid))
                if not item or item.get("status") != "pending":
                    continue
                chat_name = item.get("chat_name") or "?"
                peer_text = (item.get("peer_text") or "")[:150]
                reply_text = (item.get("reply_text") or "")[:150]

                text = (
                    f"⏰ Messenger 审批超时（>{threshold_sec}s）\n"
                    f"#{aid} 👤 {chat_name}\n"
                    f"📝 对方: {peer_text}\n"
                    f"🤖 草稿: {reply_text}"
                )

                if tg is not None and hasattr(tg, "client") and target_chat:
                    try:
                        cid = int(target_chat) if str(target_chat).lstrip("-").isdigit() \
                            else target_chat
                        await tg.client.send_message(chat_id=cid, text=text)
                    except Exception as ex:
                        logger.warning("SLA 推送 TG 失败: %s", ex)
                logger.info("[messenger_rpa] SLA 超时告警已发送 id=%s", aid)
            except Exception:
                logger.debug("SLA 单条推送异常", exc_info=True)

    @property
    def state_store(self):
        return self._state

    def check_text_input(self) -> Dict[str, Any]:
        """检查当前 adb_serial 的文本输入路径可用性。"""
        serial = (self._merged_cfg.get("adb_serial") or "").strip()
        if not serial:
            return {"ok": False, "error": "adb_serial 未配置"}
        try:
            from src.integrations.messenger_rpa.text_input import (
                precheck_text_input,
            )
            return {
                "ok": True,
                **precheck_text_input(
                    serial,
                    adb_keyboard_package=(
                        self._merged_cfg.get("adb_keyboard_package")
                        or "com.android.adbkeyboard"
                    ),
                ),
            }
        except Exception as ex:
            return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}

    async def calibrate_now(self, *, force: bool = False) -> Dict[str, Any]:
        """触发当前设备的 inbox 坐标自动校准。"""
        try:
            return await self._runner.calibrate_now(force=force)
        except Exception as ex:
            logger.exception("calibrate_now 异常")
            return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}

    async def send_approved_now(self, approval_id: int) -> Dict[str, Any]:
        """把已批准的回复真发出去：开 Messenger → 找会话 → tap → 输入 → 点 send。

        v0.1：必须 chat 仍然在 inbox 顶部最近活跃（vision 选名）；如果对方又来了
        新消息则更稳妥：refresh inbox → 用 chat_name 在新一轮 unread 里精确匹配。
        """
        appr = self._state.get_approval(int(approval_id))
        if not appr:
            return {"ok": False, "error": f"approval #{approval_id} not found"}
        if appr["status"] not in ("approved",):
            return {
                "ok": False,
                "error": f"approval #{approval_id} status={appr['status']}, need approved",
            }
        # 实际发送沿用 runner._send_reply 的 path——但要先把 RPA 推到目标 thread
        # 简化：让 runner 重新跑 run_once，并把目标会话名传下去
        try:
            r = await self._runner.send_to_chat_name(
                chat_name=str(appr["chat_name"] or ""),
                reply_text=str(appr["reply_text"] or ""),
            )
            ok = bool(r.get("ok"))
            self._state.mark_approval_sent(
                int(approval_id),
                ok=ok,
                send_error=str(r.get("error") or ""),
            )
            return {"requested": True, **r}
        except Exception as ex:
            logger.exception("send_approved_now 失败")
            self._state.mark_approval_sent(
                int(approval_id), ok=False, send_error=f"{type(ex).__name__}:{ex}"
            )
            return {
                "requested": True, "ok": False,
                "error": f"{type(ex).__name__}:{ex}",
            }

    async def send_to_chat_name_for_account(
        self,
        account_id: str,
        *,
        chat_name: str,
        reply_text: str,
        acquire_timeout: float = 180.0,
    ) -> Dict[str, Any]:
        """指定账号：开其 adb_serial 对应设备 → 找会话名 → 发送固定文本（不经 LLM）。

        用于双机互发联调、运营代发；与 ``MessengerRpaRunner.send_to_chat_name`` 等价，
        外层加 ``AccountPool.acquire`` 避免多账号抢同一 adb 时段。
        """
        if not (chat_name or "").strip():
            return {
                "ok": False,
                "step": "send_to:bad_args",
                "error": "chat_name 必填",
                "account_id": account_id,
            }
        if not (reply_text or "").strip():
            return {
                "ok": False,
                "step": "send_to:bad_args",
                "error": "reply_text 必填",
                "account_id": account_id,
            }
        try:
            runner = self._get_or_create_runner(account_id)
        except Exception as ex:
            return {
                "ok": False,
                "step": "send_to:runner_init_failed",
                "error": f"{type(ex).__name__}: {ex}",
                "account_id": account_id,
            }
        pool = self._account_registry.pool
        try:
            async with pool.acquire(account_id, timeout=acquire_timeout):
                r = await runner.send_to_chat_name(
                    chat_name=str(chat_name).strip(),
                    reply_text=str(reply_text).strip(),
                )
        except asyncio.TimeoutError:
            return {
                "ok": False,
                "step": "send_to:pool_acquire_timeout",
                "error": f"timeout {acquire_timeout}s",
                "account_id": account_id,
            }
        except Exception as ex:
            logger.exception(
                "[messenger_rpa] send_to_chat_name_for_account account=%s",
                account_id,
            )
            return {
                "ok": False,
                "step": "send_to:exception",
                "error": f"{type(ex).__name__}: {ex}",
                "account_id": account_id,
            }
        r.setdefault("account_id", account_id)
        return r

    # ── 主循环 ──────────────────────────────────────
    async def _loop(self) -> None:
        """主轮询循环。

        - 单账号路径（`registry.size()==1`）：与旧实现完全一致，零回归。
        - 多账号路径：每轮 gather 所有账号的 `_run_once_for_account`；
          `AccountPool` 的 Semaphore 控制真并发上限，Lock 防 adb 竞争。
          每账号独立维护 `consecutive_empty / cur_iv`；全局 sleep 取各账号
          iv 的最小值（让忙账号推动节奏、闲账号自然退避）。
        """
        base_iv = float(self._merged_cfg.get("interval_sec", 30.0))
        min_iv = float(self._merged_cfg.get("min_interval_sec", 8.0))
        max_iv = float(self._merged_cfg.get("max_interval_sec", 300.0))
        mult = float(self._merged_cfg.get("backoff_multiplier", 1.5))

        ctxs = self._account_registry.all_contexts()
        # P6-1：多账号判定——非 default 或 >1 个 account 时走 gather 路径
        multi_mode = (
            len(ctxs) > 1
            or (len(ctxs) == 1 and ctxs[0].account_id != "default")
        )
        account_ids = [c.account_id for c in ctxs]
        for aid in account_ids:
            self._cur_iv_map.setdefault(aid, base_iv)
            self._consecutive_empty_map.setdefault(aid, 0)

        cur_iv = base_iv  # 单账号兼容路径用
        try:
            while not self._stop_evt.is_set():
                self._last_tick_ts = time.time()

                # 暂停态：仅在被显式 trigger 时跑一次
                paused = self._pause_until > time.time()
                if paused and not self._trigger_evt.is_set():
                    sleep_iv = (
                        min(self._cur_iv_map.values()) if multi_mode
                        else cur_iv
                    )
                    await self._sleep_or_trigger(min(5.0, sleep_iv))
                    continue
                self._trigger_evt.clear()

                if not multi_mode:
                    # ── 单账号旧路径（100% 兼容）──
                    try:
                        r = await self._runner.run_once()
                        self._last_run = r
                        if r.get("ok") and r.get("step") == "sent":
                            cur_iv = min_iv
                            self._consecutive_empty = 0
                        elif r.get("step") in ("no_unread", "duplicate_skip"):
                            self._consecutive_empty += 1
                            cur_iv = min(cur_iv * mult, max_iv)
                        else:
                            self._consecutive_empty += 1
                            cur_iv = min(max(cur_iv, base_iv) * 1.2, max_iv)
                        self._track_device_health(r)
                    except Exception:
                        logger.exception(
                            "run_once 抛异常（不应该发生，已被 runner 兜底）"
                        )
                        cur_iv = min(cur_iv * mult, max_iv)
                    await self._sleep_or_trigger(cur_iv)
                    continue

                # ── 多账号 gather 路径 ──
                coros = [self._run_once_for_account(aid) for aid in account_ids]
                try:
                    results = await asyncio.gather(
                        *coros, return_exceptions=True,
                    )
                except Exception:
                    logger.exception("_loop gather 顶层异常（不应出现）")
                    results = []

                for aid, r in zip(account_ids, results):
                    if isinstance(r, BaseException):
                        logger.warning(
                            "[messenger_rpa] account=%s gather 异常: %s",
                            aid, r,
                        )
                        r = {
                            "ok": False, "step": "gather_exception",
                            "error": f"{type(r).__name__}: {r}",
                            "account_id": aid,
                        }
                    self._last_run_map[aid] = r
                    step = str(r.get("step") or "")
                    iv = self._cur_iv_map.get(aid, base_iv)
                    if r.get("ok") and step == "sent":
                        iv = min_iv
                        self._consecutive_empty_map[aid] = 0
                    elif step in ("no_unread", "duplicate_skip"):
                        self._consecutive_empty_map[aid] = (
                            self._consecutive_empty_map.get(aid, 0) + 1
                        )
                        iv = min(iv * mult, max_iv)
                    else:
                        self._consecutive_empty_map[aid] = (
                            self._consecutive_empty_map.get(aid, 0) + 1
                        )
                        iv = min(max(iv, base_iv) * 1.2, max_iv)
                    self._cur_iv_map[aid] = iv
                    # primary account 的结果同时写入 self._last_run，
                    # 保证旧 /status API 兼容
                    if aid == self._primary_account_id:
                        self._last_run = r
                        if r.get("ok") and step == "sent":
                            self._consecutive_empty = 0
                        elif step in (
                            "no_unread", "duplicate_skip",
                            "gather_exception", "runner_init_failed",
                            "pool_acquire_timeout", "run_once_exception",
                        ):
                            self._consecutive_empty += 1
                        self._track_device_health(r)

                # 全局节奏：取所有账号 iv 的最小值 → 最忙账号牵引整体
                global_iv = min(self._cur_iv_map.values()) if self._cur_iv_map else base_iv
                await self._sleep_or_trigger(global_iv)
        except asyncio.CancelledError:
            logger.info("MessengerRpaService _loop 被取消")
            raise
        except Exception:
            logger.exception("MessengerRpaService _loop 异常退出")

    # ── P0-4: 设备掉线告警 ────────────────────────────
    def _track_device_health(self, r: Dict[str, Any]) -> None:
        """观察单次 run 的 step，累计 device_unhealthy 次数并按阈值 + 冷却告警。

        step 判定：runner 在设备不可达/屏灭未解锁时返回 step='device_unhealthy'
        （runner._resolve_serial / device_health.ensure_device_ready 的组合结果）
        """
        step = str(r.get("step") or "")
        if step == "device_unhealthy" or step.startswith("device_unhealthy"):
            self._consecutive_unhealthy += 1
            threshold = int(
                self._merged_cfg.get("device_unhealthy_alert_threshold", 3)
            )
            cooldown = float(
                self._merged_cfg.get(
                    "device_unhealthy_alert_cooldown_sec", 1800.0
                )
            )
            now = time.time()
            if (
                self._consecutive_unhealthy >= threshold
                and (now - self._last_unhealthy_alert_ts) >= cooldown
            ):
                self._last_unhealthy_alert_ts = now
                self._unhealthy_alert_sent_total += 1
                try:
                    self._fire_unhealthy_alert(r)
                except Exception:
                    logger.debug("fire_unhealthy_alert 失败", exc_info=True)
        else:
            if self._consecutive_unhealthy > 0:
                logger.info(
                    "[messenger_rpa] 设备恢复健康（此前连续 unhealthy %d 次）",
                    self._consecutive_unhealthy,
                )
            self._consecutive_unhealthy = 0

    def _fire_unhealthy_alert(self, last_run: Dict[str, Any]) -> None:
        """构造告警 payload 并走：日志（必定） + webhook（若配置启用）。

        同时经由模块 logger 与顶层 ai_chat_assistant logger 两处输出，
        因为顶层日志配置只 attach 了 ai_chat_assistant 家族的 handler。
        """
        serial = str(self._merged_cfg.get("adb_serial") or "")
        msg = (
            f"Messenger RPA 设备连续 {self._consecutive_unhealthy} 次 "
            f"device_unhealthy: serial={serial!r} "
            f"last_error={last_run.get('error', '')[:200]!r}"
        )
        logger.error("[ALERT] %s", msg)
        try:
            logging.getLogger("ai_chat_assistant").error(
                "[messenger_rpa ALERT] %s", msg
            )
        except Exception:
            pass
        # 尝试推 webhook（惰性构造）
        try:
            if self._webhook_notifier is None:
                from src.utils.webhook import WebhookNotifier
                wh_cfg = self._cm.config.get("webhook", {}) or {}
                if wh_cfg.get("enabled"):
                    self._webhook_notifier = WebhookNotifier(wh_cfg)
                else:
                    self._webhook_notifier = False  # 标记为不可用
            notifier = self._webhook_notifier
            if notifier and notifier is not False:
                notifier.notify(
                    "messenger_rpa.device_unhealthy",
                    {
                        "action": "device_unhealthy",
                        "target": serial or "(unspecified)",
                        "consecutive": self._consecutive_unhealthy,
                        "last_step": last_run.get("step"),
                        "last_error": last_run.get("error", "")[:500],
                        "message": msg,
                    },
                )
        except Exception:
            logger.debug("webhook 推送 device_unhealthy 告警失败", exc_info=True)

    async def _sleep_or_trigger(self, seconds: float) -> None:
        """睡 seconds 秒，或被 stop/trigger 提前唤醒。"""
        try:
            await asyncio.wait_for(self._trigger_evt.wait(), timeout=max(0.1, seconds))
        except asyncio.TimeoutError:
            pass

    # ── 通知监听：新消息到达时立刻 trigger，避免 30s 轮询延迟 ──
    async def _notif_loop(self) -> None:
        """长连接监听 com.facebook.orca 通知 dump diff，新通知 → 立刻唤醒主循环。"""
        try:
            from src.integrations.messenger_rpa.notification_watcher import (
                MessengerNotificationWatcher,
            )
        except ImportError:
            logger.warning("notification_watcher 不可用，跳过 notif_loop")
            return

        serial = (self._merged_cfg.get("adb_serial") or "").strip()
        if not serial:
            logger.info("adb_serial 为空，notif_loop 不启动")
            return

        target_user = self._merged_cfg.get("adb_user_id")
        target_user = int(target_user) if target_user is not None else None
        poll_ms = int(self._merged_cfg.get("notif_poll_ms", 700))

        # 失败重连（设备掉线时不能让 notif_loop 自己挂掉）
        backoff = 2.0
        while not self._stop_evt.is_set():
            try:
                w = MessengerNotificationWatcher(
                    serial,
                    target_pkg=str(
                        self._merged_cfg.get("messenger_package", "com.facebook.orca")
                    ),
                    target_user=target_user,
                )
                async for evt in w.watch(poll_ms=poll_ms):
                    if self._stop_evt.is_set():
                        w.stop()
                        break
                    if evt.type == "new":
                        self._last_notif_event_ts = time.time()
                        self._notif_event_count += 1
                        logger.info(
                            "[notif_loop] 新消息通知 → trigger run_once "
                            "user=%d key=%s",
                            evt.user_id, evt.key[:80],
                        )
                        self._trigger_evt.set()
                # watch 正常退出（max_idle 超时或 stop），重启
                backoff = 2.0
            except asyncio.CancelledError:
                logger.info("[notif_loop] 被取消，退出")
                return
            except Exception:
                logger.exception("[notif_loop] 异常，%.1fs 后重启", backoff)
                try:
                    await asyncio.wait_for(self._stop_evt.wait(), timeout=backoff)
                    return
                except asyncio.TimeoutError:
                    backoff = min(backoff * 1.6, 60.0)
