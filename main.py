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

# 添加项目根目录到Python路径
sys.path.insert(0, str(Path(__file__).parent))

from src.client.telegram_client import TelegramClient
from src.ai.ai_client import AIClient
from src.skills.skill_manager import SkillManager
from src.utils.config_manager import ConfigManager
from src.utils.logger import setup_logger
from src.utils.net_helpers import is_bind_address_in_use_error
from src.utils.domain_policy import effective_domain_name


class AIChatAssistant:
    """AI聊天助手主类"""
    
    def __init__(self):
        """初始化AI聊天助手"""
        self.config = None
        self.telegram_client = None
        self.ai_client = None
        self.skill_manager = None
        self.logger = None
        self.running = False
        self.line_rpa_service = None
        self.messenger_rpa_service = None
        # W2-W4：跨平台 Contacts 子系统（feature flag 控制；默认关）
        self.contacts = None  # type: Optional["ContactsSubsystem"]  # noqa: F821
        self._telegram_task = None
        
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
            
            # 5. 初始化Telegram客户端（传入 ai_client 用于「前一条+当前消息」上下文判断是否回复）
            self.telegram_client = TelegramClient(
                config=self.config,
                skill_manager=self.skill_manager,
                ai_client=self.ai_client
            )
            await self.telegram_client.initialize()
            self.logger.info("Telegram客户端初始化成功")
            
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

            # 个人 LINE RPA 服务（可选；主进程托管循环）
            try:
                _line_rpa_cfg = self.config.get_line_rpa_config() or {}
                if isinstance(_line_rpa_cfg, dict) and _line_rpa_cfg.get("enabled"):
                    from src.integrations.line_rpa.service import LineRpaService
                    self.line_rpa_service = LineRpaService(
                        config_manager=self.config,
                        skill_manager=self.skill_manager,
                        line_rpa_cfg=_line_rpa_cfg,
                    )
                    self.logger.info("LINE RPA 服务已构建（autostart 将在 start() 中决定）")
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
            except Exception as ex:
                self.logger.warning("Contacts 子系统启动跳过: %s", ex)

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
                    if self.messenger_rpa_service is not None:
                        web_app.state.messenger_rpa_service = self.messenger_rpa_service

                    # ── 挂载 Contacts 路由（仅 contacts 子系统启用时） ──
                    if self.contacts is not None:
                        try:
                            from src.web.routes.contacts_routes import (
                                register_contacts_routes,
                            )

                            def _contacts_api_auth(request):
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
                                gateway=self.contacts.gateway,
                                account_limiter=self.contacts.limiter,
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

                    async def _serve_web_panel():
                        try:
                            await server.serve()
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

                    asyncio.get_running_loop().create_task(_serve_web_panel())
                    self.logger.info(
                        "Web 管理后台正在绑定 http://%s:%s（若端口被占用将跳过并仅记录警告）",
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
                # 给 telegram 几秒完成登录、打印 "✅ Telegram客户端已启动，等待消息..."
                # 超时不阻塞后续启动，只记 warning
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

                if self.line_rpa_service is not None:
                    try:
                        started = await self.line_rpa_service.start()
                        if started:
                            self.logger.info("✅ LINE RPA 后台循环已启动")
                        else:
                            self.logger.info("LINE RPA 后台循环未自动启动（见配置）")
                    except Exception as ex:
                        self.logger.warning("LINE RPA 启动跳过: %s", ex)

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

                asyncio.create_task(self._warmup_embeddings(), name="kb_warmup_embeddings")
                asyncio.create_task(
                    self._episodic_backfill_on_startup(), name="episodic_backfill_startup"
                )
                asyncio.create_task(
                    self._episodic_backfill_periodic(), name="episodic_backfill_periodic"
                )
                asyncio.create_task(self._periodic_self_heal(), name="kb_periodic_self_heal")
                asyncio.create_task(self._periodic_daily_learn(), name="daily_learner")

                # 保持运行直到收到停止信号
                while self.running:
                    await asyncio.sleep(1)
                    
            except KeyboardInterrupt:
                self.logger.info("收到中断信号，正在关闭...")
            except Exception as e:
                self.logger.error(f"运行错误: {e}")
            finally:
                await self.stop()
    
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
            
            if self.line_rpa_service is not None:
                try:
                    await self.line_rpa_service.stop()
                    self.logger.info("LINE RPA 后台循环已停止")
                except Exception as ex:
                    self.logger.warning("LINE RPA 停止异常: %s", ex)

            if self.messenger_rpa_service is not None:
                try:
                    await self.messenger_rpa_service.stop()
                    self.logger.info("Messenger RPA 后台循环已停止")
                except Exception as ex:
                    self.logger.warning("Messenger RPA 停止异常: %s", ex)

            if self.telegram_client:
                await self.telegram_client.stop()
            
            if self.skill_manager:
                await self.skill_manager.cleanup()
            
            if self.ai_client:
                await self.ai_client.cleanup()
            
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