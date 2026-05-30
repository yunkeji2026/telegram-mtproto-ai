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

from src.integrations.line_rpa.adb_helpers import get_device_lock
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
        self._seed_strategy_runtime_config()
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
        # ★ W2-D2.1：deferred 队列独立 drain loop（陪护模式）
        self._drain_task: Optional[asyncio.Task] = None
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
        # ★ 账号级暂停 + UI 安全状态
        self._account_pause_until: Dict[str, float] = {}
        self._account_ui_unsafe: set = set()
        # ★ 跨账号协调器：共享画像 + 同用户聊天互斥
        try:
            from src.integrations.messenger_rpa.cross_account import CrossAccountCoordinator
            self._coordinator = CrossAccountCoordinator()
        except Exception:
            logger.warning("CrossAccountCoordinator 构建失败，跨账号功能禁用", exc_info=True)
            self._coordinator = None
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
        # Step-2 migration: auto-seed PersonaManager with reply_profiles on startup
        self._mrpa_pm_import_count: int = self._import_reply_profiles_to_pm()
        # P1: warm-up PM chat bindings from durable SQLite chat_persona_overrides
        self._mrpa_pm_binding_count: int = self._warmup_pm_chat_bindings()

    def _import_reply_profiles_to_pm(self) -> int:
        """One-way idempotent import: messenger_rpa.reply_profiles.profiles → PM profile store.

        Each profile's ``persona`` sub-dict is upserted as a PM profile with the same ``id``.
        Profiles imported this way carry ``_mrpa_source: true`` so the status API can count them.
        Existing profiles with the same id are refreshed from config (config remains source-of-truth
        until the operator explicitly edits via /personas).

        Returns the number of profiles upserted.
        """
        try:
            from src.utils.persona_manager import PersonaManager
            pm = PersonaManager.get_instance()
            rp_cfg = self._merged_cfg.get("reply_profiles") or {}
            if not isinstance(rp_cfg, dict):
                return 0
            profiles = rp_cfg.get("profiles") or []
            if not isinstance(profiles, list):
                return 0
            n = 0
            for p in profiles:
                if not isinstance(p, dict):
                    continue
                pid = str(p.get("id") or p.get("name") or "").strip()
                if not pid:
                    continue
                persona_data = p.get("persona")
                if not isinstance(persona_data, dict) or not persona_data:
                    continue
                # P2-A: If PM already has this profile WITHOUT _mrpa_source, the operator
                # has edited it via /personas — don't overwrite their changes on restart.
                existing = pm.get_persona_by_id(pid)
                if existing is not None and not existing.get("_mrpa_source"):
                    logger.debug(
                        "[messenger_rpa] skip import profile=%r: operator-owned in PM",
                        pid,
                    )
                    continue
                entry = {**persona_data, "id": pid, "_mrpa_source": True}
                pm.upsert_profile(pid, entry, _track_history=False)
                n += 1
            if n:
                logger.info(
                    "[messenger_rpa] reply_profiles \u5c06 %d \u4e2a profiles \u5bfc\u5165 PersonaManager\uff08\u53ef\u5728 /personas \u7f16\u8f91\uff09",
                    n,
                )
            return n
        except Exception:
            logger.debug("[messenger_rpa] _import_reply_profiles_to_pm \u5f02\u5e38", exc_info=True)
            return 0

    def _warmup_pm_chat_bindings(self) -> int:
        """Restore PM chat bindings from SQLite messenger_rpa_chat_persona_overrides on startup.

        Without this, PM chat bindings are lost on restart and the runner falls back to
        auto-matching (which may pick a different profile than the operator intended).

        Runs after _import_reply_profiles_to_pm so that PM profiles already exist when
        bind_chat_persona_by_profile_id is called.

        Returns total number of bindings seeded into PM.
        """
        try:
            from src.utils.persona_manager import PersonaManager
            from src.integrations.messenger_rpa.state_store import mrpa_chat_cid
            pm = PersonaManager.get_instance()
            total = 0
            skipped = 0
            for ctx in self._account_registry.all_contexts():
                try:
                    store = ctx.state_store()
                    mc = ctx.merged_config(self._merged_cfg)
                    prefix = mc.get("chat_key_prefix") or "messenger_rpa"
                    overrides = store.list_chat_persona_overrides()
                    for ov in overrides:
                        chat_name = str(ov.get("chat_name") or "").strip()
                        profile_id = str(ov.get("reply_profile_id") or "").strip()
                        if not chat_name or not profile_id:
                            continue
                        cid = mrpa_chat_cid(chat_name, prefix)
                        ok = pm.bind_chat_persona_by_profile_id(str(cid), profile_id)
                        if ok:
                            total += 1
                        else:
                            skipped += 1
                            logger.debug(
                                "[messenger_rpa] warmup skip: profile_id=%r not in PM "
                                "(chat=%r account=%s)",
                                profile_id, chat_name, ctx.account_id,
                            )
                except Exception:
                    logger.debug(
                        "[messenger_rpa] warmup failed for account=%s", ctx.account_id,
                        exc_info=True,
                    )
            if total or skipped:
                logger.info(
                    "[messenger_rpa] PM \u70ed\u542f\u52a8\u5b8c\u6210: \u7ed1\u5b9a %d \u4e2a chat persona"
                    "\uff08\u8df3\u8fc7 %d \u4e2a profile \u672a\u5bfc\u5165\uff09",
                    total, skipped,
                )
            return total
        except Exception:
            logger.debug("[messenger_rpa] _warmup_pm_chat_bindings \u5f02\u5e38", exc_info=True)
            return 0

    def _seed_strategy_runtime_config(self) -> None:
        """Mirror local account/persona config into the strategy DB tables.

        This is intentionally idempotent. It lets the existing YAML remain the
        source of truth during migration while new backend workers can read the
        same account/persona data from SQLite.
        """
        try:
            for ctx in self._account_registry.all_contexts():
                store = ctx.state_store()
                send_stats = {}
                try:
                    send_stats = store.get_send_stats()
                except Exception:
                    send_stats = {}
                store.upsert_strategy_account(
                    account_id=ctx.account_id,
                    label=ctx.label,
                    status=ctx.status,
                    supported_languages=list(ctx.supported_languages or []),
                    supported_customer_types=list(
                        ctx.supported_customer_types or []
                    ),
                    persona_ids=list(ctx.persona_ids or []),
                    health_score=float(ctx.health_score or 100),
                    current_load=int(ctx.current_load or 0),
                    daily_send_count=int(send_stats.get("count") or 0),
                    max_daily_send=int(ctx.max_daily_send or 200),
                    metadata={
                        "adb_serial": ctx.adb_serial,
                        "reply_profile_id": ctx.reply_profile_id,
                        "device_alias": ctx.device_alias,
                        "login_account": ctx.login_account,
                    },
                )
            profiles = (
                (self._merged_cfg.get("reply_profiles") or {}).get("profiles")
                if isinstance(self._merged_cfg.get("reply_profiles"), dict)
                else []
            )
            if isinstance(profiles, list):
                for p in profiles:
                    if not isinstance(p, dict):
                        continue
                    pid = str(p.get("id") or p.get("name") or "").strip()
                    if not pid:
                        continue
                    persona = p.get("persona") if isinstance(p.get("persona"), dict) else {}
                    facts = []
                    try:
                        from src.integrations.messenger_rpa.persona_runtime import (
                            flatten_persona_facts,
                        )
                        facts = flatten_persona_facts(persona)
                    except Exception:
                        facts = []
                    self._state.upsert_persona(
                        persona_id=pid,
                        name=str((persona or {}).get("name") or pid),
                        language=str(p.get("language") or "auto"),
                        customer_type=str(p.get("customer_type") or ""),
                        facts=facts,
                        persona=persona,
                        status=str(p.get("status") or "active"),
                    )
        except Exception:
            logger.debug("[messenger_rpa] strategy runtime config seed failed", exc_info=True)

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

    def coordinator_snapshot(self) -> Dict[str, Any]:
        """跨账号协调器快照：当前活跃聊天 + 画像缓存概况。"""
        coord = getattr(self, "_coordinator", None)
        if coord is None:
            return {"enabled": False}
        snap = coord.snapshot()
        snap["enabled"] = True
        return snap

    # ―― 账号级暂停 / 恢复 / UI 安全 ――――――――――――――――――――――――――――───────────────────────────

    def pause_account(self, account_id: str, seconds: float = 300.0) -> None:
        """暂停单个账号 N 秒（不影响其他账号的轮询节奏）。"""
        self._account_pause_until[account_id] = time.time() + max(0.0, float(seconds))
        logger.info("[messenger_rpa] account=%s 暂停 %.0fs", account_id, seconds)

    def resume_account(self, account_id: str) -> None:
        """恢复单个账号，同时清除 ui_unsafe 标记。"""
        self._account_pause_until.pop(account_id, None)
        self._account_ui_unsafe.discard(account_id)
        logger.info("[messenger_rpa] account=%s 已恢复", account_id)

    def mark_account_ui_unsafe(self, account_id: str, pause_sec: float = 300.0) -> None:
        """标记账号 UI 不安全（误点相机后自动调用），并暂停 pause_sec 秒。

        账号保持 paused 直到运营在看板点击「清除 unsafe / 恢复」。
        """
        self._account_ui_unsafe.add(account_id)
        self.pause_account(account_id, pause_sec)
        logger.error(
            "[messenger_rpa] ★ account=%s 已标记 ui_unsafe，自动暂停 %.0fs。"
            "请在看板确认设备正常后手动恢复。",
            account_id, pause_sec,
        )

    def clear_account_ui_unsafe(self, account_id: str) -> None:
        """清除 ui_unsafe 标记（运营确认设备正常后调用，不自动恢复暂停）。"""
        self._account_ui_unsafe.discard(account_id)
        logger.info("[messenger_rpa] account=%s ui_unsafe 标记已清除", account_id)

    def account_states(self) -> Dict[str, Any]:
        """返回每个账号的暂停 / unsafe 状态快照（供 status() 和健康 API 使用）。"""
        now = time.time()
        out: Dict[str, Any] = {}
        for ctx in self._account_registry.all_contexts():
            aid = ctx.account_id
            paused_until = self._account_pause_until.get(aid, 0.0)
            out[aid] = {
                "account_id": aid,
                "label": ctx.label,
                "adb_serial": ctx.adb_serial,
                "paused": paused_until > now,
                "paused_until": paused_until,
                "paused_left_sec": max(0.0, paused_until - now),
                "ui_unsafe": aid in self._account_ui_unsafe,
                "last_run": dict(self._last_run_map.get(aid, {})),
                "cur_iv_sec": self._cur_iv_map.get(aid, 0.0),
                "consecutive_empty": self._consecutive_empty_map.get(aid, 0),
            }
        return out

    async def accounts_health(self, *, deep: bool = False) -> Dict[str, Any]:
        """对所有已注册账号执行健康检查，返回每台手机的状态。

        ``deep=False`` (默认)：仅做 ADB 快速探测（~1s）+ 内部状态快照。
        ``deep=True``：额外检查屏幕点亮、锁屏、ADB Keyboard（~3-5s）。
        """
        from src.integrations.messenger_rpa.device_health import probe_devices

        ctxs = self._account_registry.all_contexts()
        serials = [ctx.adb_serial for ctx in ctxs if ctx.adb_serial]

        probe_result: Dict[str, Any] = {}
        try:
            loop = asyncio.get_running_loop()
            probe_result = await loop.run_in_executor(
                None,
                lambda: probe_devices(serials),
            )
        except Exception as ex:
            logger.warning("[messenger_rpa] accounts_health probe_devices 失败: %s", ex)

        now = time.time()
        accounts_out: Dict[str, Any] = {}
        for ctx in ctxs:
            aid = ctx.account_id
            serial = ctx.adb_serial or ""
            probe = probe_result.get(serial, {}) if serial else {}

            paused_until = self._account_pause_until.get(aid, 0.0)
            ui_unsafe = aid in self._account_ui_unsafe
            present = bool(probe.get("present", False))

            item: Dict[str, Any] = {
                "account_id": aid,
                "label": ctx.label,
                "adb_serial": serial or "(none)",
                "adb_state": probe.get("adb_state", "no_serial" if not serial else "not_listed"),
                "present": present,
                "screen_on": probe.get("screen_on"),
                "locked": probe.get("locked"),
                "paused": paused_until > now,
                "paused_until": paused_until,
                "paused_left_sec": max(0.0, paused_until - now),
                "ui_unsafe": ui_unsafe,
                "last_run_step": (self._last_run_map.get(aid, {}) or {}).get("step", ""),
                "last_run_ts": (self._last_run_map.get(aid, {}) or {}).get("ts", 0),
            }

            if deep and present:
                try:
                    loop = asyncio.get_running_loop()
                    from src.integrations.messenger_rpa.text_input import precheck_text_input
                    kbd = await loop.run_in_executor(
                        None,
                        lambda s=serial: precheck_text_input(
                            s, auto_install_adbkeyboard=False,
                        ),
                    )
                    item["adbkeyboard_installed"] = kbd.get("adbkeyboard_installed", False)
                    item["unicode_ok"] = kbd.get("unicode_ok", False)
                    item["input_paths"] = kbd.get("available_paths", [])
                except Exception as ex:
                    item["adbkeyboard_check_error"] = f"{type(ex).__name__}: {ex}"

            reasons: List[str] = []
            if not serial:
                reasons.append("no_serial_configured")
            elif not present:
                reasons.append(item["adb_state"])
            if item.get("screen_on") is False:
                reasons.append("screen_off")
            if item.get("locked") is True:
                reasons.append("device_locked")
            if ui_unsafe:
                reasons.append("ui_unsafe")
            if paused_until > now:
                reasons.append(f"paused_{paused_until - now:.0f}s")

            item["safe_to_run"] = not bool(reasons)
            item["reasons"] = reasons

            accounts_out[aid] = item

        return {
            "total": len(accounts_out),
            "safe_count": sum(1 for v in accounts_out.values() if v["safe_to_run"]),
            "accounts": accounts_out,
        }

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
            runner._persona_ids = list(ctx.persona_ids or [])
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
        # ★ 跨账号协调器注入
        _coord = getattr(self, "_coordinator", None)
        if _coord is not None:
            try:
                runner.set_coordinator(_coord)
            except Exception:
                logger.debug("runner.set_coordinator 失败", exc_info=True)
        self._runners[account_id] = runner
        logger.info(
            "[messenger_rpa] Runner 为 account=%s 初始化完成 "
            "(serial=%s, chat_key_prefix=%s)",
            account_id, ctx.adb_serial or "(none)",
            ctx.merged_config(self._merged_cfg).get("chat_key_prefix"),
        )
        return runner

    async def enqueue_reactivation_deferred(
        self,
        *,
        account_id: str,
        chat_name: str,
        reply_text: str,
        defer_until: float,
        defer_reason: str = "reactivation",
        staleness_sec: float = 86400.0,
        extra: Optional[Dict[str, Any]] = None,
    ) -> int:
        """W2-D4.2：被 reactivation_loop 调用，把主动消息入 messenger 的 deferred 队列。

        与 runner safe_skip 类 deferred 共用 schema：drain loop 自动尊重 gate / staleness。
        Returns: deferred_id (>0 成功) 或 0 (account 不存在/失败)
        """
        try:
            runner = self._get_or_create_runner(account_id)
        except Exception:
            logger.warning(
                "enqueue_reactivation_deferred: account=%s runner 不存在",
                account_id,
            )
            return 0
        try:
            # ★ W2-D4.5 修正：chat_key 必须用 runner 的 _chat_key_prefix 拼接，
            # 否则和 inbox 路径不一致（runner 用 messenger_rpa:Alice / acc_X:Alice）
            # → drain 后续 expire_deferred_for_chat 找不到 → 重发风险
            prefix = getattr(runner, "_chat_key_prefix", None) or "messenger_rpa"
            chat_key = f"{prefix}:{chat_name}"
            return runner._state.enqueue_deferred(
                chat_key=chat_key,
                chat_name=chat_name,
                peer_text="(reactivation 主动发起)",
                peer_kind="reactivation",
                reply_text=reply_text,
                defer_until=defer_until,
                defer_reason=defer_reason[:120],
                run_id="",
                extra=extra or {},
                staleness_sec=staleness_sec,
            )
        except Exception:
            logger.exception("enqueue_reactivation_deferred 失败")
            return 0

    async def _drain_deferred_for_account(
        self, account_id: str, *, max_per_tick: int = 1,
    ) -> int:
        """W2-D1.3 + D2.1+D2.2：把账号下到期的 deferred reply 真发出去。

        改动 D2.1：默认 max_per_tick=1，因为已经迁移到独立 drain loop，
        每 tick 慢慢清比一次轰一波更安全。
        改动 D2.2：drain 前再过一次 _pre_send_gate；不通过则把 deferred_until
        推后（min_gap 类 +60s，daily_cap +1h，quiet_hours +30min），不丢消息。

        返回：实际发送条数（含成功/失败/重 defer）。
        """
        try:
            runner = self._get_or_create_runner(account_id)
        except Exception:
            return 0
        try:
            _live_cfg = self._reload_runtime_cfg()
            runner.refresh_cfg(_live_cfg)
        except Exception:
            pass
        try:
            due = runner._state.drain_due_deferred(limit=max_per_tick)
        except Exception:
            logger.debug("drain_due_deferred 失败 account=%s", account_id, exc_info=True)
            return 0
        if not due:
            return 0
        sent = 0
        pool = self._account_registry.pool
        for row in due:
            chat_name = (row.get("chat_name") or "").strip()
            reply_text = (row.get("reply_text") or "").strip()
            row_id = int(row.get("id") or 0)
            if not chat_name or not reply_text or row_id <= 0:
                runner._state.mark_deferred_failed(row_id, "invalid_row")
                continue
            # ── P2-B：drain 发送前新鲜度校验 ───────────────────────────
            # 对比 deferred.peer_text 与 chat_state.last_peer_text：
            # 若 chat_state 有更新过且两者不等 → 对方已发新消息，
            # 当前 reply_text 是基于旧上下文生成的，发出去会突兀 → 标 failed。
            # 这是 expire_deferred_for_chat 的 defense-in-depth（防
            # chat_key 拼写、并发、漏调用等）。
            # reactivation 类（主动发起）不做此检查 —— 没有对应 peer_msg。
            if bool(_live_cfg.get("drain_freshness_check", True)):
                try:
                    peer_kind_deferred = str(row.get("peer_kind") or "")
                    is_reactivation = (
                        peer_kind_deferred == "reactivation"
                        or (row.get("defer_reason") or "").startswith(
                            "reactivation"
                        )
                    )
                    if not is_reactivation:
                        chat_key_row = str(row.get("chat_key") or "").strip()
                        stored_peer_text = str(
                            row.get("peer_text") or ""
                        ).strip()
                        if chat_key_row and stored_peer_text:
                            cs = runner._state.get_chat_state(chat_key_row)
                            cs_peer_text = str(
                                cs.get("last_peer_text") or ""
                            ).strip()
                            cs_updated_at = float(
                                cs.get("updated_at") or 0.0
                            )
                            row_created_at = float(
                                row.get("created_at") or 0.0
                            )
                            if (
                                cs_peer_text
                                and cs_peer_text != stored_peer_text
                                and cs_updated_at >= row_created_at - 2.0
                            ):
                                runner._state.mark_deferred_failed(
                                    row_id,
                                    (
                                        "stale_peer_context:"
                                        f"defer={stored_peer_text[:40]!r} "
                                        f"current={cs_peer_text[:40]!r}"
                                    )[:200],
                                )
                                try:
                                    from src.monitoring.metrics_store import (
                                        get_metrics_store,
                                    )
                                    get_metrics_store().record_deferred_drain_failed(
                                        "p2b_stale_peer_context",
                                    )
                                except Exception:
                                    pass
                                logger.info(
                                    "[messenger_rpa] P2-B stale peer account=%s "
                                    "id=%d chat=%s defer_peer=%r current_peer=%r",
                                    account_id, row_id, chat_name,
                                    stored_peer_text[:60],
                                    cs_peer_text[:60],
                                )
                                continue
                except Exception:
                    logger.debug(
                        "P2-B freshness check failed account=%s id=%d",
                        account_id, row_id, exc_info=True,
                    )

            # ★ W2-D2.2：drain 前 gate 再 check（防止 quiet_hours 解除瞬间秒发 N 条触发风控）
            # ★ W2-D7.2 评估：原计划在此对 peer_typing defer 再做 vision re-detect，
            # 但 drain 阶段没有"对方此刻"的新截图（要进 thread 截屏，开销过大）。
            # 退化结论：peer_typing 命中已 defer 8s + staleness 120s 兜底，足够覆盖 90%+ 真人打字时长
            try:
                gate2 = runner._pre_send_gate(reply_text)
            except Exception:
                gate2 = None
            if gate2 is not None:
                # 还没解除 → push deferred_until，不丢消息
                push_sec = self._calc_re_defer_sec(gate2)
                new_until = time.time() + push_sec
                try:
                    runner._state.update_deferred_until(
                        row_id, new_until, note=f"re_gate:{gate2.get('reason','?')}"[:120],
                    )
                    logger.info(
                        "[messenger_rpa] drain re-gate push account=%s id=%d chat=%s "
                        "reason=%s push=%dmin",
                        account_id, row_id, chat_name,
                        gate2.get("reason"), int(push_sec / 60),
                    )
                except Exception:
                    runner._state.mark_deferred_failed(
                        row_id, f"re_gate_update_fail:{gate2.get('reason','?')}"[:200],
                    )
                continue  # 不计入 sent，下个 row
            #   - 其他（手动 defer / quiet_hours / 短）→ 2.5s burst
            #   - typing_indicator_advised=false 显式关 → 0
            typing_sec = 2.5
            try:
                if row.get("extra_json"):
                    import json as _json
                    ext = _json.loads(row["extra_json"]) or {}
                    if ext.get("typing_indicator_advised") is False:
                        typing_sec = 0.0
                    elif "typing_pulse_sec" in ext:
                        typing_sec = float(ext["typing_pulse_sec"])
                    else:
                        pds = float(ext.get("pacing_delay_sec") or 0)
                        if pds >= 30:
                            typing_sec = 5.0
                        elif pds >= 15:
                            typing_sec = 4.0
                        elif pds >= 5:
                            typing_sec = 3.0
                        # else 保持默认 2.5
            except Exception:
                pass
            try:
                async with pool.acquire(account_id, timeout=120.0):
                    r = await runner.send_to_chat_name(
                        chat_name=chat_name, reply_text=reply_text,
                        typing_pulse_sec=typing_sec,
                        skip_search=True,
                    )
                if r.get("ok") and r.get("step") == "sent":
                    runner._state.mark_deferred_sent(row_id)
                    sent += 1
                    try:
                        from src.monitoring.metrics_store import get_metrics_store
                        get_metrics_store().record_deferred_drain_sent()
                    except Exception:
                        pass
                    logger.info(
                        "[messenger_rpa] deferred drain ok account=%s id=%d chat=%s wait_min=%d",
                        account_id, row_id, chat_name,
                        max(0, int((time.time() - float(row.get("created_at") or 0)) / 60)),
                    )
                else:
                    runner._state.mark_deferred_failed(
                        row_id, str(r.get("error") or r.get("step") or "send_not_ok")[:200],
                    )
                    try:
                        from src.monitoring.metrics_store import get_metrics_store
                        get_metrics_store().record_deferred_drain_failed(
                            str(r.get("step") or "send_not_ok"),
                        )
                    except Exception:
                        pass
                    logger.warning(
                        "[messenger_rpa] deferred drain fail account=%s id=%d chat=%s step=%s",
                        account_id, row_id, chat_name, r.get("step"),
                    )
            except asyncio.TimeoutError:
                runner._state.mark_deferred_failed(row_id, "pool_acquire_timeout")
            except Exception as ex:
                runner._state.mark_deferred_failed(row_id, f"{type(ex).__name__}:{ex}"[:200])
                logger.debug("deferred drain 异常 id=%d", row_id, exc_info=True)
        return sent

    @staticmethod
    def _calc_re_defer_sec(gate: Dict[str, Any]) -> float:
        """W2-D2.2：drain 前 gate 失败时把 deferred_until 推后多久。

        不重做 _calc_defer_until_sec 那套精确日历计算，简单按类型推固定时长，
        因为 drain loop 自己会再次循环检查。
        """
        reason = (gate or {}).get("reason", "")
        if reason.startswith("rate_limit:min_gap"):
            return float(gate.get("wait_remaining_sec") or 60.0) + 5.0
        if reason.startswith("rate_limit:daily_cap"):
            return 3600.0  # 1h 后再看（最迟次日 0 点会过 cap）
        if reason.startswith("rate_limit:quiet_hours"):
            return 1800.0  # 30 min 后再看
        if reason.startswith("pace:throttle"):
            return 600.0   # 10 min
        if reason.startswith("pace:deny"):
            return 1800.0  # 30 min
        return 600.0  # 兜底 10 min

    async def _deferred_drain_loop(self) -> None:
        """W2-D2.1 + D3.2：独立 drain loop，与主 _loop 平行运行。

        - 间隔默认 30 秒（companion_drain_interval_sec 可配；每轮 reload 配置）
        - 每 tick 每账号最多 drain 1 条（独立 loop 慢慢清就够了）
        - 尊重 pause / stop 信号
        - companion_mode=false 时进入 noop 循环（不真 drain）便于热切换
        """
        try:
            while not self._stop_evt.is_set():
                # 实时读 config（不依赖 startup 时的 self._merged_cfg）
                cfg_now = self._reload_runtime_cfg()
                companion_on = bool(cfg_now.get("companion_mode", False))
                interval = float(cfg_now.get("companion_drain_interval_sec", 30.0) or 30.0)
                interval = max(5.0, min(300.0, interval))
                max_per_tick = int(cfg_now.get("companion_drain_max_per_tick", 1) or 1)

                if not companion_on:
                    # noop：等下一轮再看；不真 drain 也不退出，方便热开关
                    try:
                        await asyncio.wait_for(self._stop_evt.wait(), timeout=interval)
                    except asyncio.TimeoutError:
                        pass
                    continue

                # 暂停态尊重（pause_until 来自 web /api/messenger-rpa/pause）
                if self._pause_until > time.time():
                    await asyncio.wait_for(
                        self._stop_evt.wait(),
                        timeout=min(10.0, interval),
                    )
                    continue
                ctxs = self._account_registry.all_contexts()
                aids = [c.account_id for c in ctxs]
                # 每账号独立 drain，错开请求（避免一秒内多账号同时发）
                for aid in aids:
                    if self._stop_evt.is_set():
                        break
                    try:
                        await self._drain_deferred_for_account(
                            aid, max_per_tick=max_per_tick,
                        )
                    except Exception:
                        logger.debug(
                            "drain account=%s 异常", aid, exc_info=True,
                        )
                # ★ 写 metrics：deferred 队列健康度
                try:
                    from src.monitoring.metrics_store import get_metrics_store
                    by_acc: Dict[str, int] = {}
                    total = 0
                    for aid in aids:
                        try:
                            runner = self._get_or_create_runner(aid)
                            cnt = len(runner._state.list_approvals(
                                status="deferred", limit=10000,
                            ))
                            by_acc[aid] = cnt
                            total += cnt
                        except Exception:
                            pass
                    get_metrics_store().set_deferred_queue_size(total, by_acc)
                except Exception:
                    logger.debug("metrics deferred_queue 写入失败", exc_info=True)
                # 等下一轮（被 stop_evt 中断也 OK）
                try:
                    await asyncio.wait_for(
                        self._stop_evt.wait(), timeout=interval,
                    )
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            logger.info("MessengerRpaService _deferred_drain_loop 被取消")
            raise
        except Exception:
            logger.exception("MessengerRpaService _deferred_drain_loop 异常退出")

    async def _run_once_for_account(
        self, account_id: str, *, acquire_timeout: float = 180.0,
    ) -> Dict[str, Any]:
        """P6-1：跑单账号一轮，外层加 AccountPool.acquire 保证 adb 不冲突。

        - `acquire_timeout`：防止死锁；超时视为本轮 skip（不是错误）
        - 异常统一兜底，返回带 step 的 result dict
        """
        # ★ 账号级暂停检查（不影响其他账号）
        _pause_until = self._account_pause_until.get(account_id, 0.0)
        if _pause_until > time.time():
            return {
                "ok": False,
                "step": "account_paused",
                "error": f"account_paused_left={_pause_until - time.time():.0f}s",
                "account_id": account_id,
            }
        # ★ UI 安全护盾：ui_unsafe 账号停止自动运行，等运营清除后恢复
        if account_id in self._account_ui_unsafe:
            return {
                "ok": False,
                "step": "account_ui_unsafe",
                "error": "account_ui_unsafe: 请在看板清除 unsafe 标记后点击恢复",
                "account_id": account_id,
            }
        try:
            runner = self._get_or_create_runner(account_id)
        except Exception as ex:
            return {
                "ok": False, "step": "runner_init_failed",
                "error": f"{type(ex).__name__}: {ex}",
                "account_id": account_id,
            }
        try:
            runner.refresh_cfg(self._reload_runtime_cfg())
        except Exception:
            pass
        pool = self._account_registry.pool
        try:
            async with pool.acquire(account_id, timeout=acquire_timeout):
                _dev_serial = (runner._cfg.get("adb_serial") or "").strip()
                async with get_device_lock(_dev_serial):
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
                # ★ W3-D2.2：冷启动门槛配置化（默认 2，避免 1 条 inbound 就调 LLM）
                min_for_initial=int(pcfg.get("min_for_initial", 2) or 2),
            )
            logger.info(
                "[messenger_rpa] PortraitExtractor 已就绪 (refresh_every_n=%d, "
                "refresh_after_hours=%.1f, min_for_initial=%d)",
                self._portrait_extractor._n,
                self._portrait_extractor._refresh_after_sec / 3600.0,
                self._portrait_extractor._min_for_initial,
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
            "interval_sec": 8.0,          # 基础间隔
            "min_interval_sec": 2.0,      # 有未读时下一轮最小间隔
            "max_interval_sec": 60.0,     # 连续空跑后最大间隔
            "backoff_multiplier": 1.25,   # 每次空跑递增倍率
            # 单次 run
            "max_inbox_per_run": 1,       # 一次只处理 N 条未读
            # smart_current_thread: 若手机已停在目标聊天页，直接读当前页；
            # force_chats: 每次强制回 Chats 列表扫描。
            "run_once_start_mode": "smart_current_thread",
            "thread_title_vision_fallback": True,
            "pre_thread_self_xml_guard": True,
            "stale_peer_after_self_guard": True,
            "stale_peer_after_self_window_sec": 900,
            "stale_peer_after_self_overlap_threshold": 0.45,
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

    def _reload_runtime_cfg(self) -> Dict[str, Any]:
        """W2-D3.2：从 ConfigManager 实时拉一份最新 messenger_rpa 配置。

        用于 _deferred_drain_loop 这种需要热感知 companion_mode / 间隔等
        切换的场景。失败时回退到 startup 时的 _merged_cfg。
        """
        try:
            cfg = (self._cm.config or {}).get("messenger_rpa") or {}
            if isinstance(cfg, dict) and cfg:
                # 仍然走 defaults 兜底
                d = self._defaults()
                for k, v in cfg.items():
                    if k == "screencap" and isinstance(v, dict) and isinstance(
                        d.get("screencap"), dict,
                    ):
                        d[k] = {**d["screencap"], **v}
                    else:
                        d[k] = v
                return d
        except Exception:
            pass
        return self._merged_cfg

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
        return await self._do_start()

    async def force_start(self) -> bool:
        """Web 控制面板调用：绕过 autostart 检查，只要 enabled 就拉起 loop。"""
        if self._task and not self._task.done():
            return True  # 已在运行
        if not self._merged_cfg.get("enabled"):
            logger.info("MessengerRpaService enabled=False，跳过 force_start")
            return False
        return await self._do_start()

    async def _do_start(self) -> bool:
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
        # ★ W2-D2.1+D3.2：deferred 独立 drain loop 总是启动；内部按 companion_mode
        # 配置实时决定是否真 drain，便于热切换不重启
        self._drain_task = asyncio.create_task(
            self._deferred_drain_loop(), name="messenger_rpa_defer_drain",
        )
        logger.info("MessengerRpaService 已启动")
        return True

    @property
    def is_running(self) -> bool:
        return bool(self._task and not self._task.done())

    async def stop(self) -> None:
        self._stop_evt.set()
        self._trigger_evt.set()
        tasks = [self._task, self._notif_task, self._sla_task, self._drain_task]
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
        self._drain_task = None
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
            "coordinator": self.coordinator_snapshot(),
            "last_run": dict(self._last_run) if self._last_run else {},
            # ★ P6-1：多账号 per-account 最近一次 run + 节奏 + 暂停/unsafe
            "per_account": {
                aid: {
                    "last_run": dict(self._last_run_map.get(aid, {})),
                    "cur_iv_sec": self._cur_iv_map.get(aid, 0.0),
                    "consecutive_empty": self._consecutive_empty_map.get(aid, 0),
                    "paused": self._account_pause_until.get(aid, 0.0) > time.time(),
                    "paused_until": self._account_pause_until.get(aid, 0.0),
                    "ui_unsafe": aid in self._account_ui_unsafe,
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
                cfg_now = self._reload_runtime_cfg()
                self._merged_cfg = cfg_now
                base_iv = float(cfg_now.get("interval_sec", base_iv))
                min_iv = float(cfg_now.get("min_interval_sec", min_iv))
                max_iv = float(cfg_now.get("max_interval_sec", max_iv))
                mult = float(cfg_now.get("backoff_multiplier", mult))
                try:
                    self._runner.refresh_cfg(cfg_now)
                    for _r in self._runners.values():
                        _r.refresh_cfg(cfg_now)
                except Exception:
                    logger.debug("[messenger_rpa] hot refresh runner cfg failed", exc_info=True)

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

                # ★ W2-D2.1：drain 已迁移到独立 _deferred_drain_loop，
                # 这里不再 inline 调用，避免阻塞主 _loop 的 inbox 处理节奏
                if not multi_mode:
                    # ── 单账号旧路径（100% 兼容）──
                    try:
                        _dev_s = (self._runner._cfg.get("adb_serial") or "").strip()
                        async with get_device_lock(_dev_s):
                            r = await self._runner.run_once()
                        self._last_run = r
                        if r.get("ok") and r.get("step") == "sent":
                            cur_iv = min_iv
                            self._consecutive_empty = 0
                        elif r.get("step") == "sticky_idle":
                            # P2-A 粘性 idle：用 sticky_thread.idle_poll_interval_sec
                            # 短间隔继续 poll（默认 1.5s），实现"几秒回"
                            sticky_iv = float(
                                (cfg_now.get("sticky_thread") or {}).get(
                                    "idle_poll_interval_sec", 1.5
                                ) or 1.5
                            )
                            cur_iv = sticky_iv
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
                    # P28：手动发送队列投递
                    try:
                        sq_res = await self._runner.run_send_queue_deliveries(max_deliver=3)
                        if sq_res.get("delivered") or sq_res.get("failed"):
                            self._last_run["send_queue_deliver"] = sq_res
                            logger.info(
                                "[messenger_rpa] send_queue_deliver delivered=%s failed=%s",
                                sq_res.get("delivered"), sq_res.get("failed"),
                            )
                    except Exception:
                        logger.debug("run_send_queue_deliveries 失败", exc_info=True)
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
                    # ★ 安全护盾：自动暂停误点相机的账号
                    if step == "ui_unsafe_tap":
                        self.mark_account_ui_unsafe(aid)
                    iv = self._cur_iv_map.get(aid, base_iv)
                    if r.get("ok") and step == "sent":
                        iv = min_iv
                        self._consecutive_empty_map[aid] = 0
                    elif step == "sticky_idle":
                        # P3-D：多账号路径同步 sticky_idle 短间隔
                        # 让粘性 chat 在多账号模式下也享受 1.5s 快速 poll
                        sticky_iv = float(
                            (cfg_now.get("sticky_thread") or {}).get(
                                "idle_poll_interval_sec", 1.5
                            ) or 1.5
                        )
                        iv = sticky_iv
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

    # ── P28：手动发送队列 ──────────────────────────────────────
    def enqueue_send(
        self, *, chat_key: str, peer_name: str, text: str, created_by: str = "",
    ) -> int:
        """入队一条主动发送任务并立即唤醒 runner 进入下一轮。"""
        item_id = self._state.enqueue_send(
            chat_key=chat_key, peer_name=peer_name, text=text, created_by=created_by,
        )
        try:
            self._trigger_evt.set()
        except Exception:
            pass
        return item_id

    def list_send_queue(self, *, limit: int = 30, include_done: bool = False) -> list:
        return self._state.list_send_queue(limit=limit, include_done=include_done)

    def get_send_queue_item(self, item_id: int):
        return self._state.get_send_queue_item(int(item_id))

    def cancel_send_queue_item(self, item_id: int) -> bool:
        return self._state.cancel_send_queue_item(int(item_id))

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
        while not self._stop_evt.is_set():  # noqa: SIM117
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
