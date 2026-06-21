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
        # Phase O: 主动关怀派发器引用（关程序时 stop）
        self._care_dispatcher = None
        # P2: 陪伴主动话题调度循环引用（关程序时 stop）
        self._companion_proactive_loop = None
        # 多平台 deferred 队列（非 messenger 主动消息走此队列；关程序时 stop）
        self._deferred_outbox_dispatcher = None
        # 质量趋势持久化快照器（周期落地 companion_quality_overview；关程序时 stop）
        self._quality_trend_snapshotter = None
        # 坐席工作台实时化（D5a）：收件箱后台 ingest 轮询任务 + web_app 引用
        self._web_app = None  # type: Optional[Any]  # noqa: F821
        self._inbox_ingest_task = None
        
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

            # N 线 核心4：注入统一运行时上下文，供编排器把协议号（扫码登录）拉起为 A 线丰富 client
            try:
                from src.integrations.telegram_companion_worker import set_companion_context
                set_companion_context(
                    config_manager=self.config,
                    skill_manager=self.skill_manager,
                    ai_client=self.ai_client,
                )
            except Exception as _ctx_ex:
                self.logger.debug("companion runtime 上下文注入失败: %s", _ctx_ex)
            
            # 5. 初始化Telegram客户端（支持多账号并行）
            try:
                from src.client.telegram_account_registry import TelegramAccountRegistry
                tg_raw_cfg = (self.config.config or {}).get("telegram", {})
                _tg_registry = TelegramAccountRegistry.from_config(tg_raw_cfg)
            except Exception as _reg_ex:
                self.logger.warning("TelegramAccountRegistry 构建失败，回退单账号: %s", _reg_ex)
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
                    self.logger.info(
                        "[N5] config 账号已并入统一注册表：%s",
                        ", ".join(_synced) or "（无）",
                    )
                except Exception as _sync_ex:
                    self.logger.warning("[N5] 登录注册统一同步失败（忽略）: %s", _sync_ex)

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

            # C0-1 授权状态（只读提示，不阻断启动）
            try:
                from src.licensing import configure_license_manager

                # C0-3：按 config 配置强制开关（licensing.enforce，默认关）
                _lic_cfg = (self.config.config or {}).get("licensing", {}) or {}
                _lic = configure_license_manager(
                    enforce=bool(_lic_cfg.get("enforce", False)))
                if _lic.state == "active":
                    _exp = ("永久" if not _lic.expires_at
                            else f"剩 {_lic.days_left} 天")
                    self.logger.info(
                        "🔑 授权：%s · %s · 客户=%s · %s",
                        _lic.plan, _lic.state, _lic.customer or "—", _exp,
                    )
                elif _lic.state == "unlicensed":
                    self.logger.info("🔑 授权：社区模式（未检测到授权文件）")
                else:
                    self.logger.warning(
                        "🔑 授权状态=%s：%s",
                        _lic.state, "；".join(_lic.messages) or "—",
                    )
            except Exception:
                self.logger.debug("授权状态读取跳过", exc_info=True)

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
                    # Q3：把同一套 IntimacyEngine 事实源注册为进程级 provider，
                    # 让 A 线 Telegram（含 companion 运行时）也吃上 intimacy/funnel
                    # → companion_relationship 双信号融合。telegram hook 也受同一开关控制。
                    try:
                        from src.utils.companion_context import (
                            set_relationship_providers,
                        )
                        if self.contacts.is_rpa_hook_enabled("telegram"):
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
                                self.config.config
                                if hasattr(self.config, "config") else {}
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
                                self.logger.info(
                                    "Telegram A 线已接入关系事实源 "
                                    "(intimacy/funnel + 收发记录 + 剧情镜像写入 contacts)")
                            else:
                                self.logger.info(
                                    "Telegram A 线已接入关系事实源 "
                                    "(只读 intimacy/funnel；contacts_recording 未开)")
                        else:
                            self.logger.info(
                                "Telegram 关系事实源已按配置禁用 "
                                "(contacts.rpa_hooks.telegram=false)")
                    except Exception:
                        self.logger.warning(
                            "set_relationship_providers 失败", exc_info=True)
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
                    self._web_app = web_app  # 供收件箱后台 ingest 轮询访问 state 上的各平台 service
                    # 翻译/意图等服务的兜底路径（inbox 未启用时）需要 ai_client，
                    # 否则 _get_translation_service 会建出无引擎的退化实例。
                    if getattr(self, "ai_client", None) is not None:
                        web_app.state.ai_client = self.ai_client
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

                    # ── G1 全局 Kill-Switch：初始化单例（回填持久化的冻结态，重启不丢）──
                    try:
                        from src.ops.kill_switch import get_kill_switch
                        _cfg_dir0 = Path(self.config.config_path).parent
                        _ks_cfg = ((self.config.config or {}).get("ops") or {}).get("kill_switch") or {}
                        _ks_db = Path(_ks_cfg.get("db_path") or (_cfg_dir0 / "runtime_flags.db"))
                        if not _ks_db.is_absolute():
                            _ks_db = _cfg_dir0 / _ks_db
                        _ks = get_kill_switch(_ks_db)
                        web_app.state.kill_switch = _ks
                        _active = _ks.status()
                        if _active:
                            self.logger.warning(
                                "🛑 Kill-Switch 启动即生效（重启回填）：%s",
                                [i["scope"] for i in _active])
                        else:
                            self.logger.info("Kill-Switch 已就绪（%s）", _ks_db)
                    except Exception:
                        self.logger.warning("Kill-Switch 初始化跳过", exc_info=True)

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

                            from starlette.requests import Request as _DraftReq

                            def _drafts_api_auth(request: _DraftReq):
                                # 统一走 admin 的 _api_auth：登录校验 + 坐席(agent)白名单放行
                                # （/api/drafts 已在 _agent_api_allowed 内）；主管端点由路由内
                                # _is_supervisor 守卫。回退 require_role 仅为极端兜底。
                                # 注：参数必须带 Request 注解，否则 FastAPI 会把 request 当作
                                # 必填 query 参数导致全部 422。
                                _fn = getattr(web_app.state, "api_auth", None)
                                if _fn is not None:
                                    _fn(request)
                                elif hasattr(web_app.state, "require_role"):
                                    web_app.state.require_role(request, "line_rpa")

                            register_drafts_routes(web_app, api_auth=_drafts_api_auth)
                            self.logger.info("统一草稿层已挂载（/api/drafts）")

                            # ── Phase A：L2 草稿自动发送后台 worker ──
                            try:
                                from src.inbox.autosend_worker import AutosendWorker
                                _as_cfg = (self.config.config or {}).get(
                                    "inbox", {}
                                ).get("l2_autosend", {}) or {}
                                if _as_cfg.get("enabled", True):
                                    # H3：合并 auto_draft 清理配置到 worker cfg
                                    _ad_cleanup = (self.config.config or {}).get(
                                        "inbox", {}
                                    ).get("auto_draft", {}) or {}
                                    _merged_as_cfg = {
                                        "cleanup_age_days": int(_ad_cleanup.get("cleanup_age_days", 7)),
                                        "cleanup_enabled": bool(_ad_cleanup.get("cleanup_enabled", True)),
                                        **_as_cfg,
                                    }
                                    _as_worker = AutosendWorker(
                                        draft_service=draft_svc,
                                        config=_merged_as_cfg,
                                    )
                                    web_app.state.autosend_worker = _as_worker
                                    # C3：注册 L2 事件驱动钩子，新草稿落库时立即唤醒
                                    self.inbox_store.register_l2_callback(
                                        _as_worker.notify_new_l2
                                    )
                                    asyncio.ensure_future(_as_worker.run())
                                    self.logger.info(
                                        "AutosendWorker 已启动（min=%ss max=%ss）",
                                        _as_cfg.get("min_interval_sec", 60),
                                        _as_cfg.get("max_interval_sec", 600),
                                    )
                            except Exception:
                                self.logger.debug("AutosendWorker 启动跳过", exc_info=True)

                            # ── K1+K2：SLAWatcher 草稿 SLA 预警 + 自动再分配 ──
                            try:
                                from src.inbox.sla_watcher import SLAWatcher
                                _sw_cfg = (self.config.config or {}).get(
                                    "inbox", {}
                                ).get("sla_watcher", {}) or {}
                                if _sw_cfg.get("enabled", True):
                                    _sw = SLAWatcher(
                                        draft_service=draft_svc,
                                        inbox_store=self.inbox_store,
                                        config=_sw_cfg,
                                    )
                                    web_app.state.sla_watcher = _sw
                                    asyncio.ensure_future(_sw.run())
                                    self.logger.info(
                                        "SLAWatcher 已启动（sla=%.0fh tick=%.0fs absent=%.0fs）",
                                        float(_sw_cfg.get("sla_hours", 4)),
                                        float(_sw_cfg.get("tick_sec", 60)),
                                        float(_sw_cfg.get("absent_sec", 300)),
                                    )
                            except Exception:
                                self.logger.debug("SLAWatcher 启动跳过", exc_info=True)

                            # ── P3：AutoClaimWorker auto_assign 自动认领执行端 ──
                            # 默认关（workspace.auto_assign.auto_claim.enabled=false）；
                            # worker 每 tick 重读配置，开关无需重启。仅在 inbox 可用时启。
                            try:
                                from src.workspace.auto_claim_worker import AutoClaimWorker
                                _ac_cfg = (((self.config.config or {}).get(
                                    "workspace", {}) or {}).get(
                                    "auto_assign", {}) or {}).get("auto_claim", {}) or {}
                                _acw = AutoClaimWorker(
                                    inbox_store=self.inbox_store,
                                    config_manager=self.config,
                                    config=_ac_cfg,
                                )
                                web_app.state.auto_claim_worker = _acw
                                asyncio.ensure_future(_acw.run())
                                self.logger.info(
                                    "AutoClaimWorker 已启动（默认关，按 auto_claim.enabled 热生效）")
                            except Exception:
                                self.logger.debug("AutoClaimWorker 启动跳过", exc_info=True)

                            # ── L2：WebhookNotifier 企业 IM 通知 ──────────────
                            try:
                                from src.inbox.webhook_notifier import WebhookNotifier
                                # 有效列表：notify_webhooks.json 覆盖层优先，否则 config.yaml
                                try:
                                    from src.integrations.notify_webhooks_store import (
                                        effective_webhooks,
                                    )
                                    _wh_list = effective_webhooks(self.config.config or {})
                                except Exception:
                                    _wh_list = (self.config.config or {}).get(
                                        "notify", {}
                                    ).get("webhooks", []) or []
                                # 即使当前为空也创建 notifier：便于后台「告警渠道」面板
                                # 运行时 reload() 增删，免重启
                                _whn = WebhookNotifier(config=_wh_list)
                                web_app.state.webhook_notifier = _whn
                                asyncio.ensure_future(_whn.run())
                                self.logger.info(
                                    "WebhookNotifier 已启动（%d 个 webhook）",
                                    len(_wh_list),
                                )
                            except Exception:
                                self.logger.debug("WebhookNotifier 启动跳过", exc_info=True)

                            # ── D3：HealthWatchdog 运行时健康主动告警 ─────────
                            # 默认开；周期巡检 D1 健康，异常经 EventBus→WebhookNotifier
                            # 推送（需在「告警渠道」订阅 health_alert 事件才会真正发出）。
                            try:
                                from src.inbox.health_watchdog import HealthWatchdog
                                _hw_cfg = (self.config.config or {}).get(
                                    "health_watchdog", {}
                                ) or {}
                                if _hw_cfg.get("enabled", True):
                                    _hw = HealthWatchdog(
                                        app=web_app,
                                        config_manager=self.config,
                                        interval_sec=float(_hw_cfg.get("interval_sec", 300)),
                                        pending_threshold=int(_hw_cfg.get("queue_threshold", 200)),
                                        alert_on_warn=bool(_hw_cfg.get("alert_on_warn", False)),
                                        billing_interval_sec=float(_hw_cfg.get("billing_interval_sec", 3600)),
                                        incident_retention_days=float(_hw_cfg.get("incident_retention_days", 30)),
                                        weekly_report_enabled=bool(_hw_cfg.get("weekly_report_enabled", False)),
                                        weekly_interval_sec=float(_hw_cfg.get("weekly_interval_sec", 604800)),
                                    )
                                    web_app.state.health_watchdog = _hw
                                    asyncio.ensure_future(_hw.run())
                                    self.logger.info(
                                        "HealthWatchdog 已启动（interval=%ss alert_on_warn=%s）",
                                        _hw_cfg.get("interval_sec", 300),
                                        _hw_cfg.get("alert_on_warn", False),
                                    )
                            except Exception:
                                self.logger.debug("HealthWatchdog 启动跳过", exc_info=True)

                            # ── N2：ScheduledReporter 定时简报推送 ─────────────
                            try:
                                from src.inbox.scheduled_reporter import ScheduledReporter
                                _rpt_cfg = (self.config.config or {}).get(
                                    "report", {}
                                ) or {}
                                if _rpt_cfg.get("enabled", False):
                                    _rpt = ScheduledReporter(
                                        inbox_store=web_app.state.inbox_store,
                                        draft_service=getattr(web_app.state, "draft_service", None),
                                        app_state=web_app.state,
                                        config=_rpt_cfg,
                                    )
                                    web_app.state.scheduled_reporter = _rpt
                                    asyncio.ensure_future(_rpt.run())
                                    self.logger.info(
                                        "ScheduledReporter 已启动（daily=%s weekly=%s）",
                                        _rpt_cfg.get("daily_time", "09:00"),
                                        _rpt_cfg.get("weekly_day") or "禁用",
                                    )
                            except Exception:
                                self.logger.debug("ScheduledReporter 启动跳过", exc_info=True)

                            # E2/F2：按 auto_draft 配置注册入站新消息 → 自动草稿生成回调
                            _ad_cfg = (self.config.config or {}).get(
                                "inbox", {}
                            ).get("auto_draft", {}) or {}
                            if _ad_cfg.get("enabled", True):
                                _ad_mode = str(_ad_cfg.get("automation_mode", "auto_ai"))
                                _ad_min_len = int(_ad_cfg.get("min_text_len", 3))
                                _ad_skip = set(_ad_cfg.get("skip_platforms", []) or [])

                                def _auto_draft_cb(conv: dict, text: str) -> None:
                                    if conv.get("platform", "") in _ad_skip:
                                        return
                                    if len(str(text or "").strip()) < _ad_min_len:
                                        return
                                    draft_svc.auto_generate_draft(
                                        conv, text, automation_mode=_ad_mode
                                    )

                                self.inbox_store.register_new_inbound_cb(_auto_draft_cb)
                                self.logger.info(
                                    "AutoDraft 已启用（mode=%s min_len=%s skip=%s）",
                                    _ad_mode, _ad_min_len, _ad_skip,
                                )
                            else:
                                self.logger.info("AutoDraft 已禁用（auto_draft.enabled=false）")

                            # I3：预置回复模板库（幂等，id 冲突则跳过）
                            try:
                                from src.inbox.template_seeds import SEED_TEMPLATES
                                _seeded = self.inbox_store.seed_templates(SEED_TEMPLATES)
                                if _seeded > 0:
                                    self.logger.info("模板库已预置 %d 条种子模板", _seeded)
                            except Exception:
                                self.logger.debug("模板库预置跳过", exc_info=True)

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
                            # P56：术语库（全局+域包合并）+ 多引擎路由
                            from src.ai.translation_glossary import build_glossary
                            from src.ai.translation_engines import build_engines
                            _domain_files = []
                            try:
                                _dom_dir = Path(self.config.config_path).parent.parent / "domains"
                                if _dom_dir.exists():
                                    _domain_files = list(_dom_dir.glob("*/prompts/terminology.yaml"))
                            except Exception:
                                _domain_files = []
                            # P59：术语库可编辑覆盖层（后台控制台增删改，最高优先）
                            from src.ai.glossary_store import GlossaryStore
                            _gloss_ov_path = _cfg_dir / "glossary_overrides.yaml"
                            _gloss_store = GlossaryStore(_gloss_ov_path)
                            _gloss_overrides = _gloss_store.load()
                            _glossary = build_glossary(
                                _cfg_root, domain_files=_domain_files, overrides=_gloss_overrides,
                            )
                            _engines = build_engines(_tr_cfg, self.ai_client)
                            # 存重建上下文，供 /api/workspace/glossary 热更新复用
                            web_app.state.glossary_store = _gloss_store
                            web_app.state.glossary_config = _cfg_root
                            web_app.state.glossary_domain_files = _domain_files
                            from src.ai.translation_service import TranslationService
                            web_app.state.translation_service = TranslationService(
                                ai_client=self.ai_client,
                                memory_store=_tm_store,
                                glossary_terms=_glossary.terms,
                                glossary_version=_glossary.version,
                                glossary_protect=_glossary.protect,
                                cost_tracking=bool(_tr_cfg.get("cost_tracking", False)),
                                engines=_engines,
                            )
                            self.logger.info(
                                "Phase C/P56 服务已预置（意图LLM=%s, 翻译记忆=%s, 引擎=%s, 术语=%d, 保护词=%d）",
                                bool(_ia_cfg.get("use_llm", False)),
                                _tm_store is not None,
                                "→".join(e.name for e in _engines),
                                len(_glossary.terms), len(_glossary.protect),
                            )

                            # ── Phase B：可选统计语种检测（缺库自动跳过，仅精修含糊拉丁） ──
                            try:
                                _ld_cfg = ((_tr_cfg.get("lang_detect") or {}).get("statistical") or {})
                                if _ld_cfg.get("enabled", False):
                                    from src.ai.lang_detect_statistical import build_statistical_detector
                                    from src.ai.translation_service import set_statistical_detector
                                    _stat_fn = build_statistical_detector()
                                    if _stat_fn is not None:
                                        set_statistical_detector(
                                            _stat_fn,
                                            min_chars=int(_ld_cfg.get("min_chars", 12) or 12),
                                        )
                                        self.logger.info("统计语种检测已启用（回退精修含糊拉丁）")
                                    else:
                                        self.logger.warning(
                                            "translation.lang_detect.statistical.enabled=true 但未装 lingua/langdetect，已跳过"
                                        )
                            except Exception:
                                self.logger.debug("统计语种检测装配跳过", exc_info=True)

                            # ── Phase D：电商工具层（订单/物流查询 + 事实校验 + 审计） ──
                            _ec_cfg = _cfg_root.get("ecommerce_tools", {}) or {}
                            if _ec_cfg.get("enabled", False):
                                from src.ecommerce_tools import (
                                    EcommerceToolService, build_connector,
                                    build_logistics_connector,
                                )
                                from src.web.routes.ecommerce_tools_routes import (
                                    register_ecommerce_tools_routes,
                                )
                                _ec_conn = build_connector(_ec_cfg)
                                _logi_conn = build_logistics_connector(_ec_cfg.get("logistics") or {})
                                self.ecommerce_tools = EcommerceToolService(
                                    _ec_conn, audit_store=audit,
                                    timeout_sec=float(_ec_cfg.get("timeout_sec", 8) or 8),
                                    cache_ttl_sec=float(_ec_cfg.get("cache_ttl_sec", 0) or 0),
                                    cache_max_entries=int(_ec_cfg.get("cache_max_entries", 512) or 512),
                                    logistics_connector=_logi_conn,
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
                                # 复用 admin 的 _api_auth（登录校验）；回退 require_role 仅兜底。
                                _fn = getattr(web_app.state, "api_auth", None)
                                if _fn is not None:
                                    _fn(request)
                                elif hasattr(web_app.state, "require_role"):
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

                # ★ Phase K2：C 端变现（端用户订阅/解锁/打赏；默认关）
                # 先于 proactive_care，使变现门控开启时 EntitlementStore 已就绪
                self._maybe_init_monetization(self._web_app)

                # ★ Phase O：主动关怀引擎（记忆驱动的约定/事件跟进）
                await self._maybe_start_proactive_care(self._web_app)

                # ★ 多平台 deferred 队列（非 messenger 主动消息的发送闭环；默认关）
                await self._maybe_start_deferred_outbox()

                # ★ 质量趋势持久化（周期落地 companion_quality_overview；默认关）
                await self._maybe_start_quality_trend()

                # ★ Q 延伸：ingest 回写 contact_id（默认关）
                self._maybe_wire_ingest_contact_writeback()

                # ★ Q 延伸·存量回填：给历史会话补 contact_id（默认关，一次性）
                asyncio.create_task(
                    self._maybe_run_contact_id_backfill(),
                    name="contact_id_backfill",
                )

                # ★ P2：陪伴主动话题调度（沉默检测 + 冷却 → P1 选题 → 主动开场）
                await self._maybe_start_companion_proactive()

                # 坐席工作台实时化（D5a）：后台轻量 ingest 轮询 → 新入站消息发 SSE 事件
                self._maybe_start_inbox_ingest_loop()

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
    
    def _maybe_start_inbox_ingest_loop(self) -> None:
        """D5a：启动收件箱后台 ingest 轮询循环。

        周期性把各平台 runner 的最近会话聚合 ingest 进 inbox.db；对**新入站消息**
        发 inbox_message 事件（坐席工作台 SSE 实时刷新）。冷启动首轮 warmup 不发事件。
        条件：inbox 已挂载 + web_app 就绪。
        """
        if self.inbox_store is None or self._web_app is None:
            return
        try:
            _inbox_cfg = (self.config.config or {}).get("inbox", {}) or {}
            interval = float(_inbox_cfg.get("realtime_poll_sec", 10))
        except Exception:
            interval = 10.0
        if interval <= 0:
            self.logger.info("收件箱实时 ingest 轮询已禁用（realtime_poll_sec<=0）")
            return
        self._inbox_ingest_task = asyncio.create_task(
            self._inbox_ingest_loop(interval), name="inbox_ingest_loop",
        )
        self.logger.info("✅ 收件箱实时 ingest 轮询已启动（interval=%ss）", interval)

    async def _inbox_ingest_loop(self, interval: float) -> None:
        from types import SimpleNamespace
        from src.inbox.channel_adapters import (
            default_inbox_adapters, collect_chats_via_adapters,
        )
        from src.inbox.ingest import ingest_collected_chats

        adapters = default_inbox_adapters()
        shim = SimpleNamespace(app=self._web_app)
        warmup = True  # 首轮只 ingest 不发事件，避免冷启动事件洪泛
        while self.running:
            try:
                chats = await asyncio.to_thread(
                    collect_chats_via_adapters, shim, 50, adapters,
                )
                # ingest（含发事件）放主循环线程执行：SSE 用的 asyncio.Queue 非线程安全
                ingest_collected_chats(
                    self.inbox_store, chats, publish_events=not warmup,
                )
                warmup = False
            except Exception:
                self.logger.debug("收件箱 ingest 轮询异常", exc_info=True)
            await asyncio.sleep(interval)

    def _build_contact_resolver(self):
        """Q 延伸：构造 (platform, account_id, chat_key) → contact_id 解析器。

        inbox/contacts 未就绪时返回 None。供 ingest 回写与存量回填共用。
        """
        if self.inbox_store is None or self.contacts is None:
            return None
        from src.contacts.identity_bridge import resolve_contact_id

        cstore = self.contacts.store

        def _resolver(platform: str, account_id: str, chat_key: str) -> str:
            return resolve_contact_id(
                cstore, platform=platform, account_id=account_id, chat_key=chat_key)

        return _resolver

    def _maybe_wire_ingest_contact_writeback(self) -> None:
        """Q 延伸：ingest 热路径回写 contact_id（默认关，companion.relations_health）。"""
        try:
            rh = ((self.config.config.get("companion") or {})
                  .get("relations_health") or {})
            if not rh.get("ingest_contact_id_writeback", False):
                self.logger.info(
                    "ingest contact_id 回写未启用"
                    "（companion.relations_health.ingest_contact_id_writeback=false）")
                return
            resolver = self._build_contact_resolver()
            if resolver is None:
                self.logger.info("ingest contact_id 回写跳过（inbox/contacts 未就绪）")
                return
            self.inbox_store.register_contact_resolver(resolver)
            self.logger.info("✅ ingest contact_id 回写已接线（Q 延伸）")
        except Exception:
            self.logger.warning("ingest contact_id 回写接线跳过", exc_info=True)

    async def _maybe_run_contact_id_backfill(self) -> None:
        """Q 延伸·存量回填：给历史会话补 contact_id（默认关，可 dry_run）。

        config: companion.relations_health.contact_id_backfill.{enabled, limit, dry_run,
        delay_seconds}。一次性启动任务，DB 扫描放线程池避免阻塞事件循环。
        """
        try:
            rh = ((self.config.config.get("companion") or {})
                  .get("relations_health") or {})
            bf = (rh.get("contact_id_backfill") or {})
            if not bf.get("enabled", False):
                return
            resolver = self._build_contact_resolver()
            if resolver is None:
                self.logger.info("contact_id 存量回填跳过（inbox/contacts 未就绪）")
                return
            delay = float(bf.get("delay_seconds", 20))
            await asyncio.sleep(max(0.0, delay))
            if not self.running:
                return
            from src.contacts.contact_backfill import backfill_contact_ids

            limit = max(1, min(int(bf.get("limit", 200)), 2000))
            dry_run = bool(bf.get("dry_run", False))
            result = await asyncio.to_thread(
                backfill_contact_ids, self.inbox_store, resolver,
                limit=limit, dry_run=dry_run,
            )
            import time as _time
            out = {**result.as_dict(), "trigger": "startup", "ts": _time.time()}
            if self._web_app is not None:
                self._web_app.state.last_contact_backfill = out
            self.logger.info("✅ contact_id 存量回填完成: %s", out)
        except Exception:
            self.logger.warning("contact_id 存量回填失败", exc_info=True)

    def _ensure_deferred_outbox(self):
        """惰性建/起多平台 deferred 队列（非 messenger 主动消息走此队列）。

        返回 dispatcher（已 start），或 None（功能关/不可用）。幂等：重复调用复用同一实例。
        sender 用编排器 `orch.send(platform,account,chat_key,text)` 统一投递（编排器已
        路由到对应平台 worker 并回写收件箱出站镜像）；worker 未就绪 → 抛 NotReady 推后重试。
        messenger 不走此队列（保留既有 runner deferred 路径）。
        """
        if self._deferred_outbox_dispatcher is not None:
            return self._deferred_outbox_dispatcher
        try:
            comp = (self.config.config.get("companion") or {})
            cfg = (comp.get("multiplatform_deferred") or {})
            if not cfg.get("enabled", False):
                return None
            from src.integrations.shared.deferred_outbox import (
                DeferredDispatcher, DeferredOutboxStore, DeferredSenderNotReady,
            )

            _cfg_dir = Path(self.config.config_path).parent
            store = DeferredOutboxStore(_cfg_dir / "deferred_outbox.db")

            async def _universal_send(account_id, chat_key, text, *, platform):
                # 1) 编排器受管 worker（telegram/whatsapp/line… 任一暴露 send 的）
                try:
                    from src.integrations.account_orchestrator import get_orchestrator
                    orch = get_orchestrator(self.config.config or {})
                    if orch.owns(platform, account_id):
                        res = await orch.send(platform, account_id, chat_key, text)
                        return bool((res or {}).get("delivered", True))
                except DeferredSenderNotReady:
                    raise
                except Exception:
                    self.logger.debug("[deferred_outbox] 编排器发送异常 %s:%s",
                                      platform, account_id, exc_info=True)
                # 2) 回落：主 A 线客户端（仅 telegram default）
                if platform == "telegram" and self.telegram_client is not None:
                    try:
                        target = int(chat_key)
                    except (TypeError, ValueError):
                        target = chat_key
                    try:
                        return bool(await self.telegram_client.send_message(target, text))
                    except Exception:
                        self.logger.debug("[deferred_outbox] 主客户端发送失败", exc_info=True)
                        return False
                # 3) 该账号此刻无可用 worker → 暂态，推后重试（不丢、不标失败）
                raise DeferredSenderNotReady(f"no worker for {platform}:{account_id}")

            def _make_sender(platform):
                async def _s(account_id, chat_key, text):
                    return await _universal_send(account_id, chat_key, text,
                                                 platform=platform)
                return _s

            dispatcher = DeferredDispatcher(
                store=store,
                quiet_start_hour=float(cfg.get("quiet_start_hour", 23)),
                quiet_end_hour=float(cfg.get("quiet_end_hour", 8)),
                min_gap_sec=float(cfg.get("min_gap_sec", 45)),
                max_per_tick=int(cfg.get("max_per_tick", 3)),
                interval_sec=float(cfg.get("interval_sec", 120)),
            )
            platforms = cfg.get("platforms") or [
                "telegram", "line", "whatsapp", "instagram", "zalo",
            ]
            for p in platforms:
                dispatcher.register_sender(str(p), _make_sender(str(p)))

            self._deferred_outbox_dispatcher = dispatcher
            if self._web_app is not None:
                self._web_app.state.deferred_outbox_store = store
                self._web_app.state.deferred_outbox_dispatcher = dispatcher
            self.logger.info(
                "✅ 多平台 deferred 队列已就绪（platforms=%s interval=%ss）",
                platforms, cfg.get("interval_sec", 120))
            return dispatcher
        except Exception:
            self.logger.warning("多平台 deferred 队列初始化失败（非 messenger 主动消息将被丢弃）",
                                 exc_info=True)
            return None

    def _enqueue_deferred_outbox(self, channel, account_id, chat_name, reply,
                                 defer_until, reason, staleness_sec, extra) -> int:
        """把非 messenger 主动消息入多平台 deferred 队列。返回 row_id（0=未入队）。

        作为 care/reactivation send_callback 的非 messenger 分支：队列关或不可用 → 返回 0
        （上层据此 mark_skipped/failed，与原「return 0」语义一致，零破坏）。
        """
        dispatcher = self._ensure_deferred_outbox()
        if dispatcher is None:
            return 0
        try:
            return dispatcher._store.enqueue(
                platform=str(channel), account_id=str(account_id or "default"),
                chat_key=str(chat_name), reply_text=str(reply),
                defer_until=float(defer_until), reason=str(reason or ""),
                staleness_sec=float(staleness_sec), extra=extra or {})
        except Exception:
            self.logger.debug("deferred_outbox enqueue 失败 %s", channel, exc_info=True)
            return 0

    async def _maybe_start_deferred_outbox(self) -> None:
        """启动多平台 deferred 队列 drain loop（默认关）。"""
        try:
            dispatcher = self._ensure_deferred_outbox()
            if dispatcher is None:
                self.logger.info(
                    "多平台 deferred 队列未启用"
                    "（companion.multiplatform_deferred.enabled=false）")
                return
            await dispatcher.start()
            self.logger.info("✅ 多平台 deferred 队列 drain loop 已启动")
        except Exception:
            self.logger.warning("多平台 deferred 队列启动跳过", exc_info=True)

    def _ensure_quality_trend(self):
        """惰性建质量趋势快照器（周期落地 companion_quality_overview）。

        返回 snapshotter（未 start），或 None（功能关/不可用）。幂等。
        store 挂到 app.state.quality_trend_store 供 /api/companion/quality-trend 读。
        """
        if self._quality_trend_snapshotter is not None:
            return self._quality_trend_snapshotter
        try:
            comp = (self.config.config.get("companion") or {})
            cfg = (comp.get("quality_trend") or {})
            if not cfg.get("enabled", False):
                return None
            from src.monitoring.metrics_store import get_metrics_store
            from src.monitoring.quality_trend_store import (
                QualityTrendSnapshotter, QualityTrendStore,
            )

            _cfg_dir = Path(self.config.config_path).parent
            store = QualityTrendStore(_cfg_dir / "quality_trend.db")
            win_h = float(cfg.get("window_hours", 24))

            def _overview():
                return get_metrics_store().companion_quality_overview(
                    window_sec=max(1.0, win_h) * 3600.0)

            snap = QualityTrendSnapshotter(
                store=store,
                overview_fn=_overview,
                interval_sec=float(cfg.get("interval_sec", 300)),
                retention_days=float(cfg.get("retention_days", 30)),
            )
            self._quality_trend_snapshotter = snap
            if self._web_app is not None:
                self._web_app.state.quality_trend_store = store
            self.logger.info(
                "✅ 质量趋势持久化已就绪（interval=%ss retention=%sd）",
                cfg.get("interval_sec", 300), cfg.get("retention_days", 30))
            return snap
        except Exception:
            self.logger.warning("质量趋势持久化初始化失败", exc_info=True)
            return None

    async def _maybe_start_quality_trend(self) -> None:
        """启动质量趋势快照循环（默认关）。"""
        try:
            snap = self._ensure_quality_trend()
            if snap is None:
                self.logger.info(
                    "质量趋势持久化未启用（companion.quality_trend.enabled=false）")
                return
            await snap.start()
            self.logger.info("✅ 质量趋势快照循环已启动")
        except Exception:
            self.logger.warning("质量趋势持久化启动跳过", exc_info=True)

    def _maybe_init_monetization(self, web_app=None) -> None:
        """Phase K2：C 端变现（默认关，monetization.enabled 开）。

        开启时建 EntitlementStore 单例（落 config/entitlements.db）→ 挂 app.state 供路由用，
        并按 catalog 注入价目；启动可选清理过期订阅。关时不建库（路由会按需懒建只读单例）。
        """
        try:
            mon = (self.config.config.get("monetization") or {})
            if not mon.get("enabled", False):
                self.logger.info("C 端变现未启用（monetization.enabled=false）")
                return
            from src.utils.entitlement_store import get_entitlement_store
            from src.utils.monetization import merge_catalog

            catalog = merge_catalog(mon.get("catalog"))
            _cfg_dir = Path(self.config.config_path).parent
            store = get_entitlement_store(_cfg_dir / "entitlements.db", catalog=catalog)
            if web_app is not None:
                web_app.state.entitlement_store = store
            # Stage 1：把真实权益接进对话路径——注册进程级 resolver，让付费剧情闸
            # （story_engine.require_unlock）据端用户真实拥有判准入。仅在变现就绪时注册，
            # 故未启用时 resolve_entitlement 恒 None → 付费场景仍对所有人锁（零回归）。
            try:
                from src.utils.companion_context import set_relationship_providers
                set_relationship_providers(
                    entitlement_resolver=lambda ck: store.get_entitlement(ck))
                self.logger.info("✅ 对话剧情付费闸已接入真实权益（entitlement resolver 已注册）")
            except Exception:
                self.logger.debug("entitlement resolver 注册失败", exc_info=True)
            if mon.get("expire_on_startup", True):
                try:
                    store.expire_subscriptions()
                except Exception:
                    pass
            self.logger.info("✅ C 端变现已就绪（EntitlementStore 已挂载）")
        except Exception:
            self.logger.warning("C 端变现初始化跳过", exc_info=True)

    def _build_care_paywall(self, care_store):
        """K2b：构造主动关怀配额门控回调。变现 gate 关 → 返回 None（不拦，零破坏）。

        回调懒读 app.state 的 MonetizationRuntime：免费用户近 24h 已发主动数超配额 → False。
        """
        try:
            mon = (self.config.config.get("monetization") or {})
            if not (mon.get("enabled") and (mon.get("gate") or {}).get("enabled")):
                return None
        except Exception:
            return None

        def _allowed(contact_key: str) -> bool:
            try:
                import time as _t
                from src.utils.monetization_runtime import MonetizationRuntime
                rt = MonetizationRuntime.from_app(self._web_app)
                if rt is None:
                    return True
                since = _t.time() - 86400.0
                sent = care_store.count_sent_since(contact_key, since)
                return rt.proactive_allowed(contact_key, sent)
            except Exception:
                return True  # 门控异常绝不拦关怀

        self.logger.info("✅ 主动关怀变现配额门控已接入")
        return _allowed

    async def _maybe_start_proactive_care(self, web_app=None) -> None:
        """Phase O：主动关怀引擎（默认关，companion.proactive_care.enabled 开）。

        捕获：入站新消息回调 → 抽取约定入 care_schedule（gated）。
        派发：到期由 CareDispatcher 经 messenger deferred 队列发出（复用 reactivation 护栏）。
        """
        try:
            cfg = ((self.config.config.get("companion") or {}).get("proactive_care") or {})
            if not cfg.get("enabled", False):
                self.logger.info("proactive_care 未启用（companion.proactive_care.enabled=false）")
                return
            from src.contacts.care_schedule import get_care_schedule_store

            _cfg_dir = Path(self.config.config_path).parent
            care_store = get_care_schedule_store(_cfg_dir / "care_schedule.db")
            if web_app is not None:
                web_app.state.care_schedule_store = care_store
            # 启动时清理逾期太久的待办（错过时机不补发）
            try:
                care_store.expire_overdue(grace_days=float(cfg.get("grace_days", 1)))
            except Exception:
                pass

            # 捕获接线：入站新消息 → 抽取入库（gated，复用 inbox 既有回调钩子）
            if self.inbox_store is not None and cfg.get("capture", True):
                try:
                    from src.contacts.care_capture import make_care_inbound_cb
                    self.inbox_store.register_new_inbound_cb(
                        make_care_inbound_cb(care_store, self.config))
                    self.logger.info("✅ proactive_care 入站捕获已接线")
                except Exception:
                    self.logger.warning("proactive_care 捕获接线跳过", exc_info=True)

            # 派发循环：需 messenger deferred 队列（与 reactivation 同款发送）
            if self.messenger_rpa_service is None or self.ai_client is None:
                self.logger.info("proactive_care 派发循环跳过（messenger_rpa/ai 未就绪），仅捕获")
                return
            from src.contacts.care_dispatcher import CareDispatcher

            async def _care_send(channel, account_id, chat_name, reply, defer_until,
                                 reason, staleness_sec, extra):
                if channel != "messenger":
                    # 非 messenger → 多平台 deferred 队列（关/不可用则返回 0，零破坏）
                    return self._enqueue_deferred_outbox(
                        channel, account_id, chat_name, reply, defer_until,
                        reason, staleness_sec, extra)
                return await self.messenger_rpa_service.enqueue_reactivation_deferred(
                    account_id=account_id, chat_name=chat_name, reply_text=reply,
                    defer_until=defer_until, defer_reason=reason,
                    staleness_sec=staleness_sec, extra=extra)

            def _care_context(contact_key: str) -> str:
                # 最近若干条消息文本作 prompt 可引用要点（best-effort）
                try:
                    msgs = self.inbox_store.list_messages(contact_key, limit=8) \
                        if self.inbox_store else []
                    lines = [str(m.get("text") or "").strip() for m in (msgs or [])]
                    return "\n".join(t for t in lines if t)[:800]
                except Exception:
                    return ""

            ai_name = "她"
            try:
                ai_name = str((self.config.get_ai_config() or {}).get("ai_name") or "她")
            except Exception:
                ai_name = "她"

            # K2b：变现配额门控回调（仅当变现 gate 开启才注入；否则 None=不拦，零破坏）
            proactive_paywall = self._build_care_paywall(care_store)

            dispatcher = CareDispatcher(
                store=care_store, ai_client=self.ai_client, send_callback=_care_send,
                context_provider=_care_context, proactive_allowed=proactive_paywall,
                ai_name=ai_name,
                max_per_tick=int(cfg.get("max_per_tick", 3)),
                interval_sec=float(cfg.get("interval_sec", 600)),
                skip_if_no_context=bool(cfg.get("skip_if_no_context", True)),
                quiet_start_hour=float(cfg.get("quiet_start_hour", 23)),
                quiet_end_hour=float(cfg.get("quiet_end_hour", 8)),
                dry_run=bool(cfg.get("dry_run", False)),
            )
            await dispatcher.start()
            self._care_dispatcher = dispatcher
            self.logger.info("✅ proactive_care 派发循环已启动（interval=%ss）",
                             cfg.get("interval_sec", 600))
        except Exception as ex:
            self.logger.warning("proactive_care 启动跳过: %s", ex)
            self.logger.debug("proactive_care 启动异常", exc_info=True)

    async def _maybe_start_companion_proactive(self) -> None:
        """P2：陪伴主动话题调度（默认关，companion.proactive_topic.enabled 开）。

        沉默检测 + 冷却 → P1 选题（build_proactive_opener，只回访高置信记忆）→
        ai 生成一句自然开场 → 经编排器受管 worker / 主 A 线客户端发出（自动镜像收件箱）。
        仅 Telegram 协议号；与 proactive_care(messenger 约定驱动) 互补、不重叠。
        """
        try:
            comp = (self.config.config.get("companion") or {})
            cfg = (comp.get("proactive_topic") or {})
            enabled = bool(cfg.get("enabled", False))
            # 预览（可观测面板）仅需 inbox + skill_manager；ai 仅"真发"时才需要。
            # 故即便未启用 / ai 未就绪，也先挂上"会发给谁、引用哪条记忆"的预览能力，
            # 让运营在真正开闸前先 dry-run 看清本轮候选。
            if self.inbox_store is None or self.skill_manager is None:
                self.logger.info(
                    "companion proactive_topic 跳过（inbox_store/skill_manager 未就绪，预览亦不可用）")
                return
            from src.integrations.companion_proactive import (
                CompanionProactiveLoop, JsonCooldownStore, plan_proactive_sends,
            )

            scan_limit = int(cfg.get("scan_limit", 200))
            min_silent_hours = float(cfg.get("min_silent_hours", 24))

            def _conversations():
                try:
                    rows = self.inbox_store.list_conversations(
                        limit=scan_limit, platform="telegram") or []
                except Exception:
                    return []
                cids = [str(r.get("conversation_id") or "")
                        for r in rows if r.get("conversation_id")]
                try:
                    dirs = self.inbox_store.last_message_dirs(cids)
                except Exception:
                    dirs = {}
                try:
                    tags_map = self.inbox_store.list_conv_tags_map(cids)
                except Exception:
                    tags_map = {}
                # Phase ④续⁹：把 inbox 末条情绪并入快照——让情绪护栏的 soft 档覆盖「非危机
                # 但明显低谷」（最近一条被分析为愤怒/不满/焦虑）→ 抑制剧情邀约、留温和问候。
                try:
                    meta_intel = self.inbox_store.get_conv_meta_for_ids(cids)
                except Exception:
                    meta_intel = {}
                # Phase ④续⁵：把真实 intimacy/funnel 注入快照——既让记忆开场的沉默阈值
                # 缩放更准，也让「主动剧情邀约」能按真实关系等级判断可邀约剧情。
                # 复用 N 线已就绪的进程级 provider（resolve_*）；未注册 → 返回 None → 退回 0/""。
                try:
                    from src.utils.companion_context import (
                        resolve_funnel_stage as _resolve_funnel_stage,
                        resolve_intimacy_score as _resolve_intimacy_score,
                    )
                except Exception:
                    _resolve_intimacy_score = None
                    _resolve_funnel_stage = None
                out = []
                for r in rows:
                    cid = str(r.get("conversation_id") or "")
                    chat_key = str(r.get("chat_key") or "")
                    platform = str(r.get("platform") or "telegram")
                    account_id = str(r.get("account_id") or "default")
                    meta = tags_map.get(cid, {}) or {}
                    _intim = 0.0
                    _stage = ""
                    if _resolve_intimacy_score is not None and chat_key:
                        try:
                            _v = _resolve_intimacy_score(
                                account_id, chat_key, channel=platform)
                            _intim = float(_v) if _v is not None else 0.0
                            _stage = _resolve_funnel_stage(
                                account_id, chat_key, channel=platform) or ""
                        except Exception:
                            _intim, _stage = 0.0, ""
                    out.append({
                        "conversation_id": cid,
                        "platform": platform,
                        "account_id": account_id,
                        "chat_key": chat_key,
                        "last_ts": r.get("last_ts") or 0,
                        "last_direction": (dirs.get(cid) or {}).get("direction") or "",
                        "archived": bool(meta.get("archived")),
                        # 私聊：episodic 记忆 key == 对端 id == chat_key
                        "memory_key": chat_key,
                        "stage": _stage,
                        "intimacy": _intim,
                        "last_emotion": str(
                            (meta_intel.get(cid) or {}).get("last_emotion") or ""),
                    })
                return out

            def _opener(*, memory_key, silent_hours, stage, intimacy, last_emotion=""):
                return self.skill_manager.build_proactive_opener(
                    memory_key, silent_hours=silent_hours, stage=stage,
                    intimacy=intimacy, min_silent_hours=min_silent_hours,
                    last_emotion=last_emotion)

            cd_path = Path(self.config.config_path).parent / "companion_proactive_cooldown.json"

            # 与 proactive_care(Phase O) 去重：已排关怀的会话让路（best-effort）。
            # 仅在 care 子系统已就绪（store 已挂 web_app.state）时生效，否则不去重、无害。
            care_store = None
            try:
                care_store = getattr(
                    getattr(self._web_app, "state", None), "care_schedule_store", None)
            except Exception:
                care_store = None

            def _has_pending_care(conversation_id: str) -> bool:
                if care_store is None:
                    return False
                try:
                    return int(care_store.count_pending_by_contact(conversation_id)) > 0
                except Exception:
                    return False

            # Phase ④续⁸：危机关怀升级——severe 近期危机的沉默用户被情绪护栏拦下时，
            # 不只静默，而是排一条高优先 care 待办（人工/关怀兜底），把"静默"变"接住"。
            # 幂等：排进后 has_pending_care→True，下个 tick 该会话整段让路、不会重排。
            _crisis_escalation_on = bool(cfg.get("crisis_care_escalation", True))

            def _on_crisis_block(conv) -> None:
                if care_store is None or not _crisis_escalation_on:
                    return
                cid = str((conv or {}).get("conversation_id") or "")
                if not cid:
                    return
                try:
                    import time as _time
                    from src.contacts.care_commitment import CareCommitment
                    from src.contacts.care_schedule import CRISIS_CARE_TOPIC
                    _now = _time.time()
                    care_store.add_commitment(
                        CareCommitment(
                            due_at=_now,            # 立即到期 → 下个派发 tick 即可被关怀/坐席接住
                            event_at=_now,
                            topic=CRISIS_CARE_TOPIC,  # 派发器据此切「克制陪伴」语气模板
                            sentiment="negative",
                            anchor_text="",
                            source_text="近期危机信号，主动护栏拦下打扰，转关怀回访",
                            confidence=1.0,
                        ),
                        contact_key=cid,
                        platform=str((conv or {}).get("platform") or ""),
                        account_id=str((conv or {}).get("account_id") or ""),
                        chat_key=str((conv or {}).get("chat_key") or ""),
                    )
                except Exception:
                    self.logger.debug("[proactive] 危机关怀升级排队失败 cid=%s", cid, exc_info=True)

            # 采样评分回流存储（质量闭环）：试发采样落库，供 👍/👎 评分 + 调参看板。
            sample_store = None
            try:
                from src.integrations.companion_sample_store import (
                    get_companion_sample_store,
                )
                _sdb = Path(self.config.config_path).parent / "companion_samples.db"
                sample_store = get_companion_sample_store(_sdb)
                self._web_app.state.companion_sample_store = sample_store
            except Exception:
                sample_store = None
                self.logger.debug("[proactive] 采样评分存储初始化失败", exc_info=True)

            # few-shot 风格示范注入（默认关，人审样本后开）：把人工高赞/改写样本作口吻示范
            # 拼进生成 prompt（只学风格不照抄内容），让评分数据反哺生成——自我改进环。
            _fs_cfg = (cfg.get("few_shot") or {})
            _fs_enabled = bool(_fs_cfg.get("enabled", False))
            _fs_max = int(_fs_cfg.get("max_examples", 3))

            _pp_params = dict(
                min_silent_hours=min_silent_hours,
                cooldown_hours=float(cfg.get("cooldown_hours", 72)),
                quiet_start_hour=float(cfg.get("quiet_start_hour", 23)),
                quiet_end_hour=float(cfg.get("quiet_end_hour", 8)),
            )
            _real_max_per_tick = int(cfg.get("max_per_tick", 3))

            def _proactive_preview(limit=50):
                """可观测预览（dry-run）：本轮"会主动联系谁、引用哪条记忆、带哪些背景"。
                不发送、不写冷却；即便功能未启用也可调用（开闸前先看清候选）。"""
                lim = max(1, min(int(limit or 50), 200))
                try:
                    convs = _conversations()
                except Exception:
                    convs = []
                try:
                    cooldown_map = JsonCooldownStore(cd_path).snapshot()
                except Exception:
                    cooldown_map = {}
                # 预览展示全部候选（最多 lim 条），不受 max_per_tick 截断；
                # 另标出本 tick 实际会发的前 N 条（按沉默时长降序）。
                plans = plan_proactive_sends(
                    convs, cooldown_map=cooldown_map, opener_fn=_opener,
                    has_pending_care=_has_pending_care, max_per_tick=lim, **_pp_params)
                for i, p in enumerate(plans):
                    p["would_send_this_tick"] = i < _real_max_per_tick
                return {
                    "enabled": enabled,
                    "dry_run": bool(cfg.get("dry_run", False)),
                    "scanned": len(convs),
                    "candidates": len(plans),
                    "max_per_tick": _real_max_per_tick,
                    "min_silent_hours": min_silent_hours,
                    "cooldown_hours": _pp_params["cooldown_hours"],
                    "quiet_hours": [_pp_params["quiet_start_hour"], _pp_params["quiet_end_hour"]],
                    "care_dedup_active": care_store is not None,
                    "plans": plans,
                }

            ai_name = "她"
            try:
                ai_name = str((self.config.get_ai_config() or {}).get("ai_name") or "她")
            except Exception:
                ai_name = "她"

            async def _gen_text(plan):
                """按 plan 生成"要发出去的那一句"（directive + 背景记忆 + 最近上下文）。
                只生成、不发送；ai 未就绪或空回复 → 返回 ""。真发 _send 与试发预览共用。"""
                ctx_lines = []
                try:
                    msgs = self.inbox_store.list_recent_messages(
                        plan["conversation_id"], limit=6) or []
                    ctx_lines = [str(m.get("text") or "").strip()
                                 for m in msgs if str(m.get("text") or "").strip()]
                except Exception:
                    ctx_lines = []
                ctx = "\n".join(ctx_lines[-6:])[:600]
                prompt = (
                    f"你是「{ai_name}」，正在主动给一位许久未联系的朋友发消息。\n"
                    f"{plan['directive']}\n"
                    f"要求：只输出要发出去的那一句话本身，口语化、温暖、自然，不超过40字，"
                    f"不要解释、不要加引号、不要署名。\n"
                )
                # P1b：把其他高置信记忆作"背景"喂给模型，让开场更贴心自然，
                # 但严格只作背景——绝不罗列、不逐条追问（与 directive 的克制一致）。
                extra_facts = [
                    str(f).strip()
                    for f in (plan.get("context_facts") or [])
                    if str(f).strip()
                ]
                if extra_facts:
                    prompt += (
                        "\n（背景：你还记得关于TA的这些事，仅用来把这一句说得更走心，"
                        "绝不要罗列、不要逐条追问）：\n- "
                        + "\n- ".join(extra_facts[:3]) + "\n"
                    )
                if ctx:
                    prompt += f"\n（可参考你们最近的聊天，但不要复读原话）：\n{ctx}\n"
                # few-shot 风格示范（默认关）：人工认可样本作口吻示范，反哺生成。
                if _fs_enabled and sample_store is not None:
                    try:
                        from src.integrations.companion_sample_store import (
                            build_few_shot_block,
                        )
                        rows = (sample_store.list_recent(limit=50, rating="down")
                                + sample_store.list_recent(limit=50, rating="up"))
                        # 按当前 plan 的 mode 分桶取示范（follow_up / gentle_checkin 各用各的）
                        fs_block = build_few_shot_block(
                            rows, max_examples=_fs_max,
                            mode=str(plan.get("mode") or ""))
                        if fs_block:
                            prompt += fs_block
                    except Exception:
                        pass
                try:
                    text = await self.ai_client.chat(prompt)
                except Exception:
                    return ""
                return (text or "").strip()

            async def _proactive_generate(conversation_id):
                """试发采样：对某会话生成 AI 实际会说的那句话，但**不发送、不写冷却**。
                让运营开闸前先读到真实文案（会真实调用一次 AI，有 token 成本）。"""
                if self.ai_client is None:
                    return {"generated": False, "reason": "ai_not_ready", "message": "AI 未就绪"}
                cid = str(conversation_id or "")
                if not cid:
                    return {"generated": False, "reason": "missing",
                            "message": "缺 conversation_id"}
                try:
                    conv = next((c for c in (_conversations() or [])
                                 if str(c.get("conversation_id")) == cid), None)
                except Exception:
                    conv = None
                if conv is None:
                    return {"generated": False, "reason": "not_found",
                            "message": "会话不在当前扫描范围"}
                import time as _time
                try:
                    last_ts = float(conv.get("last_ts") or 0)
                except (TypeError, ValueError):
                    last_ts = 0.0
                silent_hours = (_time.time() - last_ts) / 3600.0 if last_ts > 0 else 0.0
                try:
                    opener = _opener(
                        memory_key=str(conv.get("memory_key") or ""),
                        silent_hours=silent_hours,
                        stage=str(conv.get("stage") or ""),
                        intimacy=float(conv.get("intimacy") or 0.0)) or {}
                except Exception:
                    opener = {}
                if not opener.get("mode") or not opener.get("directive"):
                    return {"generated": False, "reason": "not_eligible",
                            "message": "该会话当前不构成主动开场（沉默不足/无可回访记忆）"}
                plan = {
                    "conversation_id": cid,
                    "directive": str(opener.get("directive") or ""),
                    "context_facts": list(opener.get("context_facts") or []),
                    "mode": str(opener.get("mode") or ""),
                }
                text = await _gen_text(plan)
                # 采样落库（质量闭环）：供运营 👍/👎 评分回流；失败不影响返回文案。
                sample_id = None
                if sample_store is not None and text:
                    try:
                        sample_id = sample_store.record_sample(
                            conversation_id=cid,
                            account_id=str(conv.get("account_id") or ""),
                            mode=str(opener.get("mode") or ""),
                            fact=str(opener.get("fact") or ""),
                            context_facts_n=len(opener.get("context_facts") or []),
                            silent_hours=silent_hours, text=text)
                    except Exception:
                        sample_id = None
                return {
                    "generated": bool(text),
                    "text": text,
                    "sample_id": sample_id,
                    "mode": str(opener.get("mode") or ""),
                    "fact": str(opener.get("fact") or ""),
                    "context_facts": [str(f) for f in (opener.get("context_facts") or [])],
                    "silent_hours": round(silent_hours, 1),
                }

            try:
                self._web_app.state.companion_proactive_preview = _proactive_preview
                self._web_app.state.companion_proactive_generate = _proactive_generate
            except Exception:
                self.logger.debug("[proactive] 预览/试发回调挂载失败", exc_info=True)

            if not enabled:
                self.logger.info(
                    "companion proactive_topic 未启用"
                    "（预览可用：GET /api/companion/proactive/preview）")
                return
            if self.ai_client is None:
                self.logger.info(
                    "companion proactive_topic 已启用但 ai 未就绪，调度不启动（预览仍可用）")
                return

            async def _send(plan):
                # 1) 生成开场文案（复用 _gen_text：directive + 背景记忆 + 最近上下文）
                text = await _gen_text(plan)
                if not text:
                    return False
                platform = plan["platform"]
                account_id = plan["account_id"]
                chat_key = plan["chat_key"]
                # 2) 优先编排器受管 worker（自动回写收件箱出站镜像）
                try:
                    from src.integrations.account_orchestrator import get_orchestrator
                    orch = get_orchestrator(self.config.config or {})
                    if orch.owns(platform, account_id):
                        res = await orch.send(platform, account_id, chat_key, text)
                        return bool((res or {}).get("delivered", True))
                except Exception:
                    self.logger.debug("[proactive] 编排器发送失败，回落主客户端", exc_info=True)
                # 3) 回落：主 A 线客户端（default 账号）
                if self.telegram_client is not None and platform == "telegram":
                    try:
                        target = int(chat_key)
                    except (TypeError, ValueError):
                        target = chat_key
                    try:
                        ok = await self.telegram_client.send_message(target, text)
                        return bool(ok)
                    except Exception:
                        self.logger.debug("[proactive] 主客户端发送失败", exc_info=True)
                        return False
                return False

            loop = CompanionProactiveLoop(
                conversations_provider=_conversations,
                opener_fn=_opener,
                send_fn=_send,
                cooldown_store=JsonCooldownStore(cd_path),
                interval_sec=float(cfg.get("interval_sec", 900)),
                min_silent_hours=min_silent_hours,
                cooldown_hours=float(cfg.get("cooldown_hours", 72)),
                max_per_tick=int(cfg.get("max_per_tick", 3)),
                quiet_start_hour=float(cfg.get("quiet_start_hour", 23)),
                quiet_end_hour=float(cfg.get("quiet_end_hour", 8)),
                dry_run=bool(cfg.get("dry_run", False)),
                has_pending_care=_has_pending_care,
                on_crisis_block=_on_crisis_block,
            )
            await loop.start()
            self._companion_proactive_loop = loop
            self.logger.info(
                "✅ companion proactive_topic 调度已启动"
                "（interval=%ss min_silent=%sh cooldown=%sh dry_run=%s）",
                cfg.get("interval_sec", 900), min_silent_hours,
                cfg.get("cooldown_hours", 72), cfg.get("dry_run", False))
        except Exception as ex:
            self.logger.warning("companion proactive_topic 启动跳过: %s", ex)
            self.logger.debug("companion proactive_topic 启动异常", exc_info=True)

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
                    # 非 messenger → 多平台 deferred 队列（关/不可用则返回 0，零破坏）
                    return self._enqueue_deferred_outbox(
                        channel, account_id, chat_name, reply, defer_until,
                        reason, staleness_sec, extra)
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
                platform_priority=(
                    cfg_react.get("platform_priority")
                    or ["messenger", "telegram", "line", "whatsapp"]
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

            # D5a：收件箱 ingest 轮询优雅停止
            if self._inbox_ingest_task is not None:
                try:
                    self._inbox_ingest_task.cancel()
                    self.logger.info("收件箱 ingest 轮询已停止")
                except Exception as ex:
                    self.logger.warning("收件箱 ingest 轮询停止异常: %s", ex)

            # W2-D4.2：reactivation_loop 优雅停止
            if self._reactivation_loop is not None:
                try:
                    await self._reactivation_loop.stop()
                    self.logger.info("reactivation_loop 已停止")
                except Exception as ex:
                    self.logger.warning("reactivation_loop 停止异常: %s", ex)

            # P2：companion 主动话题调度优雅停止
            if self._companion_proactive_loop is not None:
                try:
                    await self._companion_proactive_loop.stop()
                    self.logger.info("companion proactive_topic 调度已停止")
                except Exception as ex:
                    self.logger.warning("companion proactive_topic 停止异常: %s", ex)

            # Phase O：care_dispatcher 优雅停止
            if self._care_dispatcher is not None:
                try:
                    await self._care_dispatcher.stop()
                    self.logger.info("care_dispatcher 已停止")
                except Exception as ex:
                    self.logger.warning("care_dispatcher 停止异常: %s", ex)

            # 多平台 deferred 队列优雅停止
            if self._deferred_outbox_dispatcher is not None:
                try:
                    await self._deferred_outbox_dispatcher.stop()
                    self.logger.info("多平台 deferred 队列已停止")
                except Exception as ex:
                    self.logger.warning("deferred_outbox 停止异常: %s", ex)

            # 质量趋势快照器优雅停止
            if self._quality_trend_snapshotter is not None:
                try:
                    await self._quality_trend_snapshotter.stop()
                    self.logger.info("质量趋势快照器已停止")
                except Exception as ex:
                    self.logger.warning("quality_trend 停止异常: %s", ex)

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


def run_config_check(config_path: str = None) -> int:
    """``python main.py --check`` 干跑模式：仅加载并体检配置，不启动任何服务。

    返回进程退出码（有 error 级问题 → 1，否则 0），便于 CI / 部署脚本 gate。
    """
    import yaml

    from src.utils.config_check import check_config, format_report, has_errors
    from src.utils.config_manager import ConfigManager

    cm = ConfigManager(config_path)
    path = cm.config_path
    if not Path(path).exists():
        print(f"✗ 配置文件不存在: {path}")
        print("  → 复制 config/config.example.yaml 为 config/config.yaml 并填写")
        return 1
    try:
        with open(path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        print(f"✗ YAML 解析失败: {path}\n  → {exc}")
        return 1

    print(f"配置文件: {path}")
    issues = check_config(config, config_path=path)
    print(format_report(issues, config=config if isinstance(config, dict) else None))
    return 1 if has_errors(issues) else 0


def run_init(preset: str, config_path: str = None, set_pairs=None, force: bool = False) -> int:
    """``python main.py --init [PRESET]`` 场景预设脚手架。

    无 PRESET → 列出可用预设并退出；有 PRESET → 生成 config.yaml + 跑自检闭环。
    """
    from src.utils.config_init import (
        describe_preset,
        list_presets,
        parse_set_args,
        scaffold_config,
    )
    from src.utils.config_check import format_report, has_errors
    from src.utils.config_manager import ConfigManager

    presets = list_presets()
    if not preset:
        print("可用场景预设（python main.py --init <名称>）:")
        for name in presets:
            desc = describe_preset(name)
            print(f"  - {name:<12} {desc}")
        if not presets:
            print("  （config/presets/ 下暂无预设）")
        return 0

    if preset not in presets:
        print(f"✗ 未知预设: {preset}；可用: {', '.join(presets) or '（无）'}")
        return 1

    dest = Path(config_path) if config_path else ConfigManager().config_path
    if str(dest).endswith("config.example.yaml"):
        # 默认路径在 config.yaml 不存在时会回落 example；--init 应写 config.yaml
        dest = Path(dest).parent / "config.yaml"

    overrides = parse_set_args(set_pairs)
    # 交互补填关键空位（仅 TTY；非交互/CI 走 --set）
    if sys.stdin.isatty():
        if "ai.api_key" not in overrides:
            ans = input("AI api_key（回车跳过，稍后手填）: ").strip()
            if ans:
                overrides["ai.api_key"] = ans

    ok, msg, issues = scaffold_config(preset, dest, overrides=overrides, force=force)
    print(msg)
    if not ok:
        return 1
    print(format_report(issues, config=None))
    print("\n下一步: 编辑上面文件填好必填项，再运行 `python main.py --check` 复核。")
    return 1 if has_errors(issues) else 0


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
    import argparse

    parser = argparse.ArgumentParser(
        description="Telegram MTProto AI 多平台客服主程序")
    parser.add_argument(
        "--check", action="store_true",
        help="只体检配置并退出（不启动服务）；有 error 级问题返回非零退出码")
    parser.add_argument(
        "--init", nargs="?", const="", metavar="PRESET",
        help="用场景预设生成 config.yaml（无名称则列出可用预设）")
    parser.add_argument(
        "--set", action="append", metavar="KEY=VAL",
        help="--init 时覆盖配置项，如 --set ai.api_key=sk-xxx（可多次）")
    parser.add_argument(
        "--force", action="store_true", help="--init 时覆盖已存在的 config.yaml")
    parser.add_argument(
        "--config", default=None, help="指定 config.yaml 路径（默认 config/config.yaml）")
    args = parser.parse_args()

    if args.init is not None:
        sys.exit(run_init(args.init, args.config, args.set, args.force))

    if args.check:
        sys.exit(run_config_check(args.config))

    # 设置默认事件循环策略（Windows需要）
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    # 运行主程序
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
