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


def setup_rpa_services(assistant):
    """装配三个 RPA 服务(Stage3,从 initialize() 原样迁出):LINE / Facebook
    Messenger / WhatsApp,均支持单/多账号,try/except 兜底,失败不挡启动。"""
    # LINE RPA 服务（单账号 or 多账号）
    try:
        _line_rpa_cfg = assistant.config.get_line_rpa_config() or {}
        if isinstance(_line_rpa_cfg, dict) and _line_rpa_cfg.get("enabled"):
            from src.integrations.line_rpa.service import LineRpaService
            _line_accounts = _line_rpa_cfg.get("accounts") or []
            if _line_accounts:
                for _acc in _line_accounts:
                    if not isinstance(_acc, dict) or not _acc.get("enabled", True):
                        continue
                    _acc_cfg = {**_line_rpa_cfg, **_acc}
                    _acc_cfg.pop("accounts", None)
                    _aid = _acc.get("account_id") or _acc.get("adb_serial", "default")
                    svc = LineRpaService(
                        config_manager=assistant.config,
                        skill_manager=assistant.skill_manager,
                        line_rpa_cfg=_acc_cfg,
                        account_id=_aid,
                    )
                    assistant.line_rpa_services.append(svc)
                    assistant.logger.info("LINE RPA 账号 [%s] 已构建 serial=%s", _aid, _acc.get("adb_serial"))
            else:
                svc = LineRpaService(
                    config_manager=assistant.config,
                    skill_manager=assistant.skill_manager,
                    line_rpa_cfg=_line_rpa_cfg,
                )
                assistant.line_rpa_services.append(svc)
                assistant.logger.info("LINE RPA 服务已构建（单账号，autostart 将在 start() 中决定）")
            assistant.line_rpa_service = assistant.line_rpa_services[0] if assistant.line_rpa_services else None
    except Exception as ex:
        assistant.logger.warning("LINE RPA 服务构建跳过: %s", ex)

    # Facebook Messenger RPA 服务（可选；主进程托管循环）
    try:
        _msgr_cfg = assistant.config.get_messenger_rpa_config() or {}
        if isinstance(_msgr_cfg, dict) and _msgr_cfg.get("enabled"):
            from src.integrations.messenger_rpa.service import MessengerRpaService
            assistant.messenger_rpa_service = MessengerRpaService(
                config_manager=assistant.config,
                skill_manager=assistant.skill_manager,
                messenger_rpa_cfg=_msgr_cfg,
            )
            assistant.logger.info(
                "Messenger RPA 服务已构建（autostart=%s）",
                bool(_msgr_cfg.get("autostart")),
            )
    except Exception as ex:
        assistant.logger.warning("Messenger RPA 服务构建跳过: %s", ex)

    # WhatsApp RPA 服务（单账号 or 多账号）
    try:
        _wa_cfg = (assistant.config.config or {}).get("whatsapp_rpa") or {}
        if isinstance(_wa_cfg, dict) and _wa_cfg.get("enabled"):
            from src.integrations.whatsapp_rpa.service import WhatsAppRpaService
            _wa_accounts = _wa_cfg.get("accounts") or []
            if _wa_accounts:
                for _acc in _wa_accounts:
                    if not isinstance(_acc, dict) or not _acc.get("enabled", True):
                        continue
                    _acc_cfg = {**_wa_cfg, **_acc}
                    _acc_cfg.pop("accounts", None)
                    _aid = _acc.get("account_id") or _acc.get("adb_serial", "default")
                    svc = WhatsAppRpaService(
                        config_manager=assistant.config,
                        skill_manager=assistant.skill_manager,
                        wa_cfg=_acc_cfg,
                        account_id=_aid,
                    )
                    assistant.whatsapp_rpa_services.append(svc)
                    assistant.logger.info("WhatsApp RPA 账号 [%s] 已构建 serial=%s", _aid, _acc.get("adb_serial"))
            else:
                svc = WhatsAppRpaService(
                    config_manager=assistant.config,
                    skill_manager=assistant.skill_manager,
                    wa_cfg=_wa_cfg,
                )
                assistant.whatsapp_rpa_services.append(svc)
                assistant.logger.info("WhatsApp RPA 服务已构建（单账号）")
            assistant.whatsapp_rpa_service = assistant.whatsapp_rpa_services[0] if assistant.whatsapp_rpa_services else None
    except Exception as ex:
        assistant.logger.warning("WhatsApp RPA 服务构建跳过: %s", ex)


async def setup_telegram_clients(assistant):
    """装配 config-Telegram 协议客户端(Stage3 专项,从 initialize() 原样迁出):
    构建账号注册表 -> N5 登录注册统一 -> 桌面/未配置则跳过、否则初始化单/多账号
    client。设置 assistant.telegram_client / telegram_clients。"""
    from src.client.telegram_client import TelegramClient
    from src.bootstrap.env_probe import _is_desktop_mode, _telegram_configured
    try:
        from src.client.telegram_account_registry import TelegramAccountRegistry
        tg_raw_cfg = (assistant.config.config or {}).get("telegram", {})
        _tg_registry = TelegramAccountRegistry.from_config(tg_raw_cfg)
    except Exception as _reg_ex:
        assistant.logger.warning("TelegramAccountRegistry 构建失败，回退单账号: %s", _reg_ex)
        _tg_registry = None

    # N5：登录注册统一（默认关）——把 A 线 config 账号并入 B 线持久注册表，
    # 与 QR 扫码登录共用一张 platform_accounts 表，供编排器/舰队视图看全。
    # 幂等且不破坏既有 QR 登录态（session_string/online 保留）。
    if _tg_registry is not None and bool(
        (tg_raw_cfg or {}).get("unify_login_registry", False)
    ):
        try:
            from src.integrations.account_registry import get_account_registry
            _synced = _tg_registry.sync_to_account_registry(
                get_account_registry()
            )
            assistant.logger.info(
                "[N5] config 账号已并入统一注册表：%s",
                ", ".join(_synced) or "（无）",
            )
        except Exception as _sync_ex:
            assistant.logger.warning("[N5] 登录注册统一同步失败（忽略）: %s", _sync_ex)

    # ★ 桌面/自包含可启动：协议号未真实配置（占位 example）或显式桌面模式时，
    #   跳过 config-Telegram 协议客户端初始化，让「纯收件箱/网页翻译」形态也能开机。
    #   （QR 扫码登录协议号走 orchestrator，不依赖此 config 账号）
    _tg_cfg = (assistant.config.config or {}).get("telegram", {})
    _desktop_mode = _is_desktop_mode(assistant.config.config)
    if _desktop_mode or not _telegram_configured(_tg_cfg):
        assistant.telegram_client = None
        assistant.telegram_clients = []
        assistant.logger.info(
            "Telegram 协议号未配置%s，跳过协议客户端初始化；"
            "统一收件箱 / 内嵌网页翻译 / RPA / QR 登录不受影响",
            "（桌面模式）" if _desktop_mode else "",
        )
    else:
        _primary_ctx = None if _tg_registry is None else _tg_registry.primary()
        _primary_cfg = _primary_ctx.account_cfg() if _primary_ctx else None

        assistant.telegram_client = TelegramClient(
            config=assistant.config,
            skill_manager=assistant.skill_manager,
            ai_client=assistant.ai_client,
            account_cfg=_primary_cfg,
        )
        await assistant.telegram_client.initialize()
        assistant.telegram_clients = [assistant.telegram_client]

        if _tg_registry is not None and _tg_registry.is_multi_account():
            for _ctx in _tg_registry.all_contexts()[1:]:
                try:
                    _tc = TelegramClient(
                        config=assistant.config,
                        skill_manager=assistant.skill_manager,
                        ai_client=assistant.ai_client,
                        account_cfg=_ctx.account_cfg(),
                    )
                    await _tc.initialize()
                    assistant.telegram_clients.append(_tc)
                    assistant.logger.info(
                        "Telegram 账号 [%s] 初始化成功", _ctx.account_id
                    )
                except Exception as _tc_ex:
                    assistant.logger.warning(
                        "Telegram 账号 [%s] 初始化失败，跳过: %s",
                        _ctx.account_id, _tc_ex,
                    )

        assistant.logger.info(
            "Telegram 客户端初始化完成（%d 个账号）", len(assistant.telegram_clients)
        )
