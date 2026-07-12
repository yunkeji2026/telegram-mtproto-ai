"""initialize() 的服务装配步骤(Stage3,从 main.py 原样迁出,行为不变)。

每个 setup_* 接收 assistant(=self)并就地装配一个子系统/服务,side-effect 式
地设置 assistant.X 或注册回调;失败均由块内 try/except 兜底,绝不挡主启动。
"""
from __future__ import annotations

from pathlib import Path


def setup_contacts_subsystem(assistant):
    """装配 Contacts 跨平台子系统(feature flag 控制)并把 ContactHooks 注入
    LINE/Messenger/WhatsApp RPA 服务、注册进程级 intimacy provider 与 cap-alert。"""
    try:
        from src.contacts import bootstrap_contacts_subsystem
        cfg_dir_for_contacts = Path(assistant.config.config_path).parent
        assistant.contacts = bootstrap_contacts_subsystem(
            assistant.config, cfg_dir_for_contacts,
        )
        if assistant.contacts is not None:
            assistant.logger.info(
                "Contacts 子系统已启用（daily_cap=%s, readiness_threshold=%s）",
                assistant.contacts.config_snapshot.get("daily_cap", 15),
                assistant.contacts.config_snapshot.get("readiness_threshold", 70),
            )
            # W4-定时：启动 silence_decay 后台循环（0 则跳过）
            try:
                assistant.contacts.start_background_tasks()
            except Exception:
                assistant.logger.warning(
                    "Contacts 后台任务启动失败", exc_info=True)
            # W4-Runner：把 ContactHooks 后置注入两个 RPA 服务，
            # 这样线上每条 inbound/outbound 都会被记到 contacts DB。
            # W4-Hooks-Flag：允许按 channel 单独关闭（灰度或隔离排错）。
            _hooks = assistant.contacts.hooks
            # Q3：把同一套 IntimacyEngine 事实源注册为进程级 provider，
            # 让 A 线 Telegram（含 companion 运行时）也吃上 intimacy/funnel
            # → companion_relationship 双信号融合。telegram hook 也受同一开关控制。
            try:
                from src.utils.companion_context import (
                    set_relationship_providers,
                )
                if assistant.contacts.is_rpa_hook_enabled("telegram"):
                    # 只读查询：始终注册（无数据时 resolve_* 返回 None，安全）
                    set_relationship_providers(
                        intimacy_lookup=getattr(
                            _hooks, "get_journey_intimacy", None),
                        funnel_lookup=getattr(
                            _hooks, "get_journey_funnel_stage", None),
                    )
                    # 写入 contacts（生成 journey + 刷新 intimacy）：仅在显式
                    # 开启时注册 → 默认零行为变化，避免意外激活下游流程。
                    _cfg_all = (
                        assistant.config.config
                        if hasattr(assistant.config, "config") else {}
                    )
                    _tg_login = (
                        (_cfg_all.get("platform_login") or {})
                        .get("telegram") or {}
                    )
                    if _tg_login.get("contacts_recording", False):
                        set_relationship_providers(
                            message_recorder=getattr(
                                _hooks, "on_message", None),
                            story_recorder=getattr(
                                _hooks, "on_story_complete", None),
                        )
                        assistant.logger.info(
                            "Telegram A 线已接入关系事实源 "
                            "(intimacy/funnel + 收发记录 + 剧情镜像写入 contacts)")
                    else:
                        assistant.logger.info(
                            "Telegram A 线已接入关系事实源 "
                            "(只读 intimacy/funnel；contacts_recording 未开)")
                else:
                    assistant.logger.info(
                        "Telegram 关系事实源已按配置禁用 "
                        "(contacts.rpa_hooks.telegram=false)")
            except Exception:
                assistant.logger.warning(
                    "set_relationship_providers 失败", exc_info=True)
            if assistant.messenger_rpa_service is not None:
                if assistant.contacts.is_rpa_hook_enabled("messenger"):
                    try:
                        assistant.messenger_rpa_service.set_contact_hooks(_hooks)
                        assistant.logger.info(
                            "Messenger RPA 已接入 ContactHooks")
                    except Exception:
                        assistant.logger.warning(
                            "Messenger RPA set_contact_hooks 失败",
                            exc_info=True)
                else:
                    assistant.logger.info(
                        "Messenger RPA ContactHooks 已按配置禁用 "
                        "(contacts.rpa_hooks.messenger=false)")
            if assistant.line_rpa_service is not None:
                if assistant.contacts.is_rpa_hook_enabled("line"):
                    try:
                        assistant.line_rpa_service.set_contact_hooks(_hooks)
                        assistant.logger.info(
                            "LINE RPA 已接入 ContactHooks")
                    except Exception:
                        assistant.logger.warning(
                            "LINE RPA set_contact_hooks 失败",
                            exc_info=True)
                else:
                    assistant.logger.info(
                        "LINE RPA ContactHooks 已按配置禁用 "
                        "(contacts.rpa_hooks.line=false)")
            for _wsvc in assistant.whatsapp_rpa_services:
                if assistant.contacts.is_rpa_hook_enabled("whatsapp"):
                    try:
                        _wsvc.set_contact_hooks(_hooks)
                        assistant.logger.info(
                            "WhatsApp RPA [%s] 已接入 ContactHooks",
                            getattr(_wsvc, "account_id", "?"))
                    except Exception:
                        assistant.logger.warning(
                            "WhatsApp RPA set_contact_hooks 失败",
                            exc_info=True)
                else:
                    assistant.logger.info(
                        "WhatsApp RPA ContactHooks 已按配置禁用 "
                        "(contacts.rpa_hooks.whatsapp=false)")
    except Exception as ex:
        assistant.logger.warning("Contacts 子系统启动跳过: %s", ex)


def setup_device_management(assistant):
    """装配设备管理三件套(Stage3,从 initialize() 原样迁出):多平台设备协调器、
    设备注册表 DB、ADB 热插拔 Watcher。均 try/except 兜底,失败不挡启动。"""
    # 多平台设备协调器（Device Coordinator）
    try:
        _dc_cfg = (assistant.config.config or {}).get("device_coordinator") or {}
        if isinstance(_dc_cfg, dict) and _dc_cfg.get("enabled"):
            from src.integrations.shared.device_service import DeviceCoordinatorService
            assistant.device_coordinator_service = DeviceCoordinatorService(
                config_manager=assistant.config,
                skill_manager=assistant.skill_manager,
                dc_cfg=_dc_cfg,
            )
            assistant.logger.info("DeviceCoordinatorService 已构建")
    except Exception as ex:
        assistant.logger.warning("DeviceCoordinatorService 构建跳过: %s", ex)

    # 初始化设备注册表 DB（可配置路径，支持远程主机不同路径）
    try:
        _reg_cfg = (assistant.config.config or {}).get("device_registry") or {}
        _reg_db_path = _reg_cfg.get("db_path", "")
        if _reg_db_path:
            from src.shared.device_registry import get_device_registry
            get_device_registry(_reg_db_path)
            assistant.logger.info("DeviceRegistry 初始化（db=%s）", _reg_db_path)
    except Exception as ex:
        assistant.logger.warning("DeviceRegistry 初始化跳过: %s", ex)

    # ADB 热插拔自动纳管（HotPlug Watcher）
    try:
        _hp_cfg = (assistant.config.config or {}).get("hotplug_watcher") or {}
        # 默认启用（只要 device_coordinator 启用）
        _hp_enabled = _hp_cfg.get("enabled", bool(assistant.device_coordinator_service))
        if _hp_enabled:
            from src.integrations.shared.hotplug_watcher import HotPlugWatcher
            # 收集静态配置中已管理的 serial，防止重复纳管
            _static_serials = set()
            if assistant.device_coordinator_service:
                for c in assistant.device_coordinator_service.coordinators:
                    _static_serials.add(c._serial)
            _host_name = str(_hp_cfg.get("host_name", "")).strip()
            assistant.hotplug_watcher = HotPlugWatcher(
                config_manager=assistant.config,
                skill_manager=assistant.skill_manager,
                scan_interval_sec=float(_hp_cfg.get("scan_interval_sec", 15)),
                static_serials=_static_serials,
                host_name=_host_name,
                offline_timeout_sec=float(_hp_cfg.get("offline_timeout_sec", 30)),
            )
            assistant.logger.info(
                "HotPlugWatcher 已构建（host=%s, 静态设备: %d 台）",
                _host_name or "(all)", len(_static_serials),
            )
    except Exception as ex:
        assistant.logger.warning("HotPlugWatcher 构建跳过: %s", ex)
