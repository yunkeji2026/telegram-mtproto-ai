"""统一收件箱——账号池编排器 / 协议入站 / 自动回复路由域（巨石拆分 slice 10）。

把"账号池编排器（M5：多账号 7×24 在线）+ 协议入站桥 + 账号自动回复管理"这一子域，
从 ``register_unified_inbox_routes`` 巨型闭包中整体外移为
``register_account_routes(app, *, api_auth, config_manager)``，由主 register 在**原位置**
顺序调用，以保持以下时序不变：

- ``_register_protocol_sink()`` / ``_register_protocol_autoreply()``：**register 时立即调用**
  的副作用（向 protocol_bridge 注册入站 sink / 自动回复 hook），随子注册函数在挂载时执行。
- ``@app.on_event("startup")`` ``_orchestrator_autostart``：**startup 钩子**，装饰器在挂载时
  注册到 app，启动时机不变。

端点路径/方法/响应零变化（admin_route_inventory URL 契约守卫 + 编排器/自动回复专项兜底）。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List

from fastapi import HTTPException, Request

from src.inbox.channel_adapters import status_via_adapters
from src.integrations.account_orchestrator import (
    account_key as _acct_key,
    ensure_builtin_workers,
    get_orchestrator,
    orchestrator_enabled,
)
from src.integrations.account_registry import get_account_registry
from src.web.routes.unified_inbox_aggregate import _INBOX_ADAPTERS
from src.web.routes.unified_inbox_auth import _session_agent

logger = logging.getLogger(__name__)


def register_account_routes(app, *, api_auth, config_manager=None) -> None:
    """挂载账号池编排器 / 协议入站 / 自动回复相关端点（/api/accounts*、/api/internal/protocol/ingest）。"""

    # ── 账号池编排器（M5：多账号 7×24 在线，默认关） ────────────────────────
    def _orch():
        cfg = (config_manager.config if config_manager is not None else {}) or {}
        ensure_builtin_workers(cfg)
        return get_orchestrator(cfg)

    # M6①：注册 protocol→收件箱 入站 sink（worker 收到消息时落库；store 在 emit 时惰性取）
    def _register_protocol_sink() -> None:
        try:
            from src.integrations.protocol_bridge import (
                ingest_incoming, register_inbox_sink, register_inbox_store_getter,
            )

            def _sink(m: Dict[str, Any]) -> None:
                store = getattr(app.state, "inbox_store", None)
                if store is None:
                    return
                ingest_incoming(store, **m)

            register_inbox_sink(_sink)
            # 供官方 webhook 的 auto_ai 让位护栏只读查 automation_mode（System Z 去重）
            register_inbox_store_getter(
                lambda: getattr(app.state, "inbox_store", None))
        except Exception:
            logger.debug("注册 protocol 收件箱 sink 失败", exc_info=True)

    _register_protocol_sink()

    # Phase 3：注册 protocol 自动回复 hook（hook 内自带双闸门，恒注册、运行时按需生效）
    def _register_protocol_autoreply() -> None:
        try:
            from src.integrations.protocol_autoreply import build_reply_hook
            from src.integrations.protocol_bridge import register_reply_hook
            register_reply_hook(build_reply_hook(app))
        except Exception:
            logger.debug("注册 protocol 自动回复 hook 失败", exc_info=True)

    _register_protocol_autoreply()

    @app.post("/api/internal/protocol/ingest")
    async def api_protocol_ingest(request: Request):
        """内部入站桥：Baileys(Node) 等外部 worker 把收到的消息 push 进统一收件箱。"""
        api_auth(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        store = getattr(request.app.state, "inbox_store", None)
        if store is None:
            raise HTTPException(503, "inbox store 未就绪")
        from src.integrations.protocol_bridge import (
            ingest_incoming, make_message, maybe_auto_reply,
        )
        if not str((body or {}).get("chat_key") or ""):
            raise HTTPException(400, "chat_key 不能为空")
        direction = str((body or {}).get("direction") or "in")
        cid = ingest_incoming(
            store,
            platform=str((body or {}).get("platform") or ""),
            account_id=str((body or {}).get("account_id") or ""),
            chat_key=str((body or {}).get("chat_key") or ""),
            name=str((body or {}).get("name") or ""),
            text=str((body or {}).get("text") or ""),
            ts=float((body or {}).get("ts") or 0),
            msg_id=str((body or {}).get("msg_id") or ""),
            direction=direction,
            media_type=str((body or {}).get("media_type") or ""),
            media_ref=str((body or {}).get("media_ref") or ""),
        )
        if direction == "in":
            await maybe_auto_reply(make_message(
                platform=str((body or {}).get("platform") or ""),
                account_id=str((body or {}).get("account_id") or ""),
                chat_key=str((body or {}).get("chat_key") or ""),
                name=str((body or {}).get("name") or ""),
                text=str((body or {}).get("text") or ""),
                ts=float((body or {}).get("ts") or 0),
                msg_id=str((body or {}).get("msg_id") or ""),
            ))
        return {"ok": bool(cid), "conversation_id": cid or ""}

    def _collect_config_accounts(cfg: Dict[str, Any]) -> List[tuple]:
        """从 config.yaml 抽取各平台声明的账号 → (platform, account_id, mode, label)。

        覆盖 telegram.accounts[]（+ 扁平单号）与 line/messenger/whatsapp_rpa.accounts[]
        （+ 扁平 enabled 单号）。形态各异，全程防御式读取。
        """
        out: List[tuple] = []
        tg = cfg.get("telegram") or {}
        if isinstance(tg, dict):
            if tg.get("api_id") or tg.get("session_name"):
                out.append(("telegram", str(tg.get("session_name") or "default"),
                            "protocol", str(tg.get("label") or "Telegram")))
            for a in tg.get("accounts") or []:
                if not isinstance(a, dict):
                    continue
                aid = str(a.get("id") or a.get("session_name") or "")
                if aid:
                    out.append(("telegram", aid, "protocol", str(a.get("label") or aid)))
        for plat, key in (("line", "line_rpa"), ("messenger", "messenger_rpa"),
                          ("whatsapp", "whatsapp_rpa")):
            block = cfg.get(key) or {}
            if not isinstance(block, dict):
                continue
            accs = block.get("accounts") or []
            if isinstance(accs, list) and accs:
                for a in accs:
                    if not isinstance(a, dict):
                        continue
                    aid = str(a.get("account_id") or a.get("id") or "")
                    if aid:
                        out.append((plat, aid, "device", str(a.get("label") or aid)))
            elif block.get("enabled"):
                aid = str(block.get("account_id") or f"{plat}_default")
                out.append((plat, aid, "device", str(block.get("label") or aid)))
        return out

    @app.get("/api/accounts")
    async def api_accounts_list(request: Request):
        """统一账号清单（Phase 2）：合并 registry + config.yaml + 运行时健康。

        让桌面端 / web 后台用同一份数据渲染「账号管理」面板，不必再拼 4 个接口。
        返回每个账号的 ``platform/account_id/mode/label/status/running/proxy_id/
        fingerprint_id/sources``（sources 标出来源：registry/config/runtime）。
        """
        api_auth(request)
        cfg = (config_manager.config if config_manager is not None else {}) or {}
        merged: Dict[tuple, Dict[str, Any]] = {}

        def _ensure(platform: str, account_id: str) -> Dict[str, Any]:
            k = (str(platform or "").lower(), str(account_id or ""))
            if k not in merged:
                merged[k] = {
                    "platform": k[0], "account_id": k[1], "mode": "",
                    "label": "", "status": "unknown", "running": False,
                    "proxy_id": "", "fingerprint_id": "", "auto_reply": False,
                    "auto_reply_override": {}, "sources": [],
                }
            return merged[k]

        # 1) 注册表（登录/编排/桌面 ingest 落库的权威账号）
        try:
            for row in get_account_registry().list():
                r = _ensure(row.get("platform"), row.get("account_id"))
                r["mode"] = row.get("mode") or r["mode"]
                r["label"] = row.get("label") or r["label"]
                r["status"] = row.get("status") or r["status"]
                r["proxy_id"] = row.get("proxy_id") or r["proxy_id"]
                r["fingerprint_id"] = row.get("fingerprint_id") or r["fingerprint_id"]
                meta = row.get("meta") or {}
                r["auto_reply"] = bool(meta.get("auto_reply"))
                r["auto_reply_override"] = dict(meta.get("autoreply_override") or {})
                if "registry" not in r["sources"]:
                    r["sources"].append("registry")
        except Exception:
            logger.debug("[accounts] registry 读取失败", exc_info=True)

        # 2) config.yaml 声明的账号（boot 时拉起的 RPA / 协议号）
        for platform, account_id, mode, label in _collect_config_accounts(cfg):
            r = _ensure(platform, account_id)
            if not r["mode"]:
                r["mode"] = mode
            if not r["label"]:
                r["label"] = label
            if "config" not in r["sources"]:
                r["sources"].append("config")

        # 3) 运行时健康（适配器在线状态）
        try:
            status_map = status_via_adapters(request, _INBOX_ADAPTERS)
            for k, v in (status_map or {}).items():
                if not isinstance(v, dict):
                    continue
                platform = v.get("platform")
                account_id = v.get("account_id") or k
                if not platform:
                    continue
                r = _ensure(platform, account_id)
                r["running"] = bool(v.get("running"))
                if v.get("running"):
                    r["status"] = "online"
                if "runtime" not in r["sources"]:
                    r["sources"].append("runtime")
        except Exception:
            logger.debug("[accounts] 运行时状态读取失败", exc_info=True)

        # 3.5) 账号池编排器 managed 状态（N4：protocol/official worker 不经 inbox 适配器，
        #       须单独并入，否则扫码登入并被编排器拉起的协议号会显示为「未在线」）
        try:
            for oa in (_orch().status().get("accounts") or []):
                platform = oa.get("platform")
                account_id = oa.get("account_id")
                if not platform or not account_id:
                    continue
                r = _ensure(platform, account_id)
                if not r["mode"]:
                    r["mode"] = oa.get("mode") or ""
                if oa.get("state") == "running":
                    r["running"] = True
                    r["status"] = "online"
                if "orchestrator" not in r["sources"]:
                    r["sources"].append("orchestrator")
        except Exception:
            logger.debug("[accounts] 编排器状态读取失败", exc_info=True)

        # 4) 自动回复配额/熔断快照（Phase 5，仅协议号或已开自动回复的号）
        try:
            from src.integrations.protocol_autoreply_limits import (
                get_autoreply_limiter,
            )
            from src.integrations.protocol_autoreply_settings import (
                cfg_with_settings,
            )
            lim = get_autoreply_limiter(cfg_with_settings(cfg))
            for a in merged.values():
                if a.get("auto_reply") or a.get("mode") == "protocol":
                    ov_rate = (a.get("auto_reply_override") or {}).get("rate") or {}
                    a["auto_reply_quota"] = lim.snapshot(
                        f"{a['platform']}:{a['account_id']}",
                        hourly=ov_rate.get("hourly"), daily=ov_rate.get("daily"))
        except Exception:
            logger.debug("[accounts] 配额快照读取失败", exc_info=True)

        accounts = sorted(merged.values(),
                          key=lambda x: (x["platform"], x["account_id"]))
        return {"ok": True, "accounts": accounts, "count": len(accounts)}

    @app.get("/api/accounts/orchestrator")
    async def api_orchestrator_status(request: Request):
        api_auth(request)
        return {"ok": True, **_orch().status()}

    @app.get("/api/accounts/fleet-health")
    async def api_accounts_fleet_health(request: Request):
        """N 线 N6：机群反封号健康灯 + 生命周期分布（云端多开运维总览）。

        信号源统一：注册表(天龄/代理/状态) + 自动回复限额计数(今日发送/熔断)，
        经 companion_send_gate.aggregate_fleet(M7) 汇成总体灯色 + 每号建议上限/原因，
        并按 pending/warming/active/restricted/banned/offline 统计生命周期分布。
        """
        api_auth(request)
        cfg = (config_manager.config if config_manager is not None else {}) or {}
        from src.skills.account_signals import fleet_overview
        from src.integrations.protocol_autoreply_limits import get_autoreply_limiter
        from src.integrations.protocol_autoreply_settings import cfg_with_settings
        reg = get_account_registry()
        try:
            lim = get_autoreply_limiter(cfg_with_settings(cfg))
        except Exception:
            lim = None
        accounts = [
            (r.get("platform"), r.get("account_id"), r.get("status", ""))
            for r in reg.list()
        ]
        overview = fleet_overview(
            accounts, registry=reg, limiter=lim, config=cfg
        )
        return {"ok": True, **overview}

    @app.get("/api/accounts/protocol/readiness")
    async def api_protocol_readiness(request: Request):
        """协议栈联调自检：配置/依赖/服务可达性/编排器/入站 sink 的结构化就绪报告。"""
        api_auth(request)
        cfg = (config_manager.config if config_manager is not None else {}) or {}
        from src.integrations.protocol_diagnostics import readiness
        report = await readiness(cfg)
        return {"ok": True, **report}

    @app.post("/api/accounts/orchestrator/sync")
    async def api_orchestrator_sync(request: Request):
        api_auth(request)
        orch = _orch()
        await orch.sync()
        await orch.tick()
        return {"ok": True, **orch.status()}

    @app.post("/api/accounts/{platform}/{account_id}/start")
    async def api_account_start(platform: str, account_id: str, request: Request):
        api_auth(request)
        orch = _orch()
        acc = get_account_registry().get(platform, account_id) or {
            "platform": platform, "account_id": account_id}
        ok = await orch.start_account(acc)
        return {"ok": ok}

    @app.post("/api/accounts/{platform}/{account_id}/stop")
    async def api_account_stop(platform: str, account_id: str, request: Request):
        api_auth(request)
        await _orch().stop_account(_acct_key(platform, account_id))
        return {"ok": True}

    @app.post("/api/accounts/{platform}/{account_id}/restart")
    async def api_account_restart(platform: str, account_id: str, request: Request):
        api_auth(request)
        ok = await _orch().restart_account(_acct_key(platform, account_id))
        return {"ok": ok}

    @app.post("/api/accounts/{platform}/{account_id}/label")
    async def api_account_set_label(
        platform: str, account_id: str, request: Request,
    ):
        """P2：给账号起人格名（落 registry.label，权威人格名来源）。

        body: {label: str}。空字符串=清除别名（回落显示 account_id）。
        对 config 声明但还没进注册表的号（如 A 线 default）会建一条承载 label 的
        记录；**不传 mode**（upsert 默认 device，telegram device 无 worker factory，
        不会被编排器拉起 → 不会触发重复连接 / database lock）。
        """
        api_auth(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        label = str((body or {}).get("label") or "").strip()[:40]
        reg = get_account_registry()
        row = reg.get(platform, account_id)
        if row is None:
            reg.upsert(platform, account_id, label=label, status="pending")
        else:
            reg.upsert(platform, account_id, label=label)
        return {"ok": True, "platform": platform,
                "account_id": account_id, "label": label}

    @app.post("/api/accounts/{platform}/{account_id}/auto-reply")
    async def api_account_auto_reply(platform: str, account_id: str, request: Request):
        """切换某协议账号的 7×24 自动回复（账号闸门，写入 registry meta.auto_reply）。

        注意这是「账号闸门」；真正自动发还需全局 ``config.protocol_autoreply.enabled``
        同时打开（双闸门）。body: {enabled: bool}
        """
        api_auth(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        enabled = bool((body or {}).get("enabled"))
        reg = get_account_registry()
        row = reg.get(platform, account_id)
        if row is None:
            raise HTTPException(404, "账号不存在")
        meta = dict(row.get("meta") or {})
        was = bool(meta.get("auto_reply"))
        meta["auto_reply"] = enabled
        reg.upsert(platform, account_id, meta=meta)
        if was != enabled:
            try:
                from src.integrations.protocol_autoreply_audit import (
                    get_autoreply_audit,
                )
                actor = _session_agent(request)
                get_autoreply_audit().record_config_change(
                    actor=actor.get("agent_id", ""), scope="toggle",
                    platform=platform, account_id=account_id,
                    changes=[{"key": "auto_reply", "old": was, "new": enabled}])
            except Exception:
                logger.debug("[autoreply] 开关审计失败", exc_info=True)
        cfg = (config_manager.config if config_manager is not None else {}) or {}
        from src.integrations.protocol_autoreply_settings import effective_settings
        global_on = bool(effective_settings(cfg).get("enabled", False))
        return {"ok": True, "platform": platform, "account_id": account_id,
                "auto_reply": enabled, "global_enabled": global_on,
                "effective": enabled and global_on}

    @app.post("/api/accounts/{platform}/{account_id}/auto-reply/override")
    async def api_account_auto_reply_override(
        platform: str, account_id: str, request: Request,
    ):
        """按账号覆盖自动回复参数(配额/营业时段/延迟)，写 registry meta.autoreply_override。

        body: {rate?, hours?, delay?}（白名单深合并到现有覆盖）；
        或 {reset: true} 清空该账号覆盖（回落全局）。
        """
        api_auth(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        reg = get_account_registry()
        row = reg.get(platform, account_id)
        if row is None:
            raise HTTPException(404, "账号不存在")
        from src.integrations.protocol_autoreply_settings import (
            deep_merge, diff_settings, sanitize_override,
        )
        meta = dict(row.get("meta") or {})
        before_ov = dict(meta.get("autoreply_override") or {})
        if (body or {}).get("reset"):
            meta.pop("autoreply_override", None)
            override: Dict[str, Any] = {}
        else:
            clean = sanitize_override(body or {})
            override = deep_merge(dict(meta.get("autoreply_override") or {}), clean)
            meta["autoreply_override"] = override
        reg.upsert(platform, account_id, meta=meta)
        try:
            changes = diff_settings(before_ov, override)
            if changes:
                from src.integrations.protocol_autoreply_audit import (
                    get_autoreply_audit,
                )
                actor = _session_agent(request)
                get_autoreply_audit().record_config_change(
                    actor=actor.get("agent_id", ""), scope="account",
                    platform=platform, account_id=account_id, changes=changes)
        except Exception:
            logger.debug("[autoreply] 覆盖审计失败", exc_info=True)
        return {"ok": True, "platform": platform, "account_id": account_id,
                "override": override}

    @app.get("/api/accounts/auto-reply/audit")
    async def api_account_auto_reply_audit(request: Request):
        """自动回复实时流（Phase 4）：最近 N 条决策 + 窗口统计 + 全局闸门状态。

        query: limit(默认50,≤500) / platform / account_id / since(秒,默认24h)
        """
        api_auth(request)
        qp = request.query_params
        try:
            limit = int(qp.get("limit") or 50)
        except Exception:
            limit = 50
        platform = qp.get("platform") or None
        account_id = qp.get("account_id") or None
        try:
            since_sec = float(qp.get("since") or 86400)
        except Exception:
            since_sec = 86400
        from src.integrations.protocol_autoreply_audit import get_autoreply_audit
        audit = get_autoreply_audit()
        items = audit.recent(limit=limit, platform=platform, account_id=account_id)
        stats = audit.stats(since_ts=time.time() - max(0.0, since_sec))
        cfg = (config_manager.config if config_manager is not None else {}) or {}
        from src.integrations.protocol_autoreply_settings import effective_settings
        global_on = bool(effective_settings(cfg).get("enabled", False))
        return {"ok": True, "items": items, "stats": stats,
                "global_enabled": global_on, "count": len(items)}

    @app.get("/api/accounts/auto-reply/config")
    async def api_account_auto_reply_config_get(request: Request):
        """读自动回复全局有效设置（config.yaml 基底 + JSON 覆盖）。"""
        api_auth(request)
        cfg = (config_manager.config if config_manager is not None else {}) or {}
        from src.integrations.protocol_autoreply_settings import effective_settings
        return {"ok": True, "settings": effective_settings(cfg)}

    @app.post("/api/accounts/auto-reply/config")
    async def api_account_auto_reply_config_set(request: Request):
        """改自动回复全局设置（白名单校验落盘 + 热更新限流器，无需重启）。"""
        api_auth(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        from src.integrations.protocol_autoreply_settings import (
            cfg_with_settings, diff_settings, effective_settings, save,
        )
        cfg0 = (config_manager.config if config_manager is not None else {}) or {}
        before = effective_settings(cfg0)
        merged = save(body or {})
        after = effective_settings(cfg0)
        try:
            changes = diff_settings(before, after)
            if changes:
                from src.integrations.protocol_autoreply_audit import (
                    get_autoreply_audit,
                )
                actor = _session_agent(request)
                get_autoreply_audit().record_config_change(
                    actor=actor.get("agent_id", ""), scope="global",
                    changes=changes)
        except Exception:
            logger.debug("[autoreply] 配置变更审计失败", exc_info=True)
        # 热更新限流器阈值
        try:
            from src.integrations.protocol_autoreply_limits import (
                get_autoreply_limiter,
            )
            cfg = (config_manager.config if config_manager is not None else {}) or {}
            lim = get_autoreply_limiter(cfg_with_settings(cfg))
            rate = merged.get("rate") or {}
            brk = merged.get("breaker") or {}
            lim.configure(
                hourly=rate.get("hourly"), daily=rate.get("daily"),
                breaker_threshold=brk.get("threshold"),
                breaker_cooldown=brk.get("cooldown_sec"),
            )
        except Exception:
            logger.debug("[autoreply] 限流器热更新失败", exc_info=True)
        cfg = (config_manager.config if config_manager is not None else {}) or {}
        return {"ok": True, "settings": effective_settings(cfg)}

    @app.get("/api/accounts/auto-reply/health")
    async def api_account_auto_reply_health(request: Request):
        """自动回复一键体检：全局/账号开关、配额余量、熔断、webhook、SkillManager 就绪
        + 最近配置变更，聚合成一张放量前自检表。"""
        api_auth(request)
        cfg = (config_manager.config if config_manager is not None else {}) or {}
        from src.integrations.protocol_autoreply_settings import (
            cfg_with_settings, effective_settings,
        )
        from src.integrations.protocol_autoreply_limits import get_autoreply_limiter
        from src.integrations.protocol_autoreply_audit import get_autoreply_audit
        eff = effective_settings(cfg)
        global_on = bool(eff.get("enabled", False))
        lim = get_autoreply_limiter(cfg_with_settings(cfg))

        accounts_on = 0
        protocol_n = 0
        circuit_open: List[str] = []
        try:
            for row in get_account_registry().list():
                meta = row.get("meta") or {}
                if row.get("mode") == "protocol":
                    protocol_n += 1
                if meta.get("auto_reply"):
                    accounts_on += 1
                ov_rate = (meta.get("autoreply_override") or {}).get("rate") or {}
                snap = lim.snapshot(
                    f"{row.get('platform')}:{row.get('account_id')}",
                    hourly=ov_rate.get("hourly"), daily=ov_rate.get("daily"))
                if snap.get("circuit_open"):
                    circuit_open.append(f"{row.get('platform')}:{row.get('account_id')}")
        except Exception:
            logger.debug("[autoreply-health] registry 读取失败", exc_info=True)

        # webhook 是否订阅了 autoreply_alert（用有效列表：覆盖层优先）
        webhook_on = False
        try:
            from src.integrations.notify_webhooks_store import effective_webhooks
            for wh in effective_webhooks(cfg):
                if wh.get("enabled") is False:
                    continue
                evs = wh.get("events") or []
                if "all" in evs or "autoreply_alert" in evs:
                    webhook_on = True
                    break
        except Exception:
            pass

        sm_ready = getattr(app.state, "skill_manager", None) is not None
        stats = get_autoreply_audit().stats(since_ts=time.time() - 86400)

        warnings: List[str] = []
        if global_on and accounts_on == 0:
            warnings.append("全局已开，但没有任何账号开启自动回复")
        if not global_on and accounts_on > 0:
            warnings.append(f"{accounts_on} 个账号已开自动回复，但全局闸门关闭，不会自动发")
        if global_on and not sm_ready:
            warnings.append("SkillManager 未就绪，无法生成回复")
        if circuit_open:
            warnings.append(f"{len(circuit_open)} 个账号处于熔断中")
        if (global_on or accounts_on) and not webhook_on:
            warnings.append("未配置 autoreply_alert webhook，熔断/配额告警不会外推")

        return {
            "ok": True,
            "healthy": len(warnings) == 0,
            "global_enabled": global_on,
            "skill_manager_ready": sm_ready,
            "webhook_alert_configured": webhook_on,
            "accounts": {"auto_reply_on": accounts_on, "protocol": protocol_n},
            "circuit_open": circuit_open,
            "limits": {
                "hourly": lim.hourly, "daily": lim.daily,
                "breaker_threshold": lim.breaker_threshold,
                "breaker_cooldown": lim.breaker_cooldown,
            },
            "stats_24h": stats,
            "warnings": warnings,
            "recent_changes": get_autoreply_audit().recent_config_changes(limit=10),
        }

    @app.get("/api/accounts/auto-reply/webhooks")
    async def api_account_auto_reply_webhooks_get(request: Request):
        """读有效告警渠道列表（脱敏 token/secret）。"""
        api_auth(request)
        cfg = (config_manager.config if config_manager is not None else {}) or {}
        from src.integrations.notify_webhooks_store import (
            effective_webhooks, mask,
        )
        items = effective_webhooks(cfg)
        return {"ok": True, "webhooks": mask(items), "count": len(items)}

    @app.post("/api/accounts/auto-reply/webhooks")
    async def api_account_auto_reply_webhooks_set(request: Request):
        """整段保存告警渠道列表（白名单校验落盘 + 热更 WebhookNotifier，免重启）。
        token/secret 留空 → 沿用同名旧值（前端展示是脱敏的，避免覆盖真实密钥）。"""
        api_auth(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        incoming = body.get("webhooks") if isinstance(body, dict) else body
        cfg = (config_manager.config if config_manager is not None else {}) or {}
        from src.integrations.notify_webhooks_store import (
            effective_webhooks, mask, sanitize_list, save_list,
        )
        # 按 name 保留旧密钥（前端回传脱敏值或空时不覆盖真实 token/secret）
        old_by_name = {w.get("name"): w for w in effective_webhooks(cfg)}
        cleaned = sanitize_list(incoming)
        for w in cleaned:
            old = old_by_name.get(w.get("name")) or {}
            for k in ("token", "secret"):
                nv = str(w.get(k) or "")
                if (not nv) or nv.endswith("***"):
                    w[k] = str(old.get(k) or "")
        saved = save_list(cleaned)
        # 热更运行中的 notifier
        try:
            notifier = getattr(app.state, "webhook_notifier", None)
            if notifier is not None and hasattr(notifier, "reload"):
                notifier.reload(saved)
        except Exception:
            logger.debug("[autoreply] webhook notifier 热更失败", exc_info=True)
        return {"ok": True, "webhooks": mask(saved), "count": len(saved)}

    @app.post("/api/accounts/auto-reply/webhooks/test")
    async def api_account_auto_reply_webhooks_test(request: Request):
        """对单条渠道即时发一条测试告警（连通性检查）。
        body: {index} 走有效列表第 index 条；或直接传 {webhook:{...}}。"""
        api_auth(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        cfg = (config_manager.config if config_manager is not None else {}) or {}
        from src.integrations.notify_webhooks_store import (
            effective_webhooks, sanitize_webhook,
        )
        items = effective_webhooks(cfg)
        wh: Dict[str, Any] = {}
        if isinstance(body, dict) and body.get("webhook"):
            wh = sanitize_webhook(body.get("webhook"))
            # 测试时若 token 脱敏/空，回退同名已存配置
            old = next((w for w in items if w.get("name") == wh.get("name")), {})
            for k in ("token", "secret"):
                nv = str(wh.get(k) or "")
                if (not nv) or nv.endswith("***"):
                    wh[k] = str(old.get(k) or "")
        else:
            idx = int((body or {}).get("index", -1))
            if 0 <= idx < len(items):
                wh = items[idx]
        if not wh:
            raise HTTPException(400, "未指定有效的 webhook（index 或 webhook）")

        notifier = getattr(app.state, "webhook_notifier", None)
        if notifier is None:
            from src.inbox.webhook_notifier import WebhookNotifier
            notifier = WebhookNotifier(config=[])
        try:
            res = await notifier.send_test(wh)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return res

    @app.get("/api/accounts/auto-reply/stream")
    async def api_account_auto_reply_stream(request: Request):
        """自动回复实时流 SSE：record() 经进程内事件总线即时推送，零轮询零延迟。
        （先订阅再取最新 id 为游标：订阅后的事件必进队列，游标仅用于对订阅/
        取游标竞态窗口内的事件去重，从而不漏不重。）"""
        api_auth(request)
        from starlette.responses import StreamingResponse
        from src.integrations.protocol_autoreply_audit import (
            get_autoreply_audit, subscribe, unsubscribe,
        )
        audit = get_autoreply_audit()
        # 先订阅再取游标：订阅后产生的事件进队列，游标用于补播订阅前的增量并去重
        queue = subscribe()
        seed = audit.recent(limit=1)
        cursor = int(seed[0]["id"]) if seed else 0

        async def _gen():
            import asyncio as _aio
            last_id = cursor
            try:
                yield ": connected\n\n"
                while True:
                    try:
                        row = await _aio.wait_for(queue.get(), timeout=15.0)
                    except _aio.TimeoutError:
                        if await request.is_disconnected():
                            break
                        yield ": heartbeat\n\n"
                        continue
                    rid = int(row.get("id") or 0)
                    if rid and rid <= last_id:
                        continue  # 已补播/已推过，去重
                    last_id = rid
                    yield f"data: {json.dumps(row, ensure_ascii=False)}\n\n"
            finally:
                unsubscribe(queue)

        return StreamingResponse(_gen(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache", "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        })

    @app.on_event("startup")
    async def _orchestrator_autostart():
        cfg = (config_manager.config if config_manager is not None else {}) or {}
        if not orchestrator_enabled(cfg):
            return
        try:
            ensure_builtin_workers(cfg)
            await get_orchestrator(cfg).start_loop()
            logger.info("账号池编排器已随启动开启")
        except Exception:
            logger.debug("编排器自启动失败", exc_info=True)
