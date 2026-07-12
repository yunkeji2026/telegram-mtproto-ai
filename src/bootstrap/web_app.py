"""main.py web 管理后台的启动编排（Stage 2 拆分目标）。

2026-07-12 Stage 2 起，把 initialize() 内联的 FastAPI web 装配/启动逐簇迁到这里，
把闭包捕获的 self.* 显式化为 assistant 参数。首簇：web 服务线程启动。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
import threading
from typing import Any

from src.utils.net_helpers import is_bind_address_in_use_error


def start_web_server_thread(assistant: Any, server: Any, web_host: str, web_port: int) -> threading.Thread:
    """在独立线程 + 独立 event loop 里跑 uvicorn server，避免与主 loop 抢占。

    从 main.py 的 initialize() 原样抽出（行为不变）：主 loop 上的同步阻塞
    （SQLite 写、BM25 全表扫描）不再卡 web 请求。绑定失败只告警、不挡启动。
    """
    def _run_web_in_thread():
        try:
            web_loop = asyncio.new_event_loop()
            assistant._web_loop = web_loop
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
                assistant.logger.warning(
                    "Web 管理后台未启动: 端口 %s 已被占用（通常为先前未退出的本程序实例）。"
                    "请先结束占用进程: taskkill /F /IM python.exe 或修改 config.yaml 中 web_admin.port",
                    web_port,
                )
            else:
                assistant.logger.warning("Web 管理后台启动失败: %s", e)
        except Exception as ex:
            assistant.logger.warning("Web 管理后台启动跳过: %s", ex)

    web_thread = threading.Thread(
        target=_run_web_in_thread,
        name="web_admin_thread",
        daemon=True,
    )
    web_thread.start()
    return web_thread


def make_api_auth(web_app: Any):
    """构造 API 鉴权依赖：优先 admin 的 api_auth（登录校验 + 坐席白名单），
    回退 require_role('line_rpa')。参数带 Request 注解，避免 FastAPI 误判为 query 参数。

    从 initialize() 抽出并去重：原 _drafts_api_auth / _contacts_api_auth 逻辑一致。
    """
    from starlette.requests import Request

    def _api_auth(request: Request):
        _fn = getattr(web_app.state, "api_auth", None)
        if _fn is not None:
            _fn(request)
        elif hasattr(web_app.state, "require_role"):
            web_app.state.require_role(request, "line_rpa")

    return _api_auth


def start_monitoring_thread(assistant: Any):
    """按配置启动监控 API 后台线程（供前端对接）。从 initialize() 原样抽出（行为不变）。

    monitoring.enabled=false 时直接跳过。绑定失败只告警、不挡启动。返回线程或 None。
    """
    mon = getattr(assistant.config, "config", {}) or {}
    mon = mon.get("monitoring", {})
    if not mon.get("enabled", True):
        return None
    try:
        port = int(mon.get("metrics_port", 9090))
        from src.monitoring.server import run_server
        _web_cfg = assistant.config.config.get("web_admin", {})
        mon_token = mon.get("auth_token") or _web_cfg.get("auth_token", "")
        t = threading.Thread(
            target=run_server,
            kwargs={"host": "127.0.0.1", "port": port,
                    "assistant_ref": assistant, "auth_token": mon_token},
            daemon=True,
        )
        t.start()
        assistant._monitor_thread = t
        assistant.logger.info(
            "监控 API 线程已启动，正在绑定 127.0.0.1:%s（若端口被占用将在线程内失败，见日志）",
            port,
        )
        return t
    except Exception as ex:
        assistant.logger.warning(f"监控 API 启动跳过: {ex}")
        return None


def setup_web_app(assistant: Any, web_cfg: dict) -> None:
    """装配并启动 FastAPI web 管理后台(Stage5,从 main.py initialize() 原样迁出)。

    web_cfg=config.web_admin;web_admin.enabled=false 时整块跳过。创建 app、
    挂载各平台 service 到 app.state、起 web 线程,均 try/except 兜底不挡启动。"""
    if web_cfg.get("enabled"):
        try:
            import uvicorn
            from src.web.admin import create_app
            from src.utils.audit_store import AuditStore
            from src.utils.webhook import WebhookNotifier
            from src.utils.log_buffer import install_log_buffer
            _log_buf = install_log_buffer()
            cfg_dir = Path(assistant.config.config_path).parent
            wh_cfg = assistant.config.config.get("webhook", {})
            webhook = WebhookNotifier(wh_cfg) if wh_cfg.get("enabled") else None
            # W4-Cap-Alert：contacts 已 bootstrap 且 webhook 就绪 → 把 cap 阈值事件接上
            if assistant.contacts is not None and webhook is not None:
                try:
                    assistant.contacts.wire_cap_alert_webhook(webhook)
                except Exception:
                    assistant.logger.debug(
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
                    audit, getattr(assistant, "_startup_advisory_events", []) or []
                )
                if n:
                    assistant.logger.debug("已将 %s 条配置告警写入审计", n)
                try:
                    from src.monitoring.metrics_store import get_metrics_store

                    get_metrics_store().set_startup_advisory_audit_logged(n)
                except Exception:
                    assistant.logger.debug(
                        "startup_advisory audit metrics 跳过", exc_info=True
                    )
            except Exception:
                assistant.logger.debug("配置告警写入审计跳过", exc_info=True)
            _tc_for_web = assistant.telegram_client
            web_app = create_app(
                assistant.config, audit_store=audit,
                boot_ts=(_tc_for_web._boot_timestamp
                         if _tc_for_web is not None else 0),
                telegram_client=_tc_for_web,
                event_tracker=(_tc_for_web.event_tracker
                               if _tc_for_web is not None else None),
                log_buffer=_log_buf)
            assistant._web_app = web_app  # 供收件箱后台 ingest 轮询访问 state 上的各平台 service
            # 翻译/意图等服务的兜底路径（inbox 未启用时）需要 ai_client，
            # 否则 _get_translation_service 会建出无引擎的退化实例。
            if getattr(assistant, "ai_client", None) is not None:
                web_app.state.ai_client = assistant.ai_client
            if assistant.line_rpa_service is not None:
                web_app.state.line_rpa_service = assistant.line_rpa_service
            web_app.state.line_rpa_services = assistant.line_rpa_services
            if assistant.messenger_rpa_service is not None:
                web_app.state.messenger_rpa_service = assistant.messenger_rpa_service
            if assistant.whatsapp_rpa_service is not None:
                web_app.state.whatsapp_rpa_service = assistant.whatsapp_rpa_service
            web_app.state.whatsapp_rpa_services = assistant.whatsapp_rpa_services
            if assistant.device_coordinator_service is not None:
                web_app.state.device_coordinator_service = assistant.device_coordinator_service
            if assistant.hotplug_watcher is not None:
                web_app.state.hotplug_watcher = assistant.hotplug_watcher
            if assistant.local_tts is not None:
                web_app.state.local_tts_supervisor = assistant.local_tts

            # ── G1 全局 Kill-Switch：初始化单例（回填持久化的冻结态，重启不丢）──
            try:
                from src.ops.kill_switch import get_kill_switch
                _cfg_dir0 = Path(assistant.config.config_path).parent
                _ks_cfg = ((assistant.config.config or {}).get("ops") or {}).get("kill_switch") or {}
                _ks_db = Path(_ks_cfg.get("db_path") or (_cfg_dir0 / "runtime_flags.db"))
                if not _ks_db.is_absolute():
                    _ks_db = _cfg_dir0 / _ks_db
                _ks = get_kill_switch(_ks_db)
                web_app.state.kill_switch = _ks
                _active = _ks.status()
                if _active:
                    assistant.logger.warning(
                        "🛑 Kill-Switch 启动即生效（重启回填）：%s",
                        [i["scope"] for i in _active])
                else:
                    assistant.logger.info("Kill-Switch 已就绪（%s）", _ks_db)
            except Exception:
                assistant.logger.warning("Kill-Switch 初始化跳过", exc_info=True)

            # ── 统一收件箱持久层（Phase A：纯旁路，store 故障/为空自动回落） ──
            try:
                _inbox_cfg = (assistant.config.config or {}).get("inbox", {}) or {}
                if _inbox_cfg.get("enabled", True):
                    from src.inbox.store import InboxStore

                    _cfg_dir = Path(assistant.config.config_path).parent
                    _inbox_db = Path(_inbox_cfg.get("db_path") or (_cfg_dir / "inbox.db"))
                    if not _inbox_db.is_absolute():
                        _inbox_db = _cfg_dir / _inbox_db
                    assistant.inbox_store = InboxStore(_inbox_db)
                    web_app.state.inbox_store = assistant.inbox_store
                    assistant.logger.info("统一收件箱持久层已挂载（%s）", _inbox_db)

                    # ── Phase B：统一草稿/审批层（read-through 聚合 4 平台源表） ──
                    from src.inbox.drafts import DraftService
                    from src.web.routes.drafts_routes import register_drafts_routes
                    from src.ai.chat_assistant_service import quick_risk as _quick_risk

                    draft_svc = DraftService(
                        inbox_store=assistant.inbox_store,
                        line_services=assistant.line_rpa_services or [],
                        wa_services=assistant.whatsapp_rpa_services or [],
                        messenger_service=assistant.messenger_rpa_service,
                        risk_fn=_quick_risk,
                    )
                    web_app.state.draft_service = draft_svc

                    from src.bootstrap.web_app import make_api_auth
                    _drafts_api_auth = make_api_auth(web_app)

                    register_drafts_routes(web_app, api_auth=_drafts_api_auth)
                    assistant.logger.info("统一草稿层已挂载（/api/drafts）")

                    # ── Phase A：L2 草稿自动发送后台 worker ──
                    try:
                        from src.inbox.autosend_worker import AutosendWorker
                        _as_cfg = (assistant.config.config or {}).get(
                            "inbox", {}
                        ).get("l2_autosend", {}) or {}
                        if _as_cfg.get("enabled", True):
                            # H3：合并 auto_draft 清理配置到 worker cfg
                            _ad_cleanup = (assistant.config.config or {}).get(
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
                            _send_cb, _translate_cb = build_autosend_callbacks(assistant, web_app, _deliver)
                            _as_worker = AutosendWorker(
                                draft_service=draft_svc,
                                config=_merged_as_cfg,
                                send_callback=_send_cb,
                                translate_callback=_translate_cb,
                            )
                            web_app.state.autosend_worker = _as_worker
                            # C3：注册 L2 事件驱动钩子，新草稿落库时立即唤醒
                            assistant.inbox_store.register_l2_callback(
                                _as_worker.notify_new_l2
                            )
                            asyncio.ensure_future(_as_worker.run())
                            assistant.logger.info(
                                "AutosendWorker 已启动（min=%ss max=%ss deliver=%s）",
                                _as_cfg.get("min_interval_sec", 60),
                                _as_cfg.get("max_interval_sec", 600),
                                _deliver,
                            )
                    except Exception:
                        assistant.logger.debug("AutosendWorker 启动跳过", exc_info=True)

                    # ── K1+K2：SLAWatcher 草稿 SLA 预警 + 自动再分配 ──
                    try:
                        from src.inbox.sla_watcher import SLAWatcher
                        _sw_cfg = (assistant.config.config or {}).get(
                            "inbox", {}
                        ).get("sla_watcher", {}) or {}
                        if _sw_cfg.get("enabled", True):
                            _sw = SLAWatcher(
                                draft_service=draft_svc,
                                inbox_store=assistant.inbox_store,
                                config=_sw_cfg,
                            )
                            web_app.state.sla_watcher = _sw
                            asyncio.ensure_future(_sw.run())
                            assistant.logger.info(
                                "SLAWatcher 已启动（sla=%.0fh tick=%.0fs absent=%.0fs）",
                                float(_sw_cfg.get("sla_hours", 4)),
                                float(_sw_cfg.get("tick_sec", 60)),
                                float(_sw_cfg.get("absent_sec", 300)),
                            )
                    except Exception:
                        assistant.logger.debug("SLAWatcher 启动跳过", exc_info=True)

                    # ── P3：AutoClaimWorker auto_assign 自动认领执行端 ──
                    # 默认关（workspace.auto_assign.auto_claim.enabled=false）；
                    # worker 每 tick 重读配置，开关无需重启。仅在 inbox 可用时启。
                    try:
                        from src.workspace.auto_claim_worker import AutoClaimWorker
                        _ac_cfg = (((assistant.config.config or {}).get(
                            "workspace", {}) or {}).get(
                            "auto_assign", {}) or {}).get("auto_claim", {}) or {}
                        _acw = AutoClaimWorker(
                            inbox_store=assistant.inbox_store,
                            config_manager=assistant.config,
                            config=_ac_cfg,
                        )
                        web_app.state.auto_claim_worker = _acw
                        asyncio.ensure_future(_acw.run())
                        assistant.logger.info(
                            "AutoClaimWorker 已启动（默认关，按 auto_claim.enabled 热生效）")
                    except Exception:
                        assistant.logger.debug("AutoClaimWorker 启动跳过", exc_info=True)

                    # ── 入站翻译存量消化（低频巡检，默认关）─────────
                    # workspace.auto_translate_inbound.backfill.enabled=true 开启；
                    # 闲时把老会话未译存量提前译好落库，坐席首开即毫秒级+译文备好。
                    # 复用 enrich 同一套判定/防重/负缓存（会话级锁与在线路径互斥）。
                    try:
                        from src.workspace.inbound_backfill import (
                            InboundXlateBackfillWorker,
                        )
                        _bfw = InboundXlateBackfillWorker(
                            inbox_store=assistant.inbox_store,
                            config_manager=assistant.config,
                            translation_svc_getter=lambda: getattr(
                                web_app.state, "translation_service", None),
                        )
                        web_app.state.inbound_backfill_worker = _bfw
                        asyncio.ensure_future(_bfw.run())
                        assistant.logger.info(
                            "InboundXlateBackfill 已启动（默认关，按 backfill.enabled 热生效）")
                    except Exception:
                        assistant.logger.debug("InboundXlateBackfill 启动跳过", exc_info=True)

                    # ── L2：WebhookNotifier 企业 IM 通知 ──────────────
                    try:
                        from src.inbox.webhook_notifier import WebhookNotifier
                        # 有效列表：notify_webhooks.json 覆盖层优先，否则 config.yaml
                        try:
                            from src.integrations.notify_webhooks_store import (
                                effective_webhooks,
                            )
                            _wh_list = effective_webhooks(assistant.config.config or {})
                        except Exception:
                            _wh_list = (assistant.config.config or {}).get(
                                "notify", {}
                            ).get("webhooks", []) or []
                        # 即使当前为空也创建 notifier：便于后台「告警渠道」面板
                        # 运行时 reload() 增删，免重启
                        _whn = WebhookNotifier(config=_wh_list)
                        web_app.state.webhook_notifier = _whn
                        asyncio.ensure_future(_whn.run())
                        assistant.logger.info(
                            "WebhookNotifier 已启动（%d 个 webhook）",
                            len(_wh_list),
                        )
                    except Exception:
                        assistant.logger.debug("WebhookNotifier 启动跳过", exc_info=True)

                    # ── D3：HealthWatchdog 运行时健康主动告警 ─────────
                    # 默认开；周期巡检 D1 健康，异常经 EventBus→WebhookNotifier
                    # 推送（需在「告警渠道」订阅 health_alert 事件才会真正发出）。
                    try:
                        from src.inbox.health_watchdog import HealthWatchdog
                        _hw_cfg = (assistant.config.config or {}).get(
                            "health_watchdog", {}
                        ) or {}
                        if _hw_cfg.get("enabled", True):
                            _hw = HealthWatchdog(
                                app=web_app,
                                config_manager=assistant.config,
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
                            assistant.logger.info(
                                "HealthWatchdog 已启动（interval=%ss alert_on_warn=%s）",
                                _hw_cfg.get("interval_sec", 300),
                                _hw_cfg.get("alert_on_warn", False),
                            )
                    except Exception:
                        assistant.logger.debug("HealthWatchdog 启动跳过", exc_info=True)

                    # ── N2：ScheduledReporter 定时简报推送 ─────────────
                    try:
                        from src.inbox.scheduled_reporter import ScheduledReporter
                        _rpt_cfg = (assistant.config.config or {}).get(
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
                            assistant.logger.info(
                                "ScheduledReporter 已启动（daily=%s weekly=%s）",
                                _rpt_cfg.get("daily_time", "09:00"),
                                _rpt_cfg.get("weekly_day") or "禁用",
                            )
                    except Exception:
                        assistant.logger.debug("ScheduledReporter 启动跳过", exc_info=True)

                    # E2/F2：按 auto_draft 配置注册入站新消息 → 自动草稿生成回调
                    from src.inbox.autodraft_helpers import setup_auto_draft
                    setup_auto_draft(assistant, draft_svc, web_app)

                    # I3：预置回复模板库（幂等，id 冲突则跳过）
                    try:
                        from src.inbox.template_seeds import SEED_TEMPLATES
                        _seeded = assistant.inbox_store.seed_templates(SEED_TEMPLATES)
                        if _seeded > 0:
                            assistant.logger.info("模板库已预置 %d 条种子模板", _seeded)
                    except Exception:
                        assistant.logger.debug("模板库预置跳过", exc_info=True)

                    # ── Phase C：意图 LLM 升级 + 翻译记忆持久化（预置带依赖的 service） ──
                    _cfg_root = assistant.config.config or {}
                    _ia_cfg = _cfg_root.get("intent_analysis", {}) or {}
                    _tr_cfg = _cfg_root.get("translation", {}) or {}
                    from src.ai.chat_assistant_service import ChatAssistantService
                    web_app.state.chat_assistant_service = ChatAssistantService(
                        ai_client=assistant.ai_client,
                        use_llm=bool(_ia_cfg.get("use_llm", False)),
                        analysis_store=assistant.inbox_store,
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
                        assistant.translation_memory = _tm_store
                    # P56：术语库（全局+域包合并）+ 多引擎路由
                    from src.ai.translation_glossary import build_glossary
                    from src.ai.translation_engines import build_engines
                    _domain_files = []
                    try:
                        _dom_dir = Path(assistant.config.config_path).parent.parent / "domains"
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
                    _engines = build_engines(_tr_cfg, assistant.ai_client)
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
                            and assistant.ai_client is not None
                            and hasattr(assistant.ai_client, "embed")):
                        _sem_fn = assistant.ai_client.embed
                    # 存重建上下文，供 /api/workspace/glossary 热更新复用
                    web_app.state.glossary_store = _gloss_store
                    web_app.state.glossary_config = _cfg_root
                    web_app.state.glossary_domain_files = _domain_files
                    from src.ai.translation_service import TranslationService
                    web_app.state.translation_service = TranslationService(
                        ai_client=assistant.ai_client,
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
                    assistant.logger.info(
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
                                assistant.logger.info("统计语种检测已启用（回退精修含糊拉丁）")
                            else:
                                assistant.logger.warning(
                                    "translation.lang_detect.statistical.enabled=true 但未装 lingua/langdetect，已跳过"
                                )
                    except Exception:
                        assistant.logger.debug("统计语种检测装配跳过", exc_info=True)

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
                        assistant.ecommerce_tools = EcommerceToolService(
                            _ec_conn, audit_store=audit,
                            timeout_sec=float(_ec_cfg.get("timeout_sec", 8) or 8),
                            cache_ttl_sec=float(_ec_cfg.get("cache_ttl_sec", 0) or 0),
                            cache_max_entries=int(_ec_cfg.get("cache_max_entries", 512) or 512),
                            logistics_connector=_logi_conn,
                        )
                        web_app.state.ecommerce_tools = assistant.ecommerce_tools
                        register_ecommerce_tools_routes(
                            web_app, api_auth=_drafts_api_auth,
                        )
                        # P1-b：注入回复生成链路 → 命中订单号自动带真实事实/反幻觉守卫
                        if assistant.ai_client is not None:
                            assistant.ai_client.set_ecommerce_tools(assistant.ecommerce_tools)
                        assistant.logger.info(
                            "电商工具层已挂载（provider=%s, /api/tools/ecommerce/* + 回复事实注入）",
                            assistant.ecommerce_tools.connector_name,
                        )
            except Exception:
                assistant.logger.warning("统一收件箱持久层挂载跳过", exc_info=True)

            # ── 挂载 Contacts 路由（仅 contacts 子系统启用时） ──
            if assistant.contacts is not None:
                try:
                    from src.web.routes.contacts_routes import (
                        register_contacts_routes,
                    )

                    from src.bootstrap.web_app import make_api_auth
                    _contacts_api_auth = make_api_auth(web_app)

                    register_contacts_routes(
                        web_app,
                        api_auth=_contacts_api_auth,
                        contacts_store=assistant.contacts.store,
                        merge_service=assistant.contacts.merge_svc,
                        audit_store=audit,
                        intimacy_engine=assistant.contacts.intimacy_engine,
                        reactivation_scheduler=assistant.contacts.reactivation,
                        eval_scheduler=getattr(
                            assistant.contacts, "draft_eval_scheduler", None,
                        ),
                        gateway=assistant.contacts.gateway,
                        account_limiter=assistant.contacts.limiter,
                        mobile_bridge=assistant.mobile_bridge,
                        fire_webhook=getattr(
                            web_app.state, "fire_webhook", None,
                        ),
                        ai_client=assistant.ai_client,
                    )
                    # 让 web 能通过 state 直接访问
                    web_app.state.contacts = assistant.contacts
                    assistant.logger.info("Contacts Web 路由已注册（/api/contacts /ops/contacts）")
                except Exception:
                    assistant.logger.warning(
                        "Contacts 路由注册跳过", exc_info=True)
                # 把 state_store 也挂上，路由能直接读 approvals
                try:
                    web_app.state.messenger_rpa_state_store = (
                        assistant.messenger_rpa_service.state_store
                    )
                except Exception:
                    assistant.logger.debug(
                        "messenger_rpa state_store 注入跳过", exc_info=True
                    )
            # ★ P1-2：Suggest More 端点需要 SkillManager
            if assistant.skill_manager is not None:
                web_app.state.skill_manager = assistant.skill_manager
            web_port = int(web_cfg.get("port", 8080))
            web_host = web_cfg.get("host", "127.0.0.1")
            uvi_config = uvicorn.Config(web_app, host=web_host, port=web_port, log_level="warning")
            server = uvicorn.Server(uvi_config)
            assistant._web_server = server

            # ★ 隔离 web 到独立线程 + 独立 event loop（Stage 2：启动逻辑抽到
            # src/bootstrap/web_app.py::start_web_server_thread，行为不变）
            from src.bootstrap.web_app import start_web_server_thread
            assistant._web_thread = start_web_server_thread(assistant, server, web_host, web_port)
            assistant.logger.info(
                "Web 管理后台正在绑定 http://%s:%s（独立线程隔离，避免抢占主 event loop）",
                web_host,
                web_port,
            )
        except Exception as ex:
            assistant.logger.warning("Web 管理后台启动跳过: %s", ex)
