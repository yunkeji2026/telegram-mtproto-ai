# -*- coding: utf-8 -*-
"""ADB HotPlug Watcher — 自动检测 USB 设备并动态创建 DeviceCoordinator。

设计理念：
  - 每 N 秒扫描 `adb devices`，与 registry DB 比对
  - 新设备出现 → 查 DeviceRegistryDB → 获取 platform 分配 → 动态构建 DeviceCoordinator
  - 设备消失 → 停止对应 DeviceCoordinator
  - 未注册设备 → 记录日志，不做处理（可通过 Web 注册后自动纳管）

多主机支持（host_name）：
  - config.yaml 新增 hotplug_watcher.host_name，对应 registry DB 的 group_name
  - host_name="主控" → 只管 group_name="主控" 的设备
  - host_name 为空 → 管所有在线且已注册的设备
  - 这样 W03/W175 各自部署一份 telegram-mtproto-ai，用不同 host_name 隔离

与 config.yaml 的关系：
  - 若 device_coordinator.devices[] 已配置某 serial，则由 DeviceCoordinatorService 静态管理
  - HotPlug Watcher 只管"不在静态 config 中但在 registry DB 中有注册"的设备
  - 这样两种模式可共存，不冲突

使用:
    from src.integrations.shared.hotplug_watcher import HotPlugWatcher
    watcher = HotPlugWatcher(config_manager=cm, skill_manager=sm, host_name="主控")
    await watcher.start()
    ...
    await watcher.stop()
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Set

from src.integrations.line_rpa.adb_helpers import list_adb_device_rows
from src.integrations.shared.device_coordinator import DeviceCoordinator, PlatformRunner
from src.integrations.shared.device_service import _build_runner, _find_account_cfg
from src.shared.device_registry import get_device_registry

logger = logging.getLogger(__name__)

# 扫描间隔（秒）
_DEFAULT_SCAN_INTERVAL = 15.0
# 设备必须连续在线 N 次扫描才纳管（防止 USB 抖动）
_STABLE_THRESHOLD = 2


# 离线超时（秒）— 设备离线超过此时间才停止 Coordinator
_DEFAULT_OFFLINE_TIMEOUT = 30.0


class HotPlugWatcher:
    """监控本机 ADB 设备，自动创建/销毁 DeviceCoordinator。"""

    def __init__(
        self,
        *,
        config_manager: Any,
        skill_manager: Any,
        scan_interval_sec: float = _DEFAULT_SCAN_INTERVAL,
        static_serials: Optional[Set[str]] = None,
        host_name: str = "",
        offline_timeout_sec: float = _DEFAULT_OFFLINE_TIMEOUT,
    ) -> None:
        """
        Args:
            config_manager: 全局 ConfigManager
            skill_manager: 全局 SkillManager
            scan_interval_sec: 扫描周期
            static_serials: 已被 DeviceCoordinatorService 静态管理的 serial 集合，
                            HotPlug 不会重复为它们创建 Coordinator
            host_name: 本机名称，对应 registry DB 的 group_name（空=管所有）
            offline_timeout_sec: 设备离线超过此秒数才停止 Coordinator
        """
        self._cm = config_manager
        self._sm = skill_manager
        self._interval = scan_interval_sec
        self._static_serials: Set[str] = set(static_serials or set())
        self._host_name = host_name
        self._offline_timeout = offline_timeout_sec

        self._task: Optional[asyncio.Task] = None
        self._stop_evt = asyncio.Event()

        # serial → DeviceCoordinator
        self._coordinators: Dict[str, DeviceCoordinator] = {}
        # serial → 连续在线计数（防抖动）
        self._seen_count: Dict[str, int] = {}
        # serial → 上次离线检测时刻
        self._offline_since: Dict[str, float] = {}
        # 在线但未注册的 serial（供 Web 面板显示）
        self._unregistered_online: Set[str] = set()

        self._registry = get_device_registry()

    # ── 生命周期 ─────────────────────────────────────────

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop_evt.clear()
        self._task = asyncio.create_task(self._loop(), name="hotplug_watcher")
        logger.info("[HotPlug] Watcher 已启动（间隔 %.0fs）", self._interval)

    async def stop(self) -> None:
        self._stop_evt.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        # 停止所有动态 Coordinator
        for serial, coord in list(self._coordinators.items()):
            try:
                await coord.stop()
            except Exception:
                pass
        self._coordinators.clear()
        logger.info("[HotPlug] Watcher 已停止")

    def status(self) -> Dict[str, Any]:
        return {
            "running": bool(self._task and not self._task.done()),
            "host_name": self._host_name or "(all)",
            "managed_devices": [
                {
                    "serial": s,
                    "source": "hotplug",
                    **coord.status(),
                }
                for s, coord in self._coordinators.items()
            ],
            "static_serials": list(self._static_serials),
            "unregistered_online": list(self._unregistered_online),
        }

    # ── 主循环 ─────────────────────────────────────────

    async def _loop(self) -> None:
        while not self._stop_evt.is_set():
            try:
                await self._scan_cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[HotPlug] scan_cycle 异常")
            try:
                await asyncio.wait_for(self._stop_evt.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                pass

    async def _scan_cycle(self) -> None:
        """一次扫描：检测在线设备，动态创建/停止 Coordinator。"""
        # 1. 获取当前在线设备
        try:
            rows = await asyncio.to_thread(list_adb_device_rows)
        except Exception:
            logger.debug("[HotPlug] adb devices 调用失败", exc_info=True)
            return

        online_serials: Set[str] = set()
        for serial, state in rows:
            if state == "device":
                online_serials.add(serial)

        # 2. 更新未注册列表（只保留当前在线的）
        self._unregistered_online &= online_serials

        # 3. 过滤掉静态管理的设备
        dynamic_online = online_serials - self._static_serials

        # 4. 处理新出现的设备
        for serial in dynamic_online:
            if serial in self._coordinators:
                # 已经在管理中
                self._seen_count[serial] = _STABLE_THRESHOLD
                self._offline_since.pop(serial, None)
                continue

            # 计数防抖
            self._seen_count[serial] = self._seen_count.get(serial, 0) + 1
            if self._seen_count[serial] < _STABLE_THRESHOLD:
                logger.debug("[HotPlug] %s 出现 %d/%d 次，等待稳定",
                             serial[:8], self._seen_count[serial], _STABLE_THRESHOLD)
                continue

            # 查 registry
            dev_info = self._registry.get(serial)
            if dev_info is None:
                self._unregistered_online.add(serial)
                logger.info("[HotPlug] 设备 %s 未注册，忽略（可通过 Web 注册后自动纳管）", serial[:8])
                continue

            # 多主机过滤：若设置了 host_name，只管本机分组的设备
            if self._host_name:
                dev_group = dev_info.get("group_name", "")
                if dev_group and dev_group != self._host_name:
                    logger.debug("[HotPlug] 设备 %s 属于 %s，本机 %s 不管",
                                 serial[:8], dev_group, self._host_name)
                    continue

            # 从 registry 获取平台分配，若无则自动检测
            platforms = self._extract_platforms(dev_info)
            if not platforms:
                logger.info("[HotPlug] 设备 %s (%s) 无平台分配，尝试自动检测 app",
                            serial[:8], dev_info.get("label", ""))
                platforms = await self._auto_detect_and_register(serial, dev_info)
                if not platforms:
                    continue
                dev_info = self._registry.get(serial) or dev_info

            # 构建 DeviceCoordinator
            coord = await self._build_coordinator(serial, dev_info, platforms)
            if coord:
                self._coordinators[serial] = coord
                self._offline_since.pop(serial, None)
                logger.warning(
                    "[HotPlug] ✅ 设备 %s (%s) 已纳管，平台: %s",
                    serial[:8], dev_info.get("label", ""),
                    [p["type"] for p in platforms],
                )
                try:
                    from src.integrations.shared.event_bus import get_event_bus
                    get_event_bus().publish("device_online", {
                        "serial": serial,
                        "label": dev_info.get("label", ""),
                        "platforms": [p["type"] for p in platforms],
                        "host": self._host_name or "(all)",
                    })
                except Exception:
                    pass

        # 5. 处理离线设备
        managed_serials = set(self._coordinators.keys())
        gone_serials = managed_serials - online_serials

        for serial in gone_serials:
            now = time.time()
            if serial not in self._offline_since:
                self._offline_since[serial] = now
                logger.info("[HotPlug] 设备 %s 离线，等待 30s 确认", serial[:8])
                continue

            # 离线超过 timeout 才停止（防止 USB 瞬断）
            if now - self._offline_since[serial] < self._offline_timeout:
                continue

            coord = self._coordinators.pop(serial, None)
            if coord:
                try:
                    await coord.stop()
                except Exception:
                    pass
                self._seen_count.pop(serial, None)
                self._offline_since.pop(serial, None)
                logger.warning("[HotPlug] ⚠ 设备 %s 已离线并停止 Coordinator", serial[:8])
                try:
                    from src.integrations.shared.event_bus import get_event_bus
                    get_event_bus().publish("device_offline", {
                        "serial": serial,
                        "host": self._host_name or "(all)",
                    })
                except Exception:
                    pass

    # ── 工具 ─────────────────────────────────────────

    async def _auto_detect_and_register(
        self, serial: str, dev_info: Dict
    ) -> List[Dict[str, str]]:
        """ADB 扫描设备已安装的聊天 app，自动写入 registry 并返回平台列表。"""
        from src.integrations.line_rpa.adb_helpers import (
            detect_installed_chat_apps,
            get_chat_account_name,
        )
        label = (
            (dev_info.get("label") or serial[:8])
            .lower()
            .replace("-", "_")
            .replace(" ", "_")
        )
        try:
            installed = await asyncio.to_thread(detect_installed_chat_apps, serial)
        except Exception as exc:
            logger.warning("[HotPlug] %s app 检测失败: %s", serial[:8], exc)
            return []

        if not any(installed.values()):
            logger.info("[HotPlug] %s 未检测到已安装的聊天 app", serial[:8])
            return []

        _PREFIX = {"messenger": "msg", "line": "line", "whatsapp": "wa"}
        platforms: List[Dict[str, str]] = []
        registry_updates: Dict[str, str] = {}

        for ptype in ["messenger", "line", "whatsapp"]:
            if not installed.get(ptype):
                continue
            account_id = f"{_PREFIX[ptype]}_{label}"
            registry_updates[f"platform_{ptype}"] = account_id
            platforms.append({"type": ptype, "account_id": account_id})
            try:
                acct_name = await asyncio.to_thread(get_chat_account_name, serial, ptype)
                logger.info(
                    "[HotPlug] %s 检测到 %s 已安装 (account_id=%s%s)",
                    serial[:8], ptype, account_id,
                    f" 账号={acct_name}" if acct_name else "",
                )
            except Exception:
                pass

        if registry_updates:
            try:
                self._registry.upsert(serial, **registry_updates)
                logger.warning(
                    "[HotPlug] ✅ %s (%s) 自动检测完成，已写入 registry: %s",
                    serial[:8], label, list(registry_updates.keys()),
                )
            except Exception as exc:
                logger.error("[HotPlug] %s registry upsert 失败: %s", serial[:8], exc)
                return []

        return platforms

    def _extract_platforms(self, dev_info: Dict) -> List[Dict[str, str]]:
        """从 registry 记录提取平台列表。"""
        platforms: List[Dict[str, str]] = []
        if dev_info.get("platform_messenger"):
            platforms.append({"type": "messenger", "account_id": dev_info["platform_messenger"]})
        if dev_info.get("platform_line"):
            platforms.append({"type": "line", "account_id": dev_info["platform_line"]})
        if dev_info.get("platform_whatsapp"):
            platforms.append({"type": "whatsapp", "account_id": dev_info["platform_whatsapp"]})
        return platforms

    async def _build_coordinator(
        self,
        serial: str,
        dev_info: Dict,
        platforms: List[Dict[str, str]],
    ) -> Optional[DeviceCoordinator]:
        """动态构建并启动一个 DeviceCoordinator。"""
        global_cfg = self._cm.config or {}
        platform_runners: List[PlatformRunner] = []

        for p in platforms:
            ptype = p["type"]
            aid = p["account_id"]
            pcfg = _find_account_cfg(global_cfg, ptype, aid)
            pcfg["adb_serial"] = serial
            if aid:
                pcfg.setdefault("account_id", aid)
            runner = _build_runner(ptype, pcfg, self._cm, self._sm)
            if runner is None:
                logger.warning("[HotPlug] %s/%s runner 构建失败", serial[:8], ptype)
                continue
            platform_runners.append(PlatformRunner(ptype, runner, aid))

        if not platform_runners:
            return None

        label = dev_info.get("label") or serial[:8]
        coord = DeviceCoordinator(
            serial=serial,
            platform_runners=platform_runners,
            label=f"HP-{label}",
            poll_interval_sec=15.0,
            idle_poll_interval_sec=30.0,
            force_check_interval_sec=45.0,
            priority_by_badge=True,
            run_timeout_sec=180.0,
            circuit_breaker_threshold=5,
            on_circuit_open=self._on_circuit_open,
            on_recovery=self._on_recovery,
            alert_cooldown_sec=600.0,
        )
        await coord.start()
        return coord

    def _get_webhook(self):
        """惰性初始化 WebhookNotifier。"""
        if not hasattr(self, "_webhook") or self._webhook is None:
            from src.utils.webhook import WebhookNotifier
            wh_cfg = (self._cm.config or {}).get("webhook", {}) or {}
            if wh_cfg.get("enabled"):
                self._webhook = WebhookNotifier(wh_cfg)
            else:
                self._webhook = False
        return self._webhook

    def _on_circuit_open(self, serial: str, platform_type: str, consecutive_fail: int) -> None:
        """设备平台熔断告警 — 推 webhook + 记日志。"""
        msg = (
            f"⚠️ 设备熔断: serial={serial[:12]} "
            f"platform={platform_type} "
            f"fail_streak={consecutive_fail} "
            f"host={self._host_name or '(all)'}"
        )
        logger.error("[ALERT] %s", msg)
        try:
            logging.getLogger("ai_chat_assistant").error("[device_alert] %s", msg)
        except Exception:
            pass
        notifier = self._get_webhook()
        if notifier and notifier is not False:
            try:
                notifier.notify(
                    "device_coordinator.circuit_open",
                    {
                        "action": "circuit_open",
                        "target": serial,
                        "platform": platform_type,
                        "consecutive_fail": consecutive_fail,
                        "host_name": self._host_name or "(all)",
                        "message": msg,
                    },
                )
            except Exception:
                logger.debug("webhook 推送 circuit_open 告警失败", exc_info=True)
        # SSE 事件推送
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("circuit_open", {
                "serial": serial, "platform": platform_type,
                "consecutive_fail": consecutive_fail,
                "host": self._host_name or "(all)",
            })
        except Exception:
            pass

    def _on_recovery(self, serial: str, platform_type: str, prev_fail_count: int) -> None:
        """设备平台恢复通知 — 推 webhook + 记日志。"""
        msg = (
            f"✅ 设备恢复: serial={serial[:12]} "
            f"platform={platform_type} "
            f"prev_fails={prev_fail_count} "
            f"host={self._host_name or '(all)'}"
        )
        logger.warning("[RECOVERY] %s", msg)
        try:
            logging.getLogger("ai_chat_assistant").warning("[device_recovery] %s", msg)
        except Exception:
            pass
        notifier = self._get_webhook()
        if notifier and notifier is not False:
            try:
                notifier.notify(
                    "device_coordinator.recovery",
                    {
                        "action": "recovery",
                        "target": serial,
                        "platform": platform_type,
                        "prev_fail_count": prev_fail_count,
                        "host_name": self._host_name or "(all)",
                        "message": msg,
                    },
                )
            except Exception:
                logger.debug("webhook 推送 recovery 通知失败", exc_info=True)
        # SSE 事件推送
        try:
            from src.integrations.shared.event_bus import get_event_bus
            get_event_bus().publish("recovery", {
                "serial": serial, "platform": platform_type,
                "prev_fail_count": prev_fail_count,
                "host": self._host_name or "(all)",
            })
        except Exception:
            pass

    # ── 外部 API ─────────────────────────────────────

    async def reload_device(self, serial: str) -> Dict[str, Any]:
        """热重建指定设备的 Coordinator（平台分配变更后调用）。

        流程：停旧 Coordinator → 重读 registry → 构建新 Coordinator。
        返回 {"ok": True/False, "action": "...", "platforms": [...]}。
        """
        result: Dict[str, Any] = {"serial": serial, "ok": False, "action": "none"}

        # 如果是静态管理的设备，不允许热重建
        if serial in self._static_serials:
            result["error"] = "device is statically managed"
            return result

        # 1. 停掉旧 Coordinator（如果有）
        old_coord = self._coordinators.pop(serial, None)
        if old_coord:
            try:
                await old_coord.stop()
            except Exception:
                pass
            result["action"] = "rebuilt"
            logger.info("[HotPlug] reload: 已停止旧 Coordinator %s", serial[:8])
        else:
            result["action"] = "created"

        # 2. 重读 registry
        dev_info = self._registry.get(serial)
        if dev_info is None:
            result["ok"] = True
            result["action"] = "removed" if old_coord else "none"
            result["platforms"] = []
            logger.info("[HotPlug] reload: %s 已从 registry 删除或未注册", serial[:8])
            return result

        # 3. 多主机过滤
        if self._host_name:
            dev_group = dev_info.get("group_name", "")
            if dev_group and dev_group != self._host_name:
                result["ok"] = True
                result["action"] = "skipped_other_host"
                result["platforms"] = []
                return result

        # 4. 提取平台
        platforms = self._extract_platforms(dev_info)
        if not platforms:
            result["ok"] = True
            result["action"] = "no_platforms"
            result["platforms"] = []
            self._unregistered_online.discard(serial)
            return result

        # 5. 构建新 Coordinator
        coord = await self._build_coordinator(serial, dev_info, platforms)
        if coord:
            self._coordinators[serial] = coord
            self._unregistered_online.discard(serial)
            result["ok"] = True
            result["platforms"] = [p["type"] for p in platforms]
            logger.warning(
                "[HotPlug] reload: ✅ %s (%s) Coordinator 已重建，平台: %s",
                serial[:8], dev_info.get("label", ""),
                result["platforms"],
            )
        else:
            result["error"] = "build_coordinator failed"

        return result

    def add_static_serial(self, serial: str) -> None:
        """运行时新增静态设备（防止 HotPlug 重复管理）。"""
        self._static_serials.add(serial)

    def remove_static_serial(self, serial: str) -> None:
        """移除静态设备标记（允许 HotPlug 接管）。"""
        self._static_serials.discard(serial)

    @property
    def managed_serials(self) -> Set[str]:
        return set(self._coordinators.keys())

    @property
    def host_name(self) -> str:
        return self._host_name
