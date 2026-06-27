"""统一收件箱——桌面壳 AI / 同步桥路由域（巨石拆分 slice 33）。

把 ``register_unified_inbox_routes`` 巨型闭包中连续的「桌面壳专用」子域整体外移为
``register_desktop_routes(app, *, api_auth)``，由主 register 在**原位置**调用：

- ``desktop/smart-reply``：人设化智能回复（SkillManager → KB → 翻译 optional）
- ``desktop/guard-check``：填入并发送前规则层风控护栏
- ``desktop/ingest``：官方 web 客户端 DOM 消息回流统一收件箱

端点路径/方法/响应零变化（admin_route_inventory URL 契约守卫 + slice 33 端点契约断言）。

依赖全部朝下：smart-reply 走 inbox.persona_reply（单一事实源，与收件箱全自动草稿同产线）；
ingest 走 protocol_bridge.ingest_incoming + account_registry（handler 内局部 import）。
只收 api_auth 一个参数。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import Depends, HTTPException, Request

from src.inbox.persona_reply import generate_persona_reply, normalize_history

logger = logging.getLogger(__name__)


def _active_config_dir(app) -> Optional[Path]:
    """活动 config 目录（与 config.yaml 同目录）；打包态=可写 AITR_DATA_DIR/config。"""
    cm = getattr(app.state, "config_manager", None)
    try:
        if cm is not None and getattr(cm, "config_path", None):
            return Path(cm.config_path).parent
    except Exception:
        logger.debug("[desktop] 解析 config 目录失败", exc_info=True)
    return None

# guard-check 机器措辞检测（模块级常量，便于后续扩展/单测引用）
_ROBOTIC_PHRASES = (
    "作为AI", "作为一个AI", "作为人工智能", "我是语言模型", "我是机器人",
    "有什么可以帮您", "很高兴为您服务", "请问有什么可以帮",
)


def register_desktop_routes(app, *, api_auth) -> None:
    """挂载桌面壳 smart-reply / guard-check / ingest 端点。"""

    @app.post("/api/desktop/smart-reply")
    async def api_desktop_smart_reply(request: Request, _=Depends(api_auth)):
        """桌面壳（嵌官方 web 客户端）专用：**人设化**智能回复。

        走与 ``/api/chat/test`` 同一条产线（SkillManager → 意图 → 策略 → KB →
        ``AIClient.generate_reply_with_intent``），由 PersonaManager 注入后台人设
        （domain ``conversion`` 的「线上陪伴」或 account_persona_id 指定的画像），
        因此回复带人设口吻、禁用「作为AI」等机器措辞、并融合知识库——而非通用提示词。

        body: {messages:[{direction,text}], persona_id?, platform?, chat_key?, target_lang?}
        返回: {ok, reply, persona?, intent?, translated?}
        """
        body = await request.json()
        msgs = body.get("messages") if isinstance(body.get("messages"), list) else []
        target_lang = str(body.get("target_lang") or "").strip()
        persona_id = str(body.get("persona_id") or "").strip()
        platform = str(body.get("platform") or "telegram").strip()
        chat_key = str(body.get("chat_key") or "").strip()

        # 归一对话历史（OpenAI 风格）+ 取最后一条入站消息作为「待回复」
        history, last_inbound = normalize_history(msgs)
        if not last_inbound:
            return {"ok": False, "detail": "无可用对话上下文"}

        # 人设化回复走单一事实源（persona_reply.generate_persona_reply）：
        # 与收件箱全自动草稿 / 协议自动回复同一条产线，避免逻辑分叉。
        out = await generate_persona_reply(
            app=request.app,
            platform=platform,
            chat_key=chat_key,
            last_inbound=last_inbound,
            history=history,
            persona_id=persona_id,
            target_lang=target_lang,
        )
        out.pop("detail", None)
        return out

    @app.post("/api/desktop/guard-check")
    async def api_desktop_guard_check(request: Request, _=Depends(api_auth)):
        """桌面壳「填入并发送」前风控护栏（规则层，零 LLM 成本，毫秒级）。

        复用 ``src.inbox.drafts.keyword_risk_level``（支付/密码/账号安全=high→拦截；
        优惠/投诉/法律=medium→提醒），并检测「作为AI」等机器措辞（可能露馅）。
        body: {text}
        返回: {ok, risk: high|medium|low, block, hits:[{term,level}], robotic:[...]}
        """
        try:
            body = await request.json()
        except Exception:
            body = {}
        text = str((body or {}).get("text") or "")
        risk = "low"
        hits: List[Dict[str, str]] = []
        robotic: List[str] = []
        try:
            from src.inbox.drafts import keyword_risk_level, _SENSITIVE_PATTERNS
            risk = keyword_risk_level(text) or "low"
            for pattern, level in _SENSITIVE_PATTERNS:
                m = pattern.search(text)
                if m:
                    hits.append({"term": m.group(0), "level": level})
        except Exception:
            logger.debug("[desktop] guard-check 规则层失败", exc_info=True)
        for ph in _ROBOTIC_PHRASES:
            if ph in text:
                robotic.append(ph)
        return {"ok": True, "risk": risk, "block": risk == "high",
                "hits": hits, "robotic": robotic}

    @app.post("/api/desktop/ingest")
    async def api_desktop_ingest(request: Request, _=Depends(api_auth)):
        """桌面壳同步桥（P1）：把官方 web 客户端 DOM 抓到的消息回流统一收件箱。

        与 ``/api/internal/protocol/ingest``（Baileys 等真 worker）不同：桌面账号无服务端
        worker、不被编排器接管，故首次同步即把账号以 ``mode="desktop"`` 落 registry——
        让收件箱列表（ProtocolInboxAdapter）与线程（_is_protocol_account）按 store 读出，
        而 ``worker_supported`` 不含 desktop 模式，编排器自动跳过、不会尝试拉起 worker。
        body: {platform, account_id, chat_key, name?, text?, ts?, msg_id?, direction?,
               media_type?, media_ref?}
        """
        try:
            body = await request.json()
        except Exception:
            body = {}
        store = getattr(request.app.state, "inbox_store", None)
        if store is None:
            raise HTTPException(503, "inbox store 未就绪")
        platform = str((body or {}).get("platform") or "").lower()
        account_id = str((body or {}).get("account_id") or "")
        chat_key = str((body or {}).get("chat_key") or "")
        if not platform or not account_id:
            raise HTTPException(400, "platform / account_id 不能为空")
        if not chat_key:
            raise HTTPException(400, "chat_key 不能为空")
        try:
            from src.integrations.account_registry import get_account_registry
            reg = get_account_registry()
            row = reg.get(platform, account_id)
            if not row:
                reg.upsert(platform, account_id, mode="desktop",
                           label=str((body or {}).get("name") or account_id),
                           status="online")
        except Exception:
            logger.debug("[desktop] registry upsert 失败（已忽略）", exc_info=True)
        from src.integrations.protocol_bridge import ingest_incoming
        cid = ingest_incoming(
            store,
            platform=platform,
            account_id=account_id,
            chat_key=chat_key,
            name=str((body or {}).get("name") or ""),
            text=str((body or {}).get("text") or ""),
            ts=float((body or {}).get("ts") or 0),
            msg_id=str((body or {}).get("msg_id") or ""),
            direction=str((body or {}).get("direction") or "in"),
            media_type=str((body or {}).get("media_type") or ""),
            media_ref=str((body or {}).get("media_ref") or ""),
        )
        return {"ok": bool(cid), "conversation_id": cid or ""}

    @app.get("/api/desktop/selector-profiles")
    async def api_desktop_selector_profiles(_=Depends(api_auth)):
        """桌面壳选择器覆写层（D1 热更新）：下发官方网页改版后的「选择器修正」补丁。

        注入脚本（``desktop/inject/profiles.js``）启动时拉取本端点，把补丁叠加到内置档：
        官方改版导致按钮没出现/抓不到文本时，运营改 ``config/desktop_selector_profiles.json``
        即可热修，无需重发桌面包。文件不存在=空补丁（注入用内置档，常态）。
        返回: {ok, version, profiles: {platform: {selectorKey: value}}}
        """
        from src.web.desktop_selectors import selector_profiles_payload
        try:
            return selector_profiles_payload()
        except Exception:
            logger.debug("[desktop] selector-profiles 读取失败", exc_info=True)
            return {"ok": True, "version": "empty", "profiles": {}}

    @app.post("/api/desktop/inject-health")
    async def api_desktop_inject_health(request: Request, _=Depends(api_auth)):
        """桌面壳注入健康信标（D1b）：收注入脚本的「逐选择器命中」上报，存最新一条。

        注入在状态变化或每 30s 心跳上报；后端据此让运营看板区分某账号是注入正常 / 输入框失配 /
        气泡失配 / 未登录。失配多半是官方网页改版，可走 D1 覆写层热修。
        body: {platform, account_id, supported, composer, bubbles, chatOpen, selectors{...}, ...}
        返回: {ok, status}
        """
        try:
            body = await request.json()
        except Exception:
            body = {}
        from src.web.desktop_inject_health import get_inject_health_store
        rec = get_inject_health_store().record(body or {})
        return {"ok": True, "status": rec.get("status")}

    @app.get("/api/desktop/inject-health")
    async def api_desktop_inject_health_list(request: Request, _=Depends(api_auth)):
        """桌面壳注入健康看板数据：各内嵌账号最新状态 + 概览计数。

        超过 90s 未上报标记 ``stale``（注入可能已停摆/页面被关）；失配账号附 ``mismatch_secs``
        （已持续秒数）。``summary.persistent_mismatch`` = 失配持续超 ``persist_sec``（默认 300s）的账号数。
        返回: {ok, persist_sec, summary:{ok,mismatch,persistent_mismatch,...,total},
               accounts:[{platform,account_id,status,stale,mismatch_secs,...}]}
        """
        from src.web.desktop_inject_health import (
            get_inject_health_store, DEFAULT_PERSIST_SEC,
        )
        try:
            persist_sec = float(request.query_params.get("persist_sec")
                                or DEFAULT_PERSIST_SEC)
        except Exception:
            persist_sec = DEFAULT_PERSIST_SEC
        store = get_inject_health_store()
        return {"ok": True, "persist_sec": persist_sec,
                "summary": store.summary(persist_sec=persist_sec),
                "accounts": store.latest(stale_after=90.0)}

    @app.get("/api/desktop/inject-health/alerts")
    async def api_desktop_inject_health_alerts(request: Request, _=Depends(api_auth)):
        """注入失配「持续告警流 + 趋势」（D4 续，#4 失配持续告警升级）。

        把 D1c 的「即时 toast」升级为「失配持续 ≥ persist_sec → 告警」+ 状态跃迁历史（趋势）。
        即时失配会自愈（一闪而过的抖动不告警）；只有**连续**失配超阈值才进告警，运营据此走
        D1 覆写层精准热修。
        query: persist_sec?（默认 300）、limit?（趋势事件条数，默认 50）
        返回: {ok, persist_sec, alerts:[{platform,account_id,status,mismatch_secs,selectors}],
               events:[{platform,account_id,status,from,ts}]}
        """
        from src.web.desktop_inject_health import (
            get_inject_health_store, DEFAULT_PERSIST_SEC,
        )
        try:
            persist_sec = float(request.query_params.get("persist_sec")
                                or DEFAULT_PERSIST_SEC)
        except Exception:
            persist_sec = DEFAULT_PERSIST_SEC
        try:
            limit = int(request.query_params.get("limit") or 50)
        except Exception:
            limit = 50
        store = get_inject_health_store()
        return {"ok": True, "persist_sec": persist_sec,
                "alerts": store.persistent_mismatches(persist_sec),
                "events": store.recent_events(limit=limit)}

    @app.get("/api/desktop/outbound")
    async def api_desktop_outbound_pull(request: Request, _=Depends(api_auth)):
        """桌面壳 / 扩展轮询「受控出站队列」：认领某内嵌账号的待发命令（D4）。

        全自动 autopilot 决定要发给 desktop 模式账号时，回复已**先过 send-gate/kill-switch
        闸门**再落队列；桌面壳/扩展据 ``account_id`` 轮询本端点取走，在官方网页 DOM 填入并
        发送（复用 inject ``fill-composer``），再调 ``/api/desktop/outbound/ack`` 回执。
        认领即标 ``claimed``（>180s 未 ack 自动回收防卡死）。
        query: platform, account_id, limit?
        返回: {ok, items:[{id,platform,account_id,chat_key,text,kind,...}]}
        """
        platform = str(request.query_params.get("platform") or "").lower()
        account_id = str(request.query_params.get("account_id") or "")
        # chat_key 给定 → 仅认领该会话命令（注入只填当前打开会话、不导航，防发错聊天）
        _ck = request.query_params.get("chat_key")
        chat_key = None if _ck is None else str(_ck)
        try:
            limit = int(request.query_params.get("limit") or 20)
        except Exception:
            limit = 20
        if not platform or not account_id:
            raise HTTPException(400, "platform / account_id 不能为空")
        from src.inbox.desktop_outbound import get_desktop_outbound_queue
        items = get_desktop_outbound_queue().pull(
            platform, account_id, chat_key=chat_key, limit=limit)
        return {"ok": True, "items": items}

    @app.get("/api/desktop/outbound/stats")
    async def api_desktop_outbound_stats(request: Request, _=Depends(api_auth)):
        """受控出站队列看板数据（D4b）：按状态概览 + 近期命令（文本仅预览，防泄露全文）。

        返回: {ok, summary:{pending,claimed,sent,failed,total}, recent:[{id,platform,
               account_id,chat_key,status,attempts,preview}]}
        """
        from src.inbox.desktop_outbound import get_desktop_outbound_queue
        q = get_desktop_outbound_queue()
        try:
            limit = int(request.query_params.get("limit") or 30)
        except Exception:
            limit = 30
        recent = []
        for it in q.recent(limit=limit):
            _t = str(it.get("text") or "")
            recent.append({
                "id": it.get("id"), "platform": it.get("platform"),
                "account_id": it.get("account_id"), "chat_key": it.get("chat_key"),
                "status": it.get("status"), "attempts": it.get("attempts"),
                "preview": (_t[:24] + "…") if len(_t) > 24 else _t,
            })
        return {"ok": True, "summary": q.summary(), "recent": recent}

    @app.post("/api/desktop/outbound/ack")
    async def api_desktop_outbound_ack(request: Request, _=Depends(api_auth)):
        """桌面壳 / 扩展发完一条出站命令后回执（D4）：claimed→sent/failed。

        body: {id, ok?, error?}
        返回: {ok, acked}
        """
        try:
            body = await request.json()
        except Exception:
            body = {}
        try:
            item_id = int((body or {}).get("id") or 0)
        except Exception:
            item_id = 0
        if not item_id:
            raise HTTPException(400, "id 不能为空")
        ok = bool((body or {}).get("ok", True))
        error = str((body or {}).get("error") or "")
        from src.inbox.desktop_outbound import get_desktop_outbound_queue
        acked = get_desktop_outbound_queue().ack(item_id, ok=ok, error=error)
        return {"ok": True, "acked": acked}

    @app.get("/api/desktop/fingerprint")
    async def api_desktop_fingerprint(request: Request, _=Depends(api_auth)):
        """桌面壳内嵌 webview 的「一号一指纹」（D3 防关联封号）。

        以 ``account_id`` 为种子**确定性**派生（同账号永远同指纹，无需持久化）：桌面端据此
        给每个账号的 webview 分区设独立 UA/Accept-Language + 注入 navigator/Intl/WebGL/Canvas 覆盖，
        使多号内嵌时互不关联（IG/Meta/X 反作弊会把「同机同环境多号」连坐封）。仅桌面 UA 子池。
        query: account_id（种子；空则随机一次性）、platform（仅日志）
        返回: {ok, fingerprint: {...}}
        """
        account_id = str(request.query_params.get("account_id") or "").strip()
        from src.integrations.fingerprint import generate_fingerprint
        fp = generate_fingerprint(account_id or None, desktop_only=True)
        return {"ok": True, "fingerprint": fp}
