"""DeviceCoordinatorService — 从配置构建 DeviceCoordinator 并管理生命周期。

config.yaml 结构：
  device_coordinator:
    enabled: true
    devices:
      - serial: IJ8HZLORS485PJWW
        label: "IJ8-主机"
        enabled: true
        poll_interval_sec: 15
        idle_poll_interval_sec: 30
        force_check_interval_sec: 120
        platforms:
          - type: line
            account_id: line_ij8      # 对应 line_rpa.accounts[].account_id
          - type: whatsapp
            account_id: wa_ij8        # 对应 whatsapp_rpa.accounts[].account_id
          - type: messenger
            account_id: msg_ij8       # 对应 messenger_rpa (单账号时忽略 account_id)
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from src.integrations.shared.device_coordinator import DeviceCoordinator, PlatformRunner

logger = logging.getLogger(__name__)


def _find_account_cfg(
    global_cfg: Dict[str, Any],
    platform_type: str,
    account_id: str,
) -> Dict[str, Any]:
    """从全局 config 里找到对应平台+账号的合并 cfg。"""
    if platform_type == "line":
        base = global_cfg.get("line_rpa") or {}
        for acc in (base.get("accounts") or []):
            if acc.get("account_id") == account_id:
                merged = {**base, **acc}
                merged.pop("accounts", None)
                return merged
        return dict(base)  # 返回副本，防止后续设备修改全局 cfg

    if platform_type == "whatsapp":
        base = global_cfg.get("whatsapp_rpa") or {}
        for acc in (base.get("accounts") or []):
            if acc.get("account_id") == account_id:
                merged = {**base, **acc}
                merged.pop("accounts", None)
                return merged
        return dict(base)  # 返回副本

    if platform_type == "messenger":
        base = global_cfg.get("messenger_rpa") or {}
        for acc in (base.get("accounts") or []):
            # config.yaml 可能用 'id' 或 'account_id' 键，两者都检查
            acc_id = acc.get("account_id") or acc.get("id") or ""
            if acc_id == account_id:
                merged = {**base, **acc}
                merged.pop("accounts", None)
                return merged
        return dict(base)  # 返回副本，防止串设备 bug

    return {}


def _build_runner(
    platform_type: str,
    platform_cfg: Dict[str, Any],
    config_manager: Any,
    skill_manager: Any,
) -> Optional[Any]:
    """按平台类型实例化对应的 runner。"""
    try:
        if platform_type == "line":
            from src.integrations.line_rpa.runner import LineRpaRunner
            from src.integrations.line_rpa.state_store import (
                LineRpaStateStore, default_state_db_path,
            )
            from pathlib import Path
            account_id = platform_cfg.get("account_id") or "default"
            if account_id and account_id != "default":
                db_path = Path(config_manager.config_path).parent / f"line_rpa_state_{account_id}.db"
            else:
                db_path = default_state_db_path(config_manager.config_path)
            state = LineRpaStateStore(db_path)
            return LineRpaRunner(
                config_manager=config_manager,
                skill_manager=skill_manager,
                line_rpa_cfg=platform_cfg,
                state_store=state,
            )

        if platform_type == "whatsapp":
            from src.integrations.whatsapp_rpa.runner import WhatsAppRpaRunner
            from src.integrations.whatsapp_rpa.state_store import (
                WaRpaStateStore, default_state_db_path as wa_db_path,
            )
            from pathlib import Path
            account_id = platform_cfg.get("account_id") or "default"
            if account_id and account_id != "default":
                db_path = Path(config_manager.config_path).parent / f"wa_rpa_state_{account_id}.db"
            else:
                db_path = wa_db_path(config_manager.config_path)
            state = WaRpaStateStore(db_path)
            return WhatsAppRpaRunner(
                config_manager=config_manager,
                skill_manager=skill_manager,
                wa_cfg=platform_cfg,
                state_store=state,
            )

        if platform_type == "messenger":
            from src.integrations.messenger_rpa.runner import MessengerRpaRunner
            from src.integrations.messenger_rpa.state_store import (
                MessengerRpaStateStore, default_state_db_path as msg_db_path,
            )
            from pathlib import Path
            account_id = platform_cfg.get("account_id") or "default"
            if account_id and account_id != "default":
                db_path = Path(config_manager.config_path).parent / f"messenger_rpa_state_{account_id}.db"
            else:
                db_path = msg_db_path(config_manager.config_path)
            state = MessengerRpaStateStore(db_path)
            return MessengerRpaRunner(
                config_manager=config_manager,
                skill_manager=skill_manager,
                messenger_rpa_cfg=platform_cfg,
                state_store=state,
            )

    except Exception:
        logger.exception(
            "[DeviceCoordinatorService] 构建 %s runner 失败", platform_type
        )
    return None


class DeviceCoordinatorService:
    """从 config 构建并管理多个 DeviceCoordinator。"""

    def __init__(
        self,
        *,
        config_manager: Any,
        skill_manager: Any,
        dc_cfg: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._cm = config_manager
        self._sm = skill_manager
        self._cfg: Dict[str, Any] = dict(dc_cfg or {})
        self._coordinators: List[DeviceCoordinator] = []
        self._build_coordinators()

    def _build_coordinators(self) -> None:
        global_cfg = (self._cm.config or {})
        for dev in (self._cfg.get("devices") or []):
            if not isinstance(dev, dict):
                continue
            if not dev.get("enabled", True):
                continue
            serial = str(dev.get("serial") or "").strip()
            if not serial:
                logger.warning("[DeviceCoordinatorService] 设备缺少 serial，跳过")
                continue

            # Merge platforms: config.yaml first, then extend from registry
            platforms_cfg: List[Dict[str, Any]] = list(dev.get("platforms") or [])
            _cfg_types = {str(p.get("type", "")).lower() for p in platforms_cfg}
            _reg_dev_info: Optional[Dict[str, Any]] = None
            try:
                from src.shared.device_registry import get_device_registry
                _reg = get_device_registry()
                _reg_dev_info = _reg.get(serial)
                if _reg_dev_info:
                    for _ptype, _field, _pf in [
                        ("messenger", "platform_messenger", "persona_messenger"),
                        ("line", "platform_line", "persona_line"),
                        ("whatsapp", "platform_whatsapp", "persona_whatsapp"),
                    ]:
                        _aid = (_reg_dev_info.get(_field) or "").strip()
                        if _ptype not in _cfg_types:
                            if _aid:
                                _persona = (_reg_dev_info.get(_pf) or "").strip()
                                platforms_cfg.append({
                                    "type": _ptype, "account_id": _aid,
                                    "persona_id": _persona,
                                })
                                logger.info(
                                    "[DeviceCoordinatorService] %s/%s 从 registry 注入",
                                    serial[:8], _ptype,
                                )
                        else:
                            # Registry 字段为空 → 覆盖 config.yaml，移除该平台
                            if not _aid and _field in _reg_dev_info:
                                platforms_cfg = [
                                    p for p in platforms_cfg
                                    if str(p.get("type", "")).lower() != _ptype
                                ]
                                logger.info(
                                    "[DeviceCoordinatorService] %s/%s registry 禁用（覆盖 config）",
                                    serial[:8], _ptype,
                                )
                            else:
                                # config-based platform, inject persona from registry
                                _persona = (_reg_dev_info.get(_pf) or "").strip()
                                if _persona:
                                    for _p in platforms_cfg:
                                        if str(_p.get("type", "")).lower() == _ptype:
                                            _p.setdefault("persona_id", _persona)
            except Exception:
                pass

            platform_runners: List[PlatformRunner] = []
            for p in platforms_cfg:
                if not isinstance(p, dict):
                    continue
                ptype = str(p.get("type") or "").lower()
                if ptype not in ("line", "whatsapp", "messenger"):
                    logger.warning(
                        "[DeviceCoordinatorService] 未知平台类型 %r，跳过", ptype
                    )
                    continue
                aid = str(p.get("account_id") or "")
                pcfg = _find_account_cfg(global_cfg, ptype, aid)
                # 总是用 DeviceCoordinator 的设备 serial 覆盖（不能用 setdefault：
                # base config 可能已含其他设备的 adb_serial，会导致串设备 bug）
                pcfg["adb_serial"] = serial
                # _find_account_cfg 未找到匹配 account 时不会注入 account_id；
                # 保底注入，确保 _build_runner 能用正确的 account_id 命名 state DB
                if aid:
                    pcfg.setdefault("account_id", aid)
                # platform 条目里的额外键（type/account_id 除外）可以覆盖全局 cfg，
                # 例如 adb_keyboard_package / adb_keyboard_ime 的设备级个性化配置
                _reserved = {"type", "account_id"}
                for _k, _v in p.items():
                    if _k not in _reserved:
                        pcfg[_k] = _v
                # persona_id (scalar) → persona_ids (list) 转换，供 runner 读取
                _pid = str(pcfg.pop("persona_id", "") or "").strip()
                if _pid:
                    pcfg["persona_ids"] = [_pid]
                runner = _build_runner(ptype, pcfg, self._cm, self._sm)
                if runner is None:
                    logger.warning(
                        "[DeviceCoordinatorService] %s %s runner 构建失败，跳过",
                        serial, ptype,
                    )
                    continue
                platform_runners.append(PlatformRunner(ptype, runner, aid))
                logger.info(
                    "[DeviceCoordinatorService] 已构建 %s/%s runner (serial=%s)",
                    ptype, aid, serial,
                )

            if not platform_runners:
                logger.warning(
                    "[DeviceCoordinatorService] 设备 %s 无有效平台，跳过", serial
                )
                continue

            coord = DeviceCoordinator(
                serial=serial,
                platform_runners=platform_runners,
                label=str(dev.get("label") or serial[:8]),
                poll_interval_sec=float(dev.get("poll_interval_sec", 15) or 15),
                idle_poll_interval_sec=float(dev.get("idle_poll_interval_sec", 30) or 30),
                force_check_interval_sec=float(dev.get("force_check_interval_sec", 45) or 45),
                home_settle_sec=float(dev.get("home_settle_sec", 1.5) or 1.5),
                priority_by_badge=bool(dev.get("priority_by_badge", True)),
                run_timeout_sec=float(dev.get("run_timeout_sec", 180) or 180),
                circuit_breaker_threshold=int(dev.get("circuit_breaker_threshold", 5) or 5),
            )
            self._coordinators.append(coord)
            logger.info(
                "[DeviceCoordinatorService] DeviceCoordinator 已构建: serial=%s label=%s platforms=%s",
                serial, dev.get("label"), [p.platform_type for p in platform_runners],
            )

    async def start(self) -> None:
        for coord in self._coordinators:
            await coord.start()

    async def stop(self) -> None:
        for coord in self._coordinators:
            await coord.stop()

    def status(self) -> List[Dict[str, Any]]:
        return [c.status() for c in self._coordinators]

    def reset_circuit_breaker(
        self, serial: Optional[str] = None, platform_type: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """重置熔断器：清零 consecutive_fail + skip_until，立即允许重试。
        serial/platform_type 均为可选过滤器，不填则重置全部。
        """
        reset_list: List[Dict[str, Any]] = []
        for coord in self._coordinators:
            if serial and coord._serial != serial:
                continue
            for pr in coord._platform_runners:
                if platform_type and pr.platform_type != platform_type:
                    continue
                old_fail = pr.consecutive_fail
                old_skip = pr.skip_until
                pr.consecutive_fail = 0
                pr.skip_until = 0.0
                entry = {
                    "serial": coord._serial,
                    "platform": pr.platform_type,
                    "old_fail": old_fail,
                    "old_skip_until": old_skip,
                }
                reset_list.append(entry)
                logger.warning(
                    "[DeviceCoordinatorService] 熔断重置: serial=%s platform=%s "
                    "(was fail=%d skip_until=%.0f)",
                    coord._serial, pr.platform_type, old_fail, old_skip,
                )
        return reset_list

    async def rebuild_from_registry(self, serial: str) -> Dict[str, Any]:
        """停止指定 serial 的旧 Coordinator，从 registry 读取平台分配后重建。"""
        from src.shared.device_registry import get_device_registry

        result: Dict[str, Any] = {"serial": serial, "ok": False, "action": "none"}

        # Find device settings from config.yaml (for poll intervals etc.)
        global_cfg = (self._cm.config or {})
        dev_cfg: Optional[Dict[str, Any]] = next(
            (d for d in (self._cfg.get("devices") or [])
             if str(d.get("serial", "")).strip() == serial),
            None,
        )

        # Read platform assignments from registry
        reg = get_device_registry()
        dev_info = reg.get(serial)
        if not dev_info:
            result["error"] = "device not in registry"
            return result

        platforms_from_reg = []
        for ptype, field, persona_field in [
            ("messenger", "platform_messenger", "persona_messenger"),
            ("line", "platform_line", "persona_line"),
            ("whatsapp", "platform_whatsapp", "persona_whatsapp"),
        ]:
            aid = (dev_info.get(field) or "").strip()
            if aid:
                persona_id = (dev_info.get(persona_field) or "").strip()
                platforms_from_reg.append({
                    "type": ptype, "account_id": aid, "persona_id": persona_id,
                })

        if not platforms_from_reg:
            result["error"] = "no platform assignments in registry"
            return result

        # Stop and remove old coordinator
        old_coord: Optional[DeviceCoordinator] = None
        for i, coord in enumerate(self._coordinators):
            if coord._serial == serial:
                old_coord = coord
                self._coordinators.pop(i)
                break
        if old_coord:
            try:
                await old_coord.stop()
            except Exception as exc:
                logger.warning("[DeviceCoordinatorService] 停止旧 Coordinator 失败: %s", exc)

        # Build new platform runners
        platform_runners: List[PlatformRunner] = []
        for p in platforms_from_reg:
            ptype, aid = p["type"], p["account_id"]
            persona_id = p.get("persona_id", "")
            pcfg = _find_account_cfg(global_cfg, ptype, aid)
            pcfg["adb_serial"] = serial
            if aid:
                pcfg.setdefault("account_id", aid)
            if persona_id:
                pcfg["persona_ids"] = [persona_id]
            runner = _build_runner(ptype, pcfg, self._cm, self._sm)
            if runner is None:
                logger.warning("[DeviceCoordinatorService] %s/%s runner 构建失败，跳过", serial[:8], ptype)
                continue
            platform_runners.append(PlatformRunner(ptype, runner, aid))

        if not platform_runners:
            result["error"] = "all runners failed to build"
            return result

        _dc = dev_cfg or {}
        label = str(_dc.get("label") or dev_info.get("label") or serial[:8])
        coord = DeviceCoordinator(
            serial=serial,
            platform_runners=platform_runners,
            label=label,
            poll_interval_sec=float(_dc.get("poll_interval_sec", 15) or 15),
            idle_poll_interval_sec=float(_dc.get("idle_poll_interval_sec", 30) or 30),
            force_check_interval_sec=float(_dc.get("force_check_interval_sec", 45) or 45),
            priority_by_badge=bool(_dc.get("priority_by_badge", True)),
            run_timeout_sec=float(_dc.get("run_timeout_sec", 180) or 180),
            circuit_breaker_threshold=int(_dc.get("circuit_breaker_threshold", 5) or 5),
        )
        await coord.start()
        self._coordinators.append(coord)

        result["ok"] = True
        result["action"] = "rebuilt"
        result["platforms"] = [p["type"] for p in platforms_from_reg]
        logger.warning(
            "[DeviceCoordinatorService] ✅ %s (%s) 已从 registry 热重建，平台: %s",
            serial[:8], label, [pr.platform_type for pr in platform_runners],
        )
        return result

    @property
    def coordinators(self) -> List[DeviceCoordinator]:
        return list(self._coordinators)
