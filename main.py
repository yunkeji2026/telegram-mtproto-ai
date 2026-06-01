#!/usr/bin/env python3
"""
Telegram MTProto AI Chat Assistant 主程序入口

基于 Telegram User API (MTProto) + 大模型 API + Skill 工作流的自动化客服/对话系统。
"""

import asyncio
import sys
import signal
import logging
import threading
import os
from pathlib import Path

# Windows console 默认 cp936；强制 UTF-8 防止日文/emoji 被 stdout 重定向时损坏。
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# 添加项目根目录到Python路径
sys.path.insert(0, str(Path(__file__).parent))

from src.client.telegram_client import TelegramClient
from src.ai.ai_client import AIClient
from src.skills.skill_manager import SkillManager
from src.utils.config_manager import ConfigManager
from src.utils.logger import setup_logger
from src.utils.net_helpers import is_bind_address_in_use_error
from src.utils.domain_policy import effective_domain_name


def _resolve_mobile_auto_openclaw_db(config, config_path) -> str:
    """Resolve mobile-auto0423 openclaw.db with workspace-adjacent defaults."""
    root = config if isinstance(config, dict) else {}
    bridge_cfg = root.get("mobile_bridge") if isinstance(root.get("mobile_bridge"), dict) else {}
    explicit = str(bridge_cfg.get("openclaw_db_path") or "").strip()
    if explicit:
        p = Path(explicit).expanduser()
        if not p.is_absolute():
            p = Path(config_path).parent / p
        return str(p)

    mr_cfg = root.get("messenger_rpa") if isinstance(root.get("messenger_rpa"), dict) else {}
    ma_cfg = mr_cfg.get("mobile_auto") if isinstance(mr_cfg.get("mobile_auto"), dict) else {}
    candidates = []
    ma_db = str(ma_cfg.get("openclaw_db_path") or "").strip()
    if ma_db:
        candidates.append(Path(ma_db).expanduser())
    ma_root = str(ma_cfg.get("root_path") or mr_cfg.get("mobile_auto_root") or "").strip()
    if ma_root:
        candidates.append(Path(ma_root).expanduser() / "data" / "openclaw.db")

    cfg_path = Path(config_path).resolve()
    repo_root = cfg_path.parent.parent
    workspace_root = repo_root.parent
    candidates.extend([
        workspace_root / "mobile-auto0423" / "data" / "openclaw.db",
        repo_root / "mobile-auto0423" / "data" / "openclaw.db",
    ])

    for p in candidates:
        try:
            if p.exists():
                return str(p)
        except Exception:
            continue
    return str(candidates[0]) if candidates else str(
        workspace_root / "mobile-auto0423" / "data" / "openclaw.db"
    )


class AIChatAssistant:
    """AI聊天助手主类"""
    
    def __init__(self):
        """初始化AI聊天助手"""
        self.config = None
        self.telegram_client = None          # primary (backward-compat)
        self.telegram_clients: list = []     # all accounts (including primary)
        self.ai_client = None
        self.skill_manager = None
        self.logger = None
        self.running = False
        self.line_rpa_service = None         # primary (backward-compat)
        self.line_rpa_services: list = []      # all LINE accounts
        self.messenger_rpa_service = None
        self.whatsapp_rpa_service = None        # primary (backward-compat)
        self.whatsapp_rpa_services: list = []   # all WhatsApp accounts
        self.device_coordinator_service = None   # 多平台设备协调器（可选）
        self.hotplug_watcher = None              # ADB 热插拔自动纳管（可选）
        # Phase A：统一收件箱持久层（纯旁路；store 故障/为空自动回落实时聚合）
        self.inbox_store = None  # type: Optional[Any]  # noqa: F821
        # Phase C：翻译记忆持久层（可选）
        self.translation_memory = None  # type: Optional[Any]  # noqa: F821
        # Phase D：电商工具服务（可选）
        self.ecommerce_tools = None  # type: Optional[Any]  # noqa: F821
        # W2-W4：跨平台 Contacts 子系统（feature flag 控制；默认关）
        self.contacts = None  # type: Optional["ContactsSubsystem"]  # noqa: F821
        # Mobile Bridge：mobile-auto0423 ↔ telegram-mtproto-ai 双向同步
        self.mobile_bridge = None  # type: Optional[Any]  # noqa: F821
        self._telegram_task = None
        self._secondary_tg_tasks: list = []  # extra account tasks
        # D: web admin 隔离到独立线程
        self._web_thread = None
        self._web_loop = None
        self._web_server = None
        # W2-D4: 主动唤醒循环引用（关程序时 stop）
        self._reactivation_loop = None
        
    async def initialize(self):
        """初始化所有组件"""
        try:
            # 1. 先设置一个临时的控制台日志记录器
            self.logger = setup_logger(log_file=None, console_output=True)
            self.logger.info("开始初始化AI聊天助手...")
            
            # 2. 加载配置
            self.config = ConfigManager()
            await self.config.load()
            self.logger.info("配置加载成功")
            
            # 3. 根据配置重新配置日志记录器
            log_config = self.config.config.get("logging", {})
            if log_config:
                log_file = log_config.get("file")
                log_level = log_config.get("level", "INFO")
                console_output = log_config.get("console_output", True)
                
                # 设置日志记录器级别
                level = getattr(logging, log_level.upper(), logging.INFO)
                self.logger.setLevel(level)
                
                # 重新配置日志记录器
                self.logger.handlers.clear()
                
                # 控制台处理器（强制 UTF-8，避免 GBK 编码 emoji 失败）
                if console_output:
                    _utf8_stdout = open(sys.stdout.fileno(), mode='w',
                                        encoding='utf-8', errors='replace',
                                        closefd=False)
                    console_handler = logging.StreamHandler(_utf8_stdout)
                    console_handler.setLevel(level)
                    console_formatter = logging.Formatter(
                        '[%(asctime)s] [%(levelname)s] %(name)s: %(message)s',
                        datefmt='%Y-%m-%d %H:%M:%S'
                    )
                    console_handler.setFormatter(console_formatter)
                    self.logger.addHandler(console_handler)
                
                # 文件处理器（RotatingFileHandler 自动轮转）
                if log_file:
                    os.makedirs(os.path.dirname(log_file), exist_ok=True)
                    from logging.handlers import RotatingFileHandler
                    max_bytes = int(log_config.get("max_size_mb", 10)) * 1024 * 1024
                    backup_count = int(log_config.get("backup_count", 5))
                    file_handler = RotatingFileHandler(
                        log_file, maxBytes=max_bytes, backupCount=backup_count,
                        encoding='utf-8',
                    )
                    file_handler.setLevel(level)
                    file_formatter = logging.Formatter(
                        '[%(asctime)s] [%(levelname)s] %(name)s: %(message)s',
                        datefmt='%Y-%m-%d %H:%M:%S'
                    )
                    file_handler.setFormatter(file_formatter)
                    self.logger.addHandler(file_handler)
                    # 防止 ai_chat_assistant 消息被 root handler 再写一次（duplicate）
                    self.logger.propagate = False
                    # ★ 让非 ai_chat_assistant 家族的 logger（如 src.integrations.messenger_rpa.*）
                    # 也能落盘到 app.log；尤其是运行时告警、异常追踪
                    try:
                        root_logger = logging.getLogger()
                        # 避免对 root 造成过度 verbose，最低仍设为 WARNING
                        root_level = max(level, logging.WARNING)
                        if root_logger.level > root_level or root_logger.level == 0:
                            root_logger.setLevel(root_level)
                        # 避免重复添加（热重启场景）
                        have_same = any(
                            isinstance(h, RotatingFileHandler)
                            and getattr(h, "baseFilename", "") ==
                            getattr(file_handler, "baseFilename", "")
                            for h in root_logger.handlers
                        )
                        if not have_same:
                            root_logger.addHandler(file_handler)
                    except Exception:
                        pass
                
                self.logger.info(f"日志已重新配置: level={log_level}, file={log_file}")
            
            # 3. 初始化AI客户端
            self.ai_client = AIClient(self.config)
            await self.ai_client.initialize()
            self.logger.info("AI客户端初始化成功")
            
            # 4. 初始化Skill管理器
            self.skill_manager = SkillManager(self.config, self.ai_client)
            await self.skill_manager.initialize()
            self.logger.info("Skill管理器初始化成功")
            
            # 5. 初始化Telegram客户端（支持多账号并行）
            try:
                from src.client.telegram_account_registry import TelegramAccountRegistry
                tg_raw_cfg = (self.config.config or {}).get("telegram", {})
                _tg_registry = TelegramAccountRegistry.from_config(tg_raw_cfg)
            except Exception as _reg_ex:
                self.logger.warning("TelegramAccountRegistry 构建失败，回退单账号: %s", _reg_ex)
                _tg_registry = None

            _primary_ctx = None if _tg_registry is None else _tg_registry.primary()
            _primary_cfg = _primary_ctx.account_cfg() if _primary_ctx else None

            self.telegram_client = TelegramClient(
                config=self.config,
                skill_manager=self.skill_manager,
                ai_client=self.ai_client,
                account_cfg=_primary_cfg,
            )
            await self.telegram_client.initialize()
            self.telegram_clients = [self.telegram_client]

            if _tg_registry is not None and _tg_registry.is_multi_account():
                for _ctx in _tg_registry.all_contexts()[1:]:
                    try:
                        _tc = TelegramClient(
                            config=self.config,
                            skill_manager=self.skill_manager,
                            ai_client=self.ai_client,
                            account_cfg=_ctx.account_cfg(),
                        )
                        await _tc.initialize()
                        self.telegram_clients.append(_tc)
                        self.logger.info(
                            "Telegram 账号 [%s] 初始化成功", _ctx.account_id
                        )
                    except Exception as _tc_ex:
                        self.logger.warning(
                            "Telegram 账号 [%s] 初始化失败，跳过: %s",
                            _ctx.account_id, _tc_ex,
                        )

            self.logger.info(
                "Telegram 客户端初始化完成（%d 个账号）", len(self.telegram_clients)
            )
            
            self.logger.info("✅ AI聊天助手初始化完成")

            self._startup_advisory_events = []
            try:
                from src.utils.config_advisories import (
                    collect_production_advisories,
                    log_advisory_events,
                )

                self._startup_advisory_events = collect_production_advisories(
                    self.config.config or {}
                )
                log_advisory_events(self.logger, self._startup_advisory_events)
            except Exception:
                self.logger.debug("config_advisories 跳过", exc_info=True)

            try:
                ev = getattr(self, "_startup_advisory_events", []) or []
                wn = sum(
                    1
                    for e in ev
                    if str(getattr(e, "level", "")).lower() == "warning"
                )
                from src.monitoring.metrics_store import get_metrics_store

                get_metrics_store().set_startup_advisory_counts(len(ev), wn)
            except Exception:
                self.logger.debug("startup_advisory metrics 跳过", exc_info=True)

            # LINE RPA 服务（单账号 or 多账号）
            try:
                _line_rpa_cfg = self.config.get_line_rpa_config() or {}
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
                                config_manager=self.config,
                                skill_manager=self.skill_manager,
                                line_rpa_cfg=_acc_cfg,
                                account_id=_aid,
                            )
                            self.line_rpa_services.append(svc)
                            self.logger.info("LINE RPA 账号 [%s] 已构建 serial=%s", _aid, _acc.get("adb_serial"))
                    else:
                        svc = LineRpaService(
                            config_manager=self.config,
                            skill_manager=self.skill_manager,
                            line_rpa_cfg=_line_rpa_cfg,
                        )
                        self.line_rpa_services.append(svc)
                        self.logger.info("LINE RPA 服务已构建（单账号，autostart 将在 start() 中决定）")
                    self.line_rpa_service = self.line_rpa_services[0] if self.line_rpa_services else None
            except Exception as ex:
                self.logger.warning("LINE RPA 服务构建跳过: %s", ex)

            # Facebook Messenger RPA 服务（可选；主进程托管循环）
            try:
                _msgr_cfg = self.config.get_messenger_rpa_config() or {}
                if isinstance(_msgr_cfg, dict) and _msgr_cfg.get("enabled"):
                    from src.integrations.messenger_rpa.service import MessengerRpaService
                    self.messenger_rpa_service = MessengerRpaService(
                        config_manager=self.config,
                        skill_manager=self.skill_manager,
                        messenger_rpa_cfg=_msgr_cfg,
                    )
                    self.logger.info(
                        "Messenger RPA 服务已构建（autostart=%s）",
                        bool(_msgr_cfg.get("autostart")),
                    )
            except Exception as ex:
                self.logger.warning("Messenger RPA 服务构建跳过: %s", ex)

            # WhatsApp RPA 服务（单账号 or 多账号）
            try:
                _wa_cfg = (self.config.config or {}).get("whatsapp_rpa") or {}
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
                                config_manager=self.config,
                                skill_manager=self.skill_manager,
                                wa_cfg=_acc_cfg,
                                account_id=_aid,
                            )
                            self.whatsapp_rpa_services.append(svc)
                            self.logger.info("WhatsApp RPA 账号 [%s] 已构建 serial=%s", _aid, _acc.get("adb_serial"))
                    else:
                        svc = WhatsAppRpaService(
                            config_manager=self.config,
                            skill_manager=self.skill_manager,
                            wa_cfg=_wa_cfg,
                        )
                        self.whatsapp_rpa_services.append(svc)
                        self.logger.info("WhatsApp RPA 服务已构建（单账号）")
                    self.whatsapp_rpa_service = self.whatsapp_rpa_services[0] if self.whatsapp_rpa_services else None
            except Exception as ex:
                self.logger.warning("WhatsApp RPA 服务构建跳过: %s", ex)

            # 多平台设备协调器（Device Coordinator）
            try:
                _dc_cfg = (self.config.config or {}).get("device_coordinator") or {}
                if isinstance(_dc_cfg, dict) and _dc_cfg.get("enabled"):
                    from src.integrations.shared.device_service import DeviceCoordinatorService
                    self.device_coordinator_service = DeviceCoordinatorService(
                        config_manager=self.config,
                        skill_manager=self.skill_manager,
                        dc_cfg=_dc_cfg,
                    )
                    self.logger.info("DeviceCoordinatorService 已构建")
            except Exception as ex:
                self.logger.warning("DeviceCoordinatorService 构建跳过: %s", ex)

            # 初始化设备注册表 DB（可配置路径，支持远程主机不同路径）
            try:
                _reg_cfg = (self.config.config or {}).get("device_registry") or {}
                _reg_db_path = _reg_cfg.get("db_path", "")
                if _reg_db_path:
                    from src.shared.device_registry import get_device_registry
                    get_device_registry(_reg_db_path)
                    self.logger.info("DeviceRegistry 初始化（db=%s）", _reg_db_path)
            except Exception as ex:
                self.logger.warning("DeviceRegistry 初始化跳过: %s", ex)

            # ADB 热插拔自动纳管（HotPlug Watcher）
            try:
                _hp_cfg = (self.config.config or {}).get("hotplug_watcher") or {}
                # 默认启用（只要 device_coordinator 启用）
                _hp_enabled = _hp_cfg.get("enabled", bool(self.device_coordinator_service))
                if _hp_enabled:
                    from src.integrations.shared.hotplug_watcher import HotPlugWatcher
                    # 收集静态配置中已管理的 serial，防止重复纳管
                    _static_serials = set()
                    if self.device_coordinator_service:
                        for c in self.device_coordinator_service.coordinators:
                            _static_serials.add(c._serial)
                    _host_name = str(_hp_cfg.get("host_name", "")).strip()
                    self.hotplug_watcher = HotPlugWatcher(
                        config_manager=self.config,
                        skill_manager=self.skill_manager,
                        scan_interval_sec=float(_hp_cfg.get("scan_interval_sec", 15)),
                        static_serials=_static_serials,
                        host_name=_host_name,
                        offline_timeout_sec=float(_hp_cfg.get("offline_timeout_sec", 30)),
                    )
                    self.logger.info(
                        "HotPlugWatcher 已构建（host=%s, 静态设备: %d 台）",
                        _host_name or "(all)", len(_static_serials),
                    )
            except Exception as ex:
                self.logger.warning("HotPlugWatcher 构建跳过: %s", ex)

            # ── Contacts 跨平台子系统（feature flag 控制）──
            try:
                from src.contacts import bootstrap_contacts_subsystem
                cfg_dir_for_contacts = Path(self.config.config_path).parent
                self.contacts = bootstrap_contacts_subsystem(
                    self.config, cfg_dir_for_contacts,
                )
                if self.contacts is not None:
                    self.logger.info(
                        "Contacts 子系统已启用（daily_cap=%s, readiness_threshold=%s）",
                        self.contacts.config_snapshot.get("daily_cap", 15),
                        self.contacts.config_snapshot.get("readiness_threshold", 70),
                    )
                    # W4-定时：启动 silence_decay 后台循环（0 则跳过）
                    try:
                        self.contacts.start_background_tasks()
                    except Exception:
                        self.logger.warning(
                            "Contacts 后台任务启动失败", exc_info=True)
                    # W4-Runner：把 ContactHooks 后置注入两个 RPA 服务，
                    # 这样线上每条 inbound/outbound 都会被记到 contacts DB。
                    # W4-Hooks-Flag：允许按 channel 单独关闭（灰度或隔离排错）。
                    _hooks = self.contacts.hooks
                    if self.messenger_rpa_service is not None:
                        if self.contacts.is_rpa_hook_enabled("messenger"):
                            try:
                                self.messenger_rpa_service.set_contact_hooks(_hooks)
                                self.logger.info(
                                    "Messenger RPA 已接入 ContactHooks")
                            except Exception:
                                self.logger.warning(
                                    "Messenger RPA set_contact_hooks 失败",
                                    exc_info=True)
                        else:
                            self.logger.info(
                                "Messenger RPA ContactHooks 已按配置禁用 "
                                "(contacts.rpa_hooks.messenger=false)")
                    if self.line_rpa_service is not None:
                        if self.contacts.is_rpa_hook_enabled("line"):
                            try:
                                self.line_rpa_service.set_contact_hooks(_hooks)
                                self.logger.info(
                                    "LINE RPA 已接入 ContactHooks")
                            except Exception:
                                self.logger.warning(
                                    "LINE RPA set_contact_hooks 失败",
                                    exc_info=True)
                        else:
                            self.logger.info(
                                "LINE RPA ContactHooks 已按配置禁用 "
                                "(contacts.rpa_hooks.line=false)")
                    for _wsvc in self.whatsapp_rpa_services:
                        if self.contacts.is_rpa_hook_enabled("whatsapp"):
                            try:
                                _wsvc.set_contact_hooks(_hooks)
                                self.logger.info(
                                    "WhatsApp RPA [%s] 已接入 ContactHooks",
                                    getattr(_wsvc, "account_id", "?"))
                            except Exception:
                                self.logger.warning(
                                    "WhatsApp RPA set_contact_hooks 失败",
                                    exc_info=True)
                        else:
                            self.logger.info(
                                "WhatsApp RPA ContactHooks 已按配置禁用 "
                                "(contacts.rpa_hooks.whatsapp=false)")
            except Exception as ex:
                self.logger.warning("Contacts 子系统启动跳过: %s", ex)

            # ── Mobile Bridge（依赖 Contacts 子系统，仅 contacts 启用时构建）──
            if self.contacts is not None:
                try:
                    from src.contacts.mobile_bridge import MobileBridgeService
                    _bridge_cfg = (self.config.config or {}).get("mobile_bridge", {})
                    _mr_cfg = (self.config.config or {}).get("messenger_rpa", {})
                    _ma_cfg = _mr_cfg.get("mobile_auto", {}) if isinstance(_mr_cfg, dict) else {}
                    _openclaw_path = _resolve_mobile_auto_openclaw_db(
                        self.config.config or {},
                        self.config.config_path,
                    )
                    _mobile_api = (
                        _bridge_cfg.get("mobile_api_base")
                        or (_ma_cfg.get("api_base") if isinstance(_ma_cfg, dict) else "")
                        or "http://127.0.0.1:18080"
                    )
                    _poll_interval = float(_bridge_cfg.get("poll_interval_sec", 15))
                    self.mobile_bridge = MobileBridgeService(
                        contacts_store=self.contacts.store,
                        openclaw_db_path=_openclaw_path,
                        mobile_api_base=_mobile_api,
                        poll_interval_sec=_poll_interval,
                    )
                    self.logger.info(
                        "Mobile Bridge 已构建 (openclaw=%s mobile_api=%s poll=%.0fs)",
                        _openclaw_path, _mobile_api, _poll_interval,
                    )
                except Exception as ex:
                    self.logger.warning("Mobile Bridge 构建跳过: %s", ex)

            # Web 管理后台
            web_cfg = self.config.config.get("web_admin", {})
            if web_cfg.get("enabled"):
                try:
                    import uvicorn
                    from src.web.admin import create_app
                    from src.utils.audit_store import AuditStore
                    from src.utils.webhook import WebhookNotifier
                    from src.utils.log_buffer import install_log_buffer
                    _log_buf = install_log_buffer()
                    cfg_dir = Path(self.config.config_path).parent
                    wh_cfg = self.config.config.get("webhook", {})
                    webhook = WebhookNotifier(wh_cfg) if wh_cfg.get("enabled") else None
                    # W4-Cap-Alert：contacts 已 bootstrap 且 webhook 就绪 → 把 cap 阈值事件接上
                    if self.contacts is not None and webhook is not None:
                        try:
                            self.contacts.wire_cap_alert_webhook(webhook)
                        except Exception:
                            self.logger.debug(
                                "wire_cap_alert_webhook 失败", exc_info=True)
                    audit = AuditStore(
                        db_path=cfg_dir / "audit.db",
                        legacy_jsonl_path=cfg_dir / "audit_log.jsonl",
                        webhook_notifier=webhook,
                    )
                    audit.cleanup(keep_days=90, max_rows=50000)
                    try:
                        from src.utils.config_advisories import (
                            record_warning_advisories_to_audit,
                        )

                        n = record_warning_advisories_to_audit(
                            audit, getattr(self, "_startup_advisory_events", []) or []
                        )
                        if n:
                            self.logger.debug("已将 %s 条配置告警写入审计", n)
                        try:
                            from src.monitoring.metrics_store import get_metrics_store

                            get_metrics_store().set_startup_advisory_audit_logged(n)
                        except Exception:
                            self.logger.debug(
                                "startup_advisory audit metrics 跳过", exc_info=True
                            )
                    except Exception:
                        self.logger.debug("配置告警写入审计跳过", exc_info=True)
                    web_app = create_app(self.config, audit_store=audit,
                                        boot_ts=self.telegram_client._boot_timestamp,
                                        telegram_client=self.telegram_client,
                                        event_tracker=self.telegram_client.event_tracker,
                                        log_buffer=_log_buf)
                    if self.line_rpa_service is not None:
                        web_app.state.line_rpa_service = self.line_rpa_service
                    web_app.state.line_rpa_services = self.line_rpa_services
                    if self.messenger_rpa_service is not None:
                        web_app.state.messenger_rpa_service = self.messenger_rpa_service
                    if self.whatsapp_rpa_service is not None:
                        web_app.state.whatsapp_rpa_service = self.whatsapp_rpa_service
                    web_app.state.whatsapp_rpa_services = self.whatsapp_rpa_services
                    if self.device_coordinator_service is not None:
                        web_app.state.device_coordinator_service = self.device_coordinator_service
                    if self.hotplug_watcher is not None:
                        web_app.state.hotplug_watcher = self.hotplug_watcher

                    # ── 统一收件箱持久层（Phase A：纯旁路，store 故障/为空自动回落） ──
                    try:
                        _inbox_cfg = (self.config.config or {}).get("inbox", {}) or {}
                        if _inbox_cfg.get("enabled", True):
                            from src.inbox.store import InboxStore

                            _cfg_dir = Path(self.config.config_path).parent
                            _inbox_db = Path(_inbox_cfg.get("db_path") or (_cfg_dir / "inbox.db"))
                            if not _inbox_db.is_absolute():
                                _inbox_db = _cfg_dir / _inbox_db
                            self.inbox_store = InboxStore(_inbox_db)
                            web_app.state.inbox_store = self.inbox_store
                            self.logger.info("统一收件箱持久层已挂载（%s）", _inbox_db)

                            # ── Phase B：统一草稿/审批层（read-through 聚合 4 平台源表） ──
                            from src.inbox.drafts import DraftService
                            from src.web.routes.drafts_routes import register_drafts_routes
                            from src.ai.chat_assistant_service import quick_risk as _quick_risk

                            draft_svc = DraftService(
                                inbox_store=self.inbox_store,
                                line_services=self.line_rpa_services or [],
                                wa_services=self.whatsapp_rpa_services or [],
                                messenger_service=self.messenger_rpa_service,
                                risk_fn=_quick_risk,
                            )
                            web_app.state.draft_service = draft_svc

                            def _drafts_api_auth(request):
                                if hasattr(web_app.state, "require_role"):
                                    web_app.state.require_role(request, "line_rpa")

                            register_drafts_routes(web_app, api_auth=_drafts_api_auth)
                            self.logger.info("统一草稿层已挂载（/api/drafts）")

                            # ── Phase C：意图 LLM 升级 + 翻译记忆持久化（预置带依赖的 service） ──
                            _cfg_root = self.config.config or {}
                            _ia_cfg = _cfg_root.get("intent_analysis", {}) or {}
                            _tr_cfg = _cfg_root.get("translation", {}) or {}
                            from src.ai.chat_assistant_service import ChatAssistantService
                            web_app.state.chat_assistant_service = ChatAssistantService(
                                ai_client=self.ai_client,
                                use_llm=bool(_ia_cfg.get("use_llm", False)),
                                analysis_store=self.inbox_store,
                                timeout_sec=float(_ia_cfg.get("timeout_sec", 8) or 8),
                            )
                            _tm_store = None
                            if (_tr_cfg.get("memory", {}) or {}).get("enabled", True):
                                from src.ai.translation_memory import TranslationMemoryStore
                                _tm_db = Path(
                                    (_tr_cfg.get("memory", {}) or {}).get("db_path")
                                    or (_cfg_dir / "translation_memory.db")
                                )
                                if not _tm_db.is_absolute():
                                    _tm_db = _cfg_dir / _tm_db
                                _tm_store = TranslationMemoryStore(_tm_db)
                                self.translation_memory = _tm_store
                            _gl_cfg = _tr_cfg.get("glossary", {}) or {}
                            _gl_terms = (_gl_cfg.get("extra_terms") or {}) if _gl_cfg.get("enabled", True) else {}
                            import hashlib as _hl
                            _gl_ver = _hl.sha256(
                                repr(sorted(_gl_terms.items())).encode("utf-8")
                            ).hexdigest()[:12] if _gl_terms else ""
                            from src.ai.translation_service import TranslationService
                            web_app.state.translation_service = TranslationService(
                                ai_client=self.ai_client,
                                memory_store=_tm_store,
                                glossary_terms=_gl_terms,
                                glossary_version=_gl_ver,
                                cost_tracking=bool(_tr_cfg.get("cost_tracking", False)),
                            )
                            self.logger.info("Phase C 服务已预置（意图LLM=%s, 翻译记忆=%s）",
                                             bool(_ia_cfg.get("use_llm", False)),
                                             _tm_store is not None)

                            # ── Phase D：电商工具层（订单/物流查询 + 事实校验 + 审计） ──
                            _ec_cfg = _cfg_root.get("ecommerce_tools", {}) or {}
                            if _ec_cfg.get("enabled", False):
                                from src.ecommerce_tools import (
                                    EcommerceToolService, build_connector,
                                )
                                from src.web.routes.ecommerce_tools_routes import (
                                    register_ecommerce_tools_routes,
                                )
                                _ec_conn = build_connector(_ec_cfg)
                                self.ecommerce_tools = EcommerceToolService(
                                    _ec_conn, audit_store=audit,
                                    timeout_sec=float(_ec_cfg.get("timeout_sec", 8) or 8),
                                )
                                web_app.state.ecommerce_tools = self.ecommerce_tools
                                register_ecommerce_tools_routes(
                                    web_app, api_auth=_drafts_api_auth,
                                )
                                # P1-b：注入回复生成链路 → 命中订单号自动带真实事实/反幻觉守卫
                                if self.ai_client is not None:
                                    self.ai_client.set_ecommerce_tools(self.ecommerce_tools)
                                self.logger.info(
                                    "电商工具层已挂载（provider=%s, /api/tools/ecommerce/* + 回复事实注入）",
                                    self.ecommerce_tools.connector_name,
                                )
                    except Exception:
                        self.logger.warning("统一收件箱持久层挂载跳过", exc_info=True)

                    # ── 挂载 Contacts 路由（仅 contacts 子系统启用时） ──
                    if self.contacts is not None:
                        try:
                            from src.web.routes.contacts_routes import (
                                register_contacts_routes,
                            )

                            from starlette.requests import Request as _Req

                            def _contacts_api_auth(request: _Req):
                                # 复用 admin 的权限体系；若不存在则无鉴权（内网）
                                if hasattr(web_app.state, "require_role"):
                                    web_app.state.require_role(request, "line_rpa")

                            register_contacts_routes(
                                web_app,
                                api_auth=_contacts_api_auth,
                                contacts_store=self.contacts.store,
                                merge_service=self.contacts.merge_svc,
                                audit_store=audit,
                                intimacy_engine=self.contacts.intimacy_engine,
                                reactivation_scheduler=self.contacts.reactivation,
                                eval_scheduler=getattr(
                                    self.contacts, "draft_eval_scheduler", None,
                                ),
                                gateway=self.contacts.gateway,
                                account_limiter=self.contacts.limiter,
                                mobile_bridge=self.mobile_bridge,
                                fire_webhook=getattr(
                                    web_app.state, "fire_webhook", None,
                                ),
                                ai_client=self.ai_client,
                            )
                            # 让 web 能通过 state 直接访问
                            web_app.state.contacts = self.contacts
                            self.logger.info("Contacts Web 路由已注册（/api/contacts /ops/contacts）")
                        except Exception:
                            self.logger.warning(
                                "Contacts 路由注册跳过", exc_info=True)
                        # 把 state_store 也挂上，路由能直接读 approvals
                        try:
                            web_app.state.messenger_rpa_state_store = (
                                self.messenger_rpa_service.state_store
                            )
                        except Exception:
                            self.logger.debug(
                                "messenger_rpa state_store 注入跳过", exc_info=True
                            )
                    # ★ P1-2：Suggest More 端点需要 SkillManager
                    if self.skill_manager is not None:
                        web_app.state.skill_manager = self.skill_manager
                    web_port = int(web_cfg.get("port", 8080))
                    web_host = web_cfg.get("host", "127.0.0.1")
                    uvi_config = uvicorn.Config(web_app, host=web_host, port=web_port, log_level="warning")
                    server = uvicorn.Server(uvi_config)
                    self._web_server = server

                    # ★ 隔离 web 到独立线程 + 独立 event loop，避免和主 loop 上的
                    # Telegram/RPA/contacts 后台任务抢占。任何主 loop 上的同步阻塞
                    # （SQLite 写、BM25 全表扫描）不会再卡 web 请求。
                    def _run_web_in_thread():
                        try:
                            web_loop = asyncio.new_event_loop()
                            self._web_loop = web_loop
                            asyncio.set_event_loop(web_loop)
                            try:
                                web_loop.run_until_complete(server.serve())
                            finally:
                                try:
                                    web_loop.close()
                                except Exception:
                                    pass
                        except OSError as e:
                            if is_bind_address_in_use_error(e):
                                self.logger.warning(
                                    "Web 管理后台未启动: 端口 %s 已被占用（通常为先前未退出的本程序实例）。"
                                    "请先结束占用进程: taskkill /F /IM python.exe 或修改 config.yaml 中 web_admin.port",
                                    web_port,
                                )
                            else:
                                self.logger.warning("Web 管理后台启动失败: %s", e)
                        except Exception as ex:
                            self.logger.warning("Web 管理后台启动跳过: %s", ex)

                    web_thread = threading.Thread(
                        target=_run_web_in_thread,
                        name="web_admin_thread",
                        daemon=True,
                    )
                    web_thread.start()
                    self._web_thread = web_thread
                    self.logger.info(
                        "Web 管理后台正在绑定 http://%s:%s（独立线程隔离，避免抢占主 event loop）",
                        web_host,
                        web_port,
                    )
                except Exception as ex:
                    self.logger.warning("Web 管理后台启动跳过: %s", ex)

            # 若启用监控，在后台线程启动监控 API（供前端对接）
            mon = getattr(self.config, "config", {}) or {}
            mon = mon.get("monitoring", {})
            if mon.get("enabled", True):
                try:
                    port = int(mon.get("metrics_port", 9090))
                    from src.monitoring.server import run_server
                    _web_cfg = self.config.config.get("web_admin", {})
                    mon_token = mon.get("auth_token") or _web_cfg.get("auth_token", "")
                    t = threading.Thread(
                        target=run_server,
                        kwargs={"host": "127.0.0.1", "port": port,
                                "assistant_ref": self, "auth_token": mon_token},
                        daemon=True,
                    )
                    t.start()
                    self._monitor_thread = t
                    self.logger.info(
                        "监控 API 线程已启动，正在绑定 127.0.0.1:%s（若端口被占用将在线程内失败，见日志）",
                        port,
                    )
                except Exception as ex:
                    self.logger.warning(f"监控 API 启动跳过: {ex}")
            return True
            
        except Exception as e:
            self.logger.error(f"初始化失败: {e}")
            return False
    
    async def start(self):
        """启动AI聊天助手"""
        if not self.running:
            try:
                self.logger.info("🚀 启动AI聊天助手...")
                self.running = True

                # ★ 修复：telegram_client.start() 内部 await idle()，永不返回；
                # 若直接 await 会阻塞后续 LINE/Messenger RPA 的 start()，
                # 所以包装成后台 task，紧接着启动 RPA 服务，保持原日志语义不变。
                self._telegram_task = asyncio.create_task(
                    self.telegram_client.start(), name="telegram_client_start",
                )
                # 次要账号各自建独立 task
                for _i, _tc in enumerate(self.telegram_clients[1:], 2):
                    _t = asyncio.create_task(
                        _tc.start(),
                        name=f"telegram_client_start_{_tc.account_id}",
                    )
                    self._secondary_tg_tasks.append(_t)
                    self.logger.info(
                        "Telegram 账号 [%s] 已在后台启动", _tc.account_id
                    )
                # 给主 telegram 几秒完成登录
                try:
                    await asyncio.wait_for(
                        self._wait_until_telegram_ready(), timeout=15.0
                    )
                except asyncio.TimeoutError:
                    self.logger.warning(
                        "Telegram 客户端 15s 内未就绪，继续启动 RPA 服务（会在后台重试）"
                    )

                # 设置信号处理
                self._setup_signal_handlers()

                self.logger.info("✅ AI聊天助手已启动，等待消息...")

                for _lsvc in self.line_rpa_services:
                    try:
                        started = await _lsvc.start()
                        _aid = getattr(_lsvc, "account_id", "default")
                        if started:
                            self.logger.info("✅ LINE RPA [%s] 后台循环已启动", _aid)
                        else:
                            self.logger.info("LINE RPA [%s] 未自动启动（见配置）", _aid)
                    except Exception as ex:
                        self.logger.warning("LINE RPA 启动跳过: %s", ex)

                for _wsvc in self.whatsapp_rpa_services:
                    try:
                        started = await _wsvc.start()
                        _aid = getattr(_wsvc, "account_id", "default")
                        if started:
                            self.logger.info("✅ WhatsApp RPA [%s] 后台循环已启动", _aid)
                        else:
                            self.logger.info("WhatsApp RPA [%s] 未自动启动（见配置）", _aid)
                    except Exception as ex:
                        self.logger.warning("WhatsApp RPA 启动跳过: %s", ex)

                if self.messenger_rpa_service is not None:
                    try:
                        # ★ P2-4：注入 telegram_client 给 service → runner，
                        # 使人工转接能推送到 TG 管理员群
                        if self.telegram_client is not None and hasattr(
                            self.messenger_rpa_service, "bind_telegram_client"
                        ):
                            try:
                                self.messenger_rpa_service.bind_telegram_client(
                                    self.telegram_client
                                )
                            except Exception:
                                self.logger.debug(
                                    "bind_telegram_client 失败", exc_info=True
                                )
                        started = await self.messenger_rpa_service.start()
                        if started:
                            self.logger.info("✅ Messenger RPA 后台循环已启动")
                        else:
                            self.logger.info("Messenger RPA 后台循环未自动启动（见配置）")
                    except Exception as ex:
                        self.logger.warning("Messenger RPA 启动跳过: %s", ex)

                if self.device_coordinator_service is not None:
                    try:
                        await self.device_coordinator_service.start()
                        self.logger.info("✅ DeviceCoordinatorService 已启动")
                    except Exception as ex:
                        self.logger.warning("DeviceCoordinatorService 启动跳过: %s", ex)

                if self.hotplug_watcher is not None:
                    try:
                        await self.hotplug_watcher.start()
                        self.logger.info("✅ HotPlugWatcher 已启动")
                    except Exception as ex:
                        self.logger.warning("HotPlugWatcher 启动跳过: %s", ex)

                asyncio.create_task(self._warmup_embeddings(), name="kb_warmup_embeddings")
                asyncio.create_task(
                    self._episodic_backfill_on_startup(), name="episodic_backfill_startup"
                )
                asyncio.create_task(
                    self._episodic_backfill_periodic(), name="episodic_backfill_periodic"
                )
                asyncio.create_task(self._periodic_self_heal(), name="kb_periodic_self_heal")
                asyncio.create_task(self._periodic_daily_learn(), name="daily_learner")

                # ★ W3-3G / W3-3K：启动 reunion 草稿成功率评估循环（DraftEvalScheduler）
                if self.contacts is not None and self.contacts.store is not None:
                    from src.contacts.draft_eval import DraftEvalScheduler
                    self.contacts.draft_eval_scheduler = DraftEvalScheduler(
                        self.contacts.store, eval_window_secs=86400,
                    )
                    asyncio.create_task(
                        self._periodic_draft_eval(), name="draft_success_evaluator",
                    )

                # ★ W2-D4.2/4.3：启动 reactivation 主动唤醒循环
                # 必须在 contacts + messenger_rpa_service + ai_client 都就绪后启动
                await self._maybe_start_reactivation_loop()

                # Mobile Bridge 轮询循环
                if self.mobile_bridge is not None:
                    try:
                        await self.mobile_bridge.start()
                        self.logger.info("✅ Mobile Bridge 已启动")
                    except Exception as ex:
                        self.logger.warning("Mobile Bridge 启动跳过: %s", ex)

                # 保持运行直到收到停止信号
                while self.running:
                    await asyncio.sleep(1)
                    
            except KeyboardInterrupt:
                self.logger.info("收到中断信号，正在关闭...")
            except Exception as e:
                self.logger.error(f"运行错误: {e}")
            finally:
                await self.stop()
    
    async def _maybe_start_reactivation_loop(self) -> None:
        """W2-D4.2/4.3：启动 reactivation 主动唤醒循环（陪护核心）。

        条件：contacts 子系统已启用 + messenger_rpa_service 已起 + 配置 reactivation.enabled
        """
        try:
            cfg_react = (self.config.config.get("reactivation") or {})
            if not cfg_react.get("enabled", False):
                self.logger.info("reactivation_loop 未启用（reactivation.enabled=false）")
                return
            if self.contacts is None or self.messenger_rpa_service is None:
                self.logger.info(
                    "reactivation_loop 跳过（contacts=%s messenger_rpa=%s）",
                    self.contacts is not None, self.messenger_rpa_service is not None,
                )
                return
            from src.contacts.reactivation_loop import ReactivationLoop

            # send_callback：把 reply 入 messenger 的 deferred 队列
            async def _send_to_messenger(channel, account_id, chat_name, reply,
                                         defer_until, reason, staleness_sec, extra):
                if channel != "messenger":
                    return 0
                return await self.messenger_rpa_service.enqueue_reactivation_deferred(
                    account_id=account_id,
                    chat_name=chat_name,
                    reply_text=reply,
                    defer_until=defer_until,
                    defer_reason=reason,
                    staleness_sec=staleness_sec,
                    extra=extra,
                )

            # episodic_provider：拿 journey 对象 → 渲染画像 block 给 reactivation prompt
            def _episodic_provider(journey) -> str:
                try:
                    if journey is None:
                        return ""
                    snap = (getattr(journey, "context_snapshot_json", "") or "").strip()
                    if snap:
                        from src.contacts.portrait_extractor import render_block
                        return render_block(snap) or ""
                    return ""
                except Exception:
                    return ""

            ai_name = ""
            try:
                ai_name = str((self.config.get_ai_config() or {}).get("ai_name") or "她")
            except Exception:
                ai_name = "她"

            loop = ReactivationLoop(
                scheduler=self.contacts.reactivation,
                store=self.contacts.store,
                ai_client=self.ai_client,
                send_callback=_send_to_messenger,
                episodic_provider=_episodic_provider,
                ai_name=ai_name,
                max_per_tick=int(cfg_react.get("max_per_tick", 3)),
                interval_sec=float(cfg_react.get("interval_sec", 600)),
                skip_if_no_episodic=bool(cfg_react.get("skip_if_no_episodic", True)),
                dry_run=bool(cfg_react.get("dry_run", False)),
                first_run_grace_minutes=float(
                    cfg_react.get("first_run_grace_minutes", 60),
                ),
                first_run_max_per_tick=int(
                    cfg_react.get("first_run_max_per_tick", 1),
                ),
            )
            await loop.start()
            self._reactivation_loop = loop
            self.logger.info(
                "✅ reactivation_loop 已启动（interval=%ss max_per_tick=%s）",
                cfg_react.get("interval_sec", 600), cfg_react.get("max_per_tick", 3),
            )
        except Exception as ex:
            self.logger.warning("reactivation_loop 启动跳过: %s", ex)
            self.logger.debug("reactivation_loop 启动异常", exc_info=True)

    async def _wait_until_telegram_ready(self) -> None:
        """轮询 telegram_client.running/client.is_connected 直到 True，用于启动
        顺序解耦：我们不能 await telegram_client.start()（它内部 idle 永不返回），
        但需要在继续启动 RPA 之前给 Telegram 一个合理的就绪窗口。"""
        while True:
            try:
                tc = self.telegram_client
                running = bool(getattr(tc, "running", False))
                client = getattr(tc, "client", None)
                connected = bool(client and getattr(client, "is_connected", False))
                if running and connected:
                    return
            except Exception:
                pass
            await asyncio.sleep(0.3)

    async def _warmup_embeddings(self):
        """后台批量向量化无 embedding 的知识库条目"""
        try:
            await asyncio.sleep(5)
            if not self.ai_client or not self.ai_client.client:
                return
            cfg_dir = (Path(self.config.config_path).parent if hasattr(self.config, "config_path") else Path("config")).resolve()
            kb_path = (cfg_dir / "knowledge_base.db").resolve()
            if not kb_path.exists():
                self.logger.info("向量预热: 知识库文件不存在，跳过 (%s)", kb_path)
                return
            from src.utils.kb_store import KnowledgeBaseStore
            kb = KnowledgeBaseStore(kb_path)
            pending = kb.get_entries_without_embedding()
            if not pending:
                self.logger.info("向量预热: 所有条目已向量化 (%d 条)", kb._vindex.count())
                return
            self.logger.info("向量预热: 发现 %d 条待向量化条目，开始批量处理...", len(pending))
            batch_size = 20
            done = 0
            for i in range(0, len(pending), batch_size):
                if not self.running:
                    break
                batch = pending[i:i + batch_size]
                texts = []
                for e in batch:
                    parts = [e.get("title", "")]
                    trigs = e.get("triggers", "")
                    if trigs:
                        try:
                            import json as _j
                            tl = _j.loads(trigs) if isinstance(trigs, str) else trigs
                            if isinstance(tl, list):
                                parts.append(" ".join(tl))
                        except Exception:
                            pass
                    for f in ("scenario", "steps", "principles"):
                        if e.get(f):
                            parts.append(e[f][:200])
                    texts.append(" ".join(parts)[:500])
                try:
                    vecs = await self.ai_client.embed_with_fallback(texts)
                    if vecs and len(vecs) == len(batch):
                        n_ok = 0
                        for entry, vec in zip(batch, vecs):
                            if not vec:
                                continue
                            kb.set_single_embedding(entry["id"], vec)
                            n_ok += 1
                        done += n_ok
                        self.logger.debug("向量预热: 已处理 %d/%d (本批成功 %d)", done, len(pending), n_ok)
                    else:
                        self.logger.warning(
                            "向量预热: 批次返回数量仍不匹配 (%s vs %s)",
                            len(vecs) if vecs else 0, len(batch),
                        )
                except Exception as e:
                    self.logger.warning("向量预热: 批次失败: %s", e)
                await asyncio.sleep(1.5)
            cov = kb.embedding_coverage()
            self.logger.info("向量预热完成: %d 条新增向量化, 总覆盖率 %s%%", done, cov.get("pct", 0))
        except Exception:
            self.logger.exception("向量预热异常")

    async def _episodic_backfill_on_startup(self):
        """可选：启动后补全一批缺失的情景记忆向量（配置 memory.vector.backfill_on_startup）。"""
        try:
            mcfg = (self.config.config or {}).get("memory") or {}
            vcfg = (mcfg.get("vector") or {})
            bcfg = vcfg.get("backfill_on_startup") or {}
            if not bcfg.get("enabled", False):
                return
            if (vcfg.get("backfill_periodic") or {}).get("enabled", False):
                self.logger.info(
                    "情景记忆启动补全已跳过（已启用周期补全 memory.vector.backfill_periodic，避免重复嵌入）"
                )
                return
            delay = float(bcfg.get("delay_seconds", 12))
            limit = max(1, min(int(bcfg.get("limit", 15)), 50))
            await asyncio.sleep(max(0.0, delay))
            if not self.running:
                return
            sm = self.skill_manager
            if not sm:
                return
            out = await sm.episodic_backfill_embeddings(limit)
            self.logger.info("情景记忆启动补全: %s", out)
        except Exception:
            self.logger.exception("情景记忆启动补全失败")

    async def _episodic_backfill_periodic(self):
        """可选：按间隔补全情景记忆缺失向量（memory.vector.backfill_periodic）。"""
        try:
            mcfg = (self.config.config or {}).get("memory") or {}
            vcfg = (mcfg.get("vector") or {})
            pcfg = vcfg.get("backfill_periodic") or {}
            if not pcfg.get("enabled", False):
                return
            init_delay = float(pcfg.get("initial_delay_seconds", 1800))
            await asyncio.sleep(max(0.0, init_delay))
        except Exception:
            self.logger.exception("情景记忆周期补全初始化失败")
            return

        while self.running:
            try:
                mcfg = (self.config.config or {}).get("memory") or {}
                vcfg = (mcfg.get("vector") or {})
                pcfg = vcfg.get("backfill_periodic") or {}
                if not pcfg.get("enabled", False):
                    await asyncio.sleep(3600)
                    continue
                if not vcfg.get("enabled", False):
                    await asyncio.sleep(min(3600.0, float(pcfg.get("interval_hours", 6)) * 3600.0))
                    continue
                limit = max(1, min(int(pcfg.get("limit", 20)), 100))
                sm = self.skill_manager
                if sm:
                    out = await sm.episodic_backfill_embeddings(limit)
                    if int(out.get("updated") or 0) > 0:
                        self.logger.info("情景记忆周期补全: %s", out)
                    else:
                        self.logger.debug("情景记忆周期补全: %s", out)
            except Exception:
                self.logger.exception("情景记忆周期补全失败")
            try:
                hrs = float(
                    ((self.config.config or {}).get("memory") or {})
                    .get("vector", {})
                    .get("backfill_periodic", {})
                    .get("interval_hours", 6)
                )
            except (TypeError, ValueError):
                hrs = 6.0
            await asyncio.sleep(max(60.0, hrs * 3600.0))

    async def _periodic_self_heal(self):
        """每24小时执行一次知识库自愈巡检"""
        await asyncio.sleep(300)
        while self.running:
            try:
                cfg_dir = (Path(self.config.config_path).parent if hasattr(self.config, "config_path") else Path("config")).resolve()
                kb_path = (cfg_dir / "knowledge_base.db").resolve()
                if kb_path.exists():
                    from src.utils.kb_store import KnowledgeBaseStore
                    kb = KnowledgeBaseStore(kb_path)
                    result = kb.run_self_heal(stale_days=14)
                    self.logger.info(
                        "知识库自愈完成: 触发词扩展=%d, 归档=%d, 过载标记=%d",
                        result.get("triggers_expanded", 0),
                        result.get("entries_archived", 0),
                        result.get("overloaded_flagged", 0),
                    )
                    for detail in result.get("details", [])[:5]:
                        self.logger.debug("  自愈: %s", detail)
            except Exception as e:
                self.logger.warning("知识库自愈异常: %s", e)
            await asyncio.sleep(86400)

    async def _periodic_draft_eval(self):
        """W3-3G：每小时跑一次 reunion 草稿成功率评估。

        对所有「已发 24h+ 但还没评估」的草稿，看 sent_ts 后 24h 内有没有
        对方 msg_in，写回 ``draft_log.success``。让 digest 的成功率
        指标持续刷新，无需运营手动触发 ``/api/drafts/eval-run``。

        启动延迟 5min（避免与启动期其他 init 抢 SQLite 锁）；
        每轮 sleep 3600 秒（1h，比窗口 24h 更密以减小 stats 滞后）。
        """
        await asyncio.sleep(300)
        sched = getattr(getattr(self, "contacts", None), "draft_eval_scheduler", None)
        while self.running:
            if sched is not None:
                sched.run_once()
            interval = sched.next_interval_secs if sched else 3600
            await asyncio.sleep(interval)

    async def _periodic_daily_learn(self):
        """每24小时执行一次自动学习：汇总未命中 → AI生成草稿 → 等待人工审核"""
        await asyncio.sleep(600)
        while self.running:
            try:
                cfg_dir = (Path(self.config.config_path).parent
                           if hasattr(self.config, "config_path")
                           else Path("config")).resolve()
                kb_path = (cfg_dir / "knowledge_base.db").resolve()
                if kb_path.exists() and self.ai_client:
                    from src.utils.kb_store import KnowledgeBaseStore
                    from src.utils.daily_learner import DailyLearner
                    kb = KnowledgeBaseStore(kb_path)
                    learner = DailyLearner(kb, self.ai_client, db_path=kb_path)
                    domain_name = ""
                    if hasattr(self.config, "config") and isinstance(self.config.config, dict):
                        domain_name = effective_domain_name(self.config.config)
                    domain_ctx = f"当前行业: {domain_name}" if domain_name else ""
                    result = await learner.run_daily_learn(domain_context=domain_ctx)
                    self.logger.info(
                        "每日自动学习完成: 收集=%d, 生成=%d, 保存=%d",
                        result["collected"], result["generated"], result["saved"]
                    )
            except Exception as e:
                self.logger.warning("每日自动学习异常: %s", e)
            await asyncio.sleep(86400)

    async def stop(self):
        """停止AI聊天助手"""
        if self.running:
            self.logger.info("正在停止AI聊天助手...")
            self.running = False

            # 排空消息队列（graceful drain）
            if self.telegram_client and hasattr(self.telegram_client, 'message_queue'):
                q = self.telegram_client.message_queue
                if not q.empty():
                    self.logger.info("等待消息队列排空 (%d 条)...", q.qsize())
                    try:
                        await asyncio.wait_for(q.join(), timeout=10)
                        self.logger.info("消息队列已排空")
                    except asyncio.TimeoutError:
                        self.logger.warning("消息队列排空超时，放弃剩余 %d 条", q.qsize())

            # 持久化上下文快照
            if self.telegram_client and hasattr(self.telegram_client, 'context_manager'):
                cm = self.telegram_client.context_manager
                if cm and hasattr(cm, 'persist_snapshot'):
                    try:
                        cm.persist_snapshot()
                        self.logger.info("上下文快照已保存")
                    except Exception as e:
                        self.logger.warning("上下文快照保存失败: %s", e)
            
            for _lsvc in self.line_rpa_services:
                try:
                    await _lsvc.stop()
                    self.logger.info("LINE RPA [%s] 后台循环已停止", getattr(_lsvc, "account_id", "?"))
                except Exception as ex:
                    self.logger.warning("LINE RPA 停止异常: %s", ex)

            if self.messenger_rpa_service is not None:
                try:
                    await self.messenger_rpa_service.stop()
                    self.logger.info("Messenger RPA 后台循环已停止")
                except Exception as ex:
                    self.logger.warning("Messenger RPA 停止异常: %s", ex)

            for _wsvc in self.whatsapp_rpa_services:
                try:
                    await _wsvc.stop()
                    self.logger.info("WhatsApp RPA [%s] 后台循环已停止", getattr(_wsvc, "account_id", "?"))
                except Exception as ex:
                    self.logger.warning("WhatsApp RPA 停止异常: %s", ex)

            # W2-D4.2：reactivation_loop 优雅停止
            if self._reactivation_loop is not None:
                try:
                    await self._reactivation_loop.stop()
                    self.logger.info("reactivation_loop 已停止")
                except Exception as ex:
                    self.logger.warning("reactivation_loop 停止异常: %s", ex)

            if self.mobile_bridge is not None:
                try:
                    await self.mobile_bridge.stop()
                except Exception as ex:
                    self.logger.warning("Mobile Bridge 停止异常: %s", ex)

            if self.hotplug_watcher is not None:
                try:
                    await self.hotplug_watcher.stop()
                    self.logger.info("HotPlugWatcher 已停止")
                except Exception as ex:
                    self.logger.warning("HotPlugWatcher 停止异常: %s", ex)

            if self.telegram_client:
                await self.telegram_client.stop()
            
            if self.skill_manager:
                await self.skill_manager.cleanup()
            
            if self.ai_client:
                await self.ai_client.cleanup()

            # D: 关掉 web 独立线程（uvicorn server 设 should_exit + 等线程退出）
            try:
                if self._web_server is not None:
                    self._web_server.should_exit = True
                if self._web_thread is not None and self._web_thread.is_alive():
                    self._web_thread.join(timeout=5.0)
                    if self._web_thread.is_alive():
                        self.logger.warning("Web 管理后台线程 5s 内未退出，跳过等待")
                    else:
                        self.logger.info("Web 管理后台线程已停止")
            except Exception as ex:
                self.logger.warning("Web 管理后台停止异常: %s", ex)

            self.logger.info("✅ AI聊天助手已停止")
    
    def _setup_signal_handlers(self):
        """设置信号处理"""
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
    
    def _signal_handler(self, signum, frame):
        """信号处理函数"""
        self.logger.info(f"收到信号 {signum}, 正在关闭...")
        asyncio.create_task(self.stop())


async def main():
    """主函数"""
    assistant = AIChatAssistant()
    
    # 初始化
    if not await assistant.initialize():
        print("初始化失败，请检查配置和日志")
        return 1
    
    try:
        # 启动
        await assistant.start()
    except Exception as e:
        logging.error(f"程序运行错误: {e}")
        return 1
    
    return 0


if __name__ == "__main__":
    # 设置默认事件循环策略（Windows需要）
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    # 运行主程序
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
