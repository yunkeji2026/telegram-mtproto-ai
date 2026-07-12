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


# 启动期环境/配置探测辅助已抽取到 src/bootstrap/env_probe.py（2026-07-12 Stage 1.5，行为不变）
# 保留 main.* 命名以兼容 tests/test_desktop_boot_gate.py 的 main._telegram_configured 等访问。
from src.bootstrap.env_probe import (
    _is_desktop_mode,
    _resolve_mobile_auto_openclaw_db,
    _telegram_configured,
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
        # 本机 IndexTTS2 情感克隆服务的进程托管（随主程序启停；见 local_autostart 开关）
        self.local_tts = None
        # W2-D4: 主动唤醒循环引用（关程序时 stop）
        self._reactivation_loop = None
        # Phase O: 主动关怀派发器引用（关程序时 stop）
        self._care_dispatcher = None
        # P2: 陪伴主动话题调度循环引用（关程序时 stop）
        self._companion_proactive_loop = None
        self._companion_funnel_store = None
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

            # 2b. 本机情感克隆(IndexTTS2)进程托管：随主程序一起启停（默认关，见
            #     minicpm_clone.local_autostart）。尽早拉起，让 ~60-90s 的 eager 载入
            #     与后续初始化/登录并行；非阻塞，失败只回落 edge，绝不挡启动。
            try:
                from src.integrations.local_tts_supervisor import LocalTTSSupervisor
                self.local_tts = LocalTTSSupervisor(
                    self.config.config.get("minicpm_clone") or {})
                await self.local_tts.start()
            except Exception as ex:
                self.logger.warning("本机 TTS 托管启动异常（忽略，语音走回落）: %s", ex)

            # 2c. AvatarHub 语音预热：对每个配了参考音的人设调 7852 register_spk
            #     （显著降首句延迟）。后台 daemon 线程 fire-and-forget：服务没起会先经
            #     计划任务拉起再轮询；任何失败只影响首句延迟，绝不挡启动/主流程。
            try:
                from src.ai.avatar_voice import warmup_personas_async
                if (self.config.config.get("avatar_voice") or {}).get("enabled"):
                    warmup_personas_async(self.config.config)
                    self.logger.info("AvatarHub 语音预热已调度（后台）")
            except Exception as ex:
                self.logger.warning("AvatarHub 语音预热调度异常（忽略）: %s", ex)

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
                    # ★★ src.* 命名空间的 INFO 也要落盘（2026-07-12 排障盲区修复）：
                    # root 钉在 WARNING 防第三方库（httpx/uvicorn/pyrogram）刷屏，代价是
                    # 本仓 src.* 业务模块的 INFO（「配置热重载完成」「入站翻译超时」
                    # 「backfill 消化」…）在 app.log 全体隐身——线上行为无从追溯。
                    # 单独给 "src" logger 挂同一 file handler（幂等/防重复行语义见模块单测）。
                    try:
                        from src.utils.log_setup import attach_src_file_handler
                        attach_src_file_handler(file_handler, level=level)
                    except Exception:
                        pass

                self.logger.info(f"日志已重新配置: level={log_level}, file={log_file}")

            # 3b. 进程退出可观测（2026-07-12 无痕死亡排障配套）：哨兵残留检测上次
            # 非正常死亡（taskkill /F / OOM 等任何死法）+ atexit/signal 记退出原因 +
            # faulthandler 落致命 traceback。失败绝不挡启动。
            try:
                from src.utils.exit_sentinel import install as _install_exit_obs
                _install_exit_obs()
            except Exception:
                self.logger.debug("退出可观测安装失败（已忽略）", exc_info=True)

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

            # 5. Telegram 协议客户端(registry + N5 + desktop/client-init)
            from src.bootstrap.services import setup_telegram_clients
            await setup_telegram_clients(self)
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

            # RPA 服务: LINE / Messenger / WhatsApp
            from src.bootstrap.services import setup_rpa_services
            setup_rpa_services(self)

            # 设备管理: 协调器 / 注册表 / 热插拔(HotPlug)
            from src.bootstrap.services import setup_device_management
            setup_device_management(self)

            # ── Contacts 跨平台子系统（feature flag 控制）──
            from src.bootstrap.services import setup_contacts_subsystem
            setup_contacts_subsystem(self)

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
                    _tc_for_web = self.telegram_client
                    web_app = create_app(
                        self.config, audit_store=audit,
                        boot_ts=(_tc_for_web._boot_timestamp
                                 if _tc_for_web is not None else 0),
                        telegram_client=_tc_for_web,
                        event_tracker=(_tc_for_web.event_tracker
                                       if _tc_for_web is not None else None),
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
                    if self.local_tts is not None:
                        web_app.state.local_tts_supervisor = self.local_tts

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

                            from src.bootstrap.web_app import make_api_auth
                            _drafts_api_auth = make_api_auth(web_app)

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
                                    # 全自动真实投递：默认 false（仅 DB 标记+审计，不发客户）。
                                    # 置 inbox.l2_autosend.deliver=true 才真正把 L2 草稿发到平台，
                                    # 且仅对会话档位=全自动(auto_ai) 的低风险草稿生效（双重 opt-in）。
                                    _deliver = bool(_as_cfg.get("deliver", False))
                                    from src.inbox.autosend_helpers import build_autosend_callbacks
                                    _send_cb, _translate_cb = build_autosend_callbacks(self, web_app, _deliver)
                                    _as_worker = AutosendWorker(
                                        draft_service=draft_svc,
                                        config=_merged_as_cfg,
                                        send_callback=_send_cb,
                                        translate_callback=_translate_cb,
                                    )
                                    web_app.state.autosend_worker = _as_worker
                                    # C3：注册 L2 事件驱动钩子，新草稿落库时立即唤醒
                                    self.inbox_store.register_l2_callback(
                                        _as_worker.notify_new_l2
                                    )
                                    asyncio.ensure_future(_as_worker.run())
                                    self.logger.info(
                                        "AutosendWorker 已启动（min=%ss max=%ss deliver=%s）",
                                        _as_cfg.get("min_interval_sec", 60),
                                        _as_cfg.get("max_interval_sec", 600),
                                        _deliver,
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

                            # ── 入站翻译存量消化（低频巡检，默认关）─────────
                            # workspace.auto_translate_inbound.backfill.enabled=true 开启；
                            # 闲时把老会话未译存量提前译好落库，坐席首开即毫秒级+译文备好。
                            # 复用 enrich 同一套判定/防重/负缓存（会话级锁与在线路径互斥）。
                            try:
                                from src.workspace.inbound_backfill import (
                                    InboundXlateBackfillWorker,
                                )
                                _bfw = InboundXlateBackfillWorker(
                                    inbox_store=self.inbox_store,
                                    config_manager=self.config,
                                    translation_svc_getter=lambda: getattr(
                                        web_app.state, "translation_service", None),
                                )
                                web_app.state.inbound_backfill_worker = _bfw
                                asyncio.ensure_future(_bfw.run())
                                self.logger.info(
                                    "InboundXlateBackfill 已启动（默认关，按 backfill.enabled 热生效）")
                            except Exception:
                                self.logger.debug("InboundXlateBackfill 启动跳过", exc_info=True)

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
                            from src.inbox.autodraft_helpers import setup_auto_draft
                            setup_auto_draft(self, draft_svc, web_app)

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
                            # K：引擎置信度智能切换（默认关 → min_confidence=0 行为不变）
                            _conf_sw = (_tr_cfg.get("engines") or {}).get("confidence_switch") or {}
                            _min_conf = (
                                float(_conf_sw.get("min_confidence", 0.5) or 0.5)
                                if _conf_sw.get("enabled", False) else 0.0
                            )
                            # 按目标语引擎覆写（弱语对直走强引擎；只重排 order 内引擎）
                            _per_lang = (_tr_cfg.get("engines") or {}).get("per_lang_order") or {}
                            # 在线语义闸门（confidence_switch 的可选进阶；默认关。
                            # 开启需 confidence_switch.enabled + semantic.enabled + 嵌入端点已配）
                            _sem_cfg = _conf_sw.get("semantic") or {}
                            _sem_fn = None
                            _sem_min = float(_sem_cfg.get("min_similarity", 0.65) or 0.65)
                            if (_conf_sw.get("enabled", False)
                                    and _sem_cfg.get("enabled", False)
                                    and self.ai_client is not None
                                    and hasattr(self.ai_client, "embed")):
                                _sem_fn = self.ai_client.embed
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
                                min_confidence=_min_conf,
                                per_lang_order=_per_lang,
                                semantic_embed_fn=_sem_fn,
                                semantic_min_similarity=_sem_min,
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

                            from src.bootstrap.web_app import make_api_auth
                            _contacts_api_auth = make_api_auth(web_app)

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

                    # ★ 隔离 web 到独立线程 + 独立 event loop（Stage 2：启动逻辑抽到
                    # src/bootstrap/web_app.py::start_web_server_thread，行为不变）
                    from src.bootstrap.web_app import start_web_server_thread
                    self._web_thread = start_web_server_thread(self, server, web_host, web_port)
                    self.logger.info(
                        "Web 管理后台正在绑定 http://%s:%s（独立线程隔离，避免抢占主 event loop）",
                        web_host,
                        web_port,
                    )
                except Exception as ex:
                    self.logger.warning("Web 管理后台启动跳过: %s", ex)

            # 监控 API 后台线程（Stage 2：抽到 bootstrap/web_app.py::start_monitoring_thread）
            from src.bootstrap.web_app import start_monitoring_thread
            start_monitoring_thread(self)
            return True

        except Exception as e:
            self.logger.error(f"初始化失败: {e}")
            return False

    async def start(self):
        """启动 AI 聊天助手(Stage4:实现已迁至 bootstrap/lifecycle)。"""
        from src.bootstrap.lifecycle import start_assistant
        return await start_assistant(self)

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

    def _ensure_deferred_outbox(self, *args, **kwargs):
        from src.bootstrap.background_tasks import ensure_deferred_outbox
        return ensure_deferred_outbox(self, *args, **kwargs)

    async def _maybe_translate_outbound(self, platform, account_id, chat_key, text):
        """deferred 主动触达投递前的出站自动翻译（best-effort，绝不阻塞投递）。

        复用 L2 autosend 同一 ``translate_outbound_text``（含「已是客户语言则跳过」检测护栏）
        与同一开关 ``inbox.l2_autosend.translate.enabled``。translation_service 懒取，
        会话客户语言经 conversations.language 解析。任何缺失/异常 → 回落发原文。
        """
        try:
            from src.inbox.outbound_translate import (
                parse_outbound_translate_cfg, translate_outbound_text,
            )
            cfg = parse_outbound_translate_cfg(self.config.config or {})
            if not cfg.get("enabled"):
                return text
            ts = getattr(self._web_app.state, "translation_service", None) \
                if self._web_app is not None else None
            if ts is None or self.inbox_store is None:
                return text
            from src.inbox.draft_models import _conv_id
            item = {"conversation_id": _conv_id(str(platform), str(account_id), str(chat_key)),
                    "text": str(text)}
            return await translate_outbound_text(
                item, translation_service=ts, store=self.inbox_store,
                source_lang=cfg.get("source_lang") or "zh", style=cfg.get("style") or "chat")
        except Exception:
            self.logger.debug("[deferred_outbox] 出站翻译跳过", exc_info=True)
            return text

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

    def _maybe_init_tts_cost_log(self) -> None:
        """P4-B：按 ``voice_routing.cost_log.enabled`` 装配 TTS 成本日聚合落库（默认关）。

        开启后 ``tts_pipeline._record_stats`` 旁路把每次合成写入 ``tts_cost.db``，
        ops 看板经 ``/api/admin/tts-cost-trend`` 读近 N 天花费/缓存命中曲线。
        关闭时 ``record_tts_cost`` 恒 no-op（无 voice 用量部署零 IO）。
        """
        try:
            vr = (self.config.config.get("voice_routing") or {})
            cl = (vr.get("cost_log") or {})
            if not cl.get("enabled", False):
                self.logger.info("TTS 成本落库未启用（voice_routing.cost_log.enabled=false）")
                return
            from src.ai.tts_cost_store import configure_tts_cost_store
            _cfg_dir = Path(self.config.config_path).parent
            store = configure_tts_cost_store(
                enabled=True,
                db_path=_cfg_dir / "tts_cost.db",
                retention_days=float(cl.get("retention_days", 90)),
            )
            if store is not None:
                self.logger.info(
                    "✅ TTS 成本落库已就绪（retention=%sd）", cl.get("retention_days", 90))
        except Exception:
            self.logger.warning("TTS 成本落库初始化失败（已忽略）", exc_info=True)

    def _maybe_init_translation_trend_log(self) -> None:
        """S：按 ``translation.engines.confidence_switch.trend_log`` 装配翻译置信度日聚合落库（默认关）。

        开启后 ``EngineRouter.translate`` 旁路把每次翻译的 {尝试/低置信/切换} 写入
        ``xlate_trend.db``，ops 看板经 ``/api/admin/translation-confidence-trend``
        读近 N 天低置信率/切换率 sparkline。关闭时 ``record_translation_trend`` 恒 no-op。
        """
        try:
            _tr = (self.config.config.get("translation") or {})
            _cs = ((_tr.get("engines") or {}).get("confidence_switch") or {})
            if not _cs.get("trend_log", False):
                self.logger.info(
                    "翻译置信度趋势落库未启用（translation.engines.confidence_switch.trend_log=false）")
                return
            from src.ai.translation_trend_store import configure_translation_trend_store
            _cfg_dir = Path(self.config.config_path).parent
            store = configure_translation_trend_store(
                enabled=True,
                db_path=_cfg_dir / "xlate_trend.db",
                retention_days=float(_cs.get("trend_retention_days", 90)),
            )
            if store is not None:
                self.logger.info(
                    "✅ 翻译置信度趋势落库已就绪（retention=%sd）",
                    _cs.get("trend_retention_days", 90))
        except Exception:
            self.logger.warning("翻译置信度趋势落库初始化失败（已忽略）", exc_info=True)

    def _init_persona_media_store(self) -> None:
        """装配每人设「相册/媒体」注册表（DB 落 config/persona_media.db；始终开启）。

        媒体元数据（触发词/配文/权重/关系闸门/命中）落库，文件落 static/persona_albums；
        相册后台（``/api/personas/{pid}/media*``）与回复链（image_autosend / skill_manager
        Stage 0）读同一份 store。DB 路径随 config 目录，避免与 :memory: 单测串味。
        """
        try:
            from src.companion.persona_media_store import configure_persona_media_store
            _cfg_dir = Path(self.config.config_path).parent
            store = configure_persona_media_store(_cfg_dir / "persona_media.db")
            if store is not None:
                self.logger.info("✅ 每人设相册/媒体注册表就绪（persona_media.db）")
        except Exception:
            self.logger.warning("每人设相册/媒体注册表初始化失败（已忽略）", exc_info=True)

    def _maybe_init_identity_trend_log(self) -> None:
        """F1：按 ``inbox.identity.trend_log`` 装配会话身份健康日聚合落库（默认关）。

        开启后 ``_record_ingest_identity`` / ``_record_avatar`` 旁路把入站 named/backfilled/raw
        与头像 hit/empty/total 写入 ``identity_trend.db``，ops 看板经
        ``/api/admin/identity-health-trend`` 读近 N 天 raw%/empty% sparkline。关闭时
        ``record_identity_trend`` 恒 no-op。
        """
        try:
            _inbox = (self.config.config.get("inbox") or {})
            _ident = (_inbox.get("identity") or {})
            if not _ident.get("trend_log", False):
                self.logger.info("会话身份趋势落库未启用（inbox.identity.trend_log=false）")
                return
            from src.web.identity_trend_store import configure_identity_trend_store
            _cfg_dir = Path(self.config.config_path).parent
            store = configure_identity_trend_store(
                enabled=True,
                db_path=_cfg_dir / "identity_trend.db",
                retention_days=float(_ident.get("trend_retention_days", 90)),
            )
            if store is not None:
                self.logger.info(
                    "✅ 会话身份趋势落库已就绪（retention=%sd）",
                    _ident.get("trend_retention_days", 90))
        except Exception:
            self.logger.warning("会话身份趋势落库初始化失败（已忽略）", exc_info=True)

    def _maybe_init_realtime_voice_trend_log(self) -> None:
        """E 线：按 ``realtime_voice.trend_log`` 装配实时语音按日聚合落库（默认关）。

        开启后 stats 热路旁路 upsert ``config/rtv_trend.db``，ops 经
        ``/api/admin/realtime-voice-trend`` 画 sparkline，告警校准可读近 N 天回放。
        """
        try:
            rtv = (self.config.config.get("realtime_voice") or {})
            if not rtv.get("trend_log", False):
                self.logger.info(
                    "实时语音趋势落库未启用（realtime_voice.trend_log=false）")
                return
            from src.ai.realtime_voice_trend_store import configure_realtime_voice_trend_store
            _cfg_dir = Path(self.config.config_path).parent
            store = configure_realtime_voice_trend_store(
                enabled=True,
                db_path=_cfg_dir / "rtv_trend.db",
                retention_days=float(rtv.get("trend_retention_days", 90)),
            )
            if store is not None:
                self.logger.info(
                    "✅ 实时语音趋势落库已就绪（retention=%sd）",
                    rtv.get("trend_retention_days", 90))
        except Exception:
            self.logger.warning("实时语音趋势落库初始化失败（已忽略）", exc_info=True)

    def _maybe_init_send_route_trend_log(self) -> None:
        """P8：按 ``inbox.send_route.trend_log`` 装配出站路由回落率按日聚合落库（默认关）。

        开启后 watchdog tick 旁路把 ``SendRouteStats`` 的累计增量 upsert
        ``config/send_route_trend.db``，ops 看板经 ``/api/admin/send-route-trend`` 画近 N 天
        回落率 sparkline。关闭时 ``sync_send_route_trend_from_stats`` 恒 no-op。
        """
        try:
            _sr = ((self.config.config.get("inbox") or {}).get("send_route") or {})
            if not _sr.get("trend_log", False):
                self.logger.info(
                    "出站路由趋势落库未启用（inbox.send_route.trend_log=false）")
                return
            from src.inbox.send_route_trend_store import configure_send_route_trend_store
            _cfg_dir = Path(self.config.config_path).parent
            store = configure_send_route_trend_store(
                enabled=True,
                db_path=_cfg_dir / "send_route_trend.db",
                retention_days=float(_sr.get("trend_retention_days", 90)),
            )
            if store is not None:
                self.logger.info(
                    "✅ 出站路由趋势落库已就绪（retention=%sd）",
                    _sr.get("trend_retention_days", 90))
        except Exception:
            self.logger.warning("出站路由趋势落库初始化失败（已忽略）", exc_info=True)

    def _maybe_init_monetization(self, *args, **kwargs):
        from src.bootstrap.background_tasks import maybe_init_monetization
        return maybe_init_monetization(self, *args, **kwargs)

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

    async def _maybe_start_proactive_care(self, *args, **kwargs):
        from src.bootstrap.background_tasks import maybe_start_proactive_care
        return await maybe_start_proactive_care(self, *args, **kwargs)

    async def _maybe_start_companion_proactive(self) -> None:
        from src.companion.proactive_topic import maybe_start_companion_proactive
        return await maybe_start_companion_proactive(self)

    async def _maybe_start_reactivation_loop(self, *args, **kwargs):
        from src.bootstrap.background_tasks import maybe_start_reactivation_loop
        return await maybe_start_reactivation_loop(self, *args, **kwargs)

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

    async def _warmup_embeddings(self, *args, **kwargs):
        from src.bootstrap.background_tasks import warmup_embeddings
        return await warmup_embeddings(self, *args, **kwargs)

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

    async def _episodic_backfill_periodic(self, *args, **kwargs):
        from src.bootstrap.background_tasks import episodic_backfill_periodic
        return await episodic_backfill_periodic(self, *args, **kwargs)

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
        """停止 AI 聊天助手(Stage4:实现已迁至 bootstrap/lifecycle)。"""
        from src.bootstrap.lifecycle import stop_assistant
        return await stop_assistant(self)

    def _setup_signal_handlers(self):
        """设置信号处理"""
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum, frame):
        """信号处理函数"""
        self.logger.info(f"收到信号 {signum}, 正在关闭...")
        asyncio.create_task(self.stop())


# CLI 入口 --check / --init 已抽取到 src/bootstrap/cli.py（2026-07-11 重构 Stage 1，行为不变）
from src.bootstrap.cli import run_config_check, run_init


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
