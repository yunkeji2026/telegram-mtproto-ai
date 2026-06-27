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

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import Depends, HTTPException, Request
from fastapi.responses import Response

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

def _sla_config(bridge_cfg) -> "tuple[int, int]":
    """从 desktop_bridge 配置解析人审 SLA 阈值（秒）：(warn, urgent)（纯函数，可单测）。

    默认 warn=300 / urgent=900；非法值回落默认；保证 ``urgent >= warn``（否则抬到 warn）。
    """
    br = bridge_cfg if isinstance(bridge_cfg, dict) else {}

    def _pos_int(v, default):
        try:
            n = int(v)
            return n if n > 0 else default
        except Exception:
            return default

    warn = _pos_int(br.get("review_sla_sec"), 300)
    urgent = _pos_int(br.get("review_sla_urgent_sec"), 900)
    if urgent < warn:
        urgent = warn
    return warn, urgent


def _msgs_from_store_rows(rows) -> List[Dict[str, str]]:
    """inbox store 行 → normalize_history 可吃的 [{direction,text}]（纯函数，可单测）。

    只保留有文本的行，direction 归一为 in/out（异常行跳过，不让脏数据炸重写）。
    """
    out: List[Dict[str, str]] = []
    for r in rows or []:
        try:
            text = str((r.get("text") or r.get("original_text") or "")).strip()
            if not text:
                continue
            direction = "out" if str(r.get("direction") or "") == "out" else "in"
            out.append({"direction": direction, "text": text})
        except Exception:
            continue
    return out


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
    async def api_desktop_selector_profiles(request: Request, _=Depends(api_auth)):
        """桌面壳选择器覆写层（D1 热更新）：下发官方网页改版后的「选择器修正」补丁。

        注入脚本（``desktop/inject/profiles.js``）启动时拉取本端点，把补丁叠加到内置档：
        官方改版导致按钮没出现/抓不到文本时，运营改 ``<config 目录>/desktop_selector_profiles.json``
        即可热修，无需重发桌面包。文件不存在=空补丁（注入用内置档，常态）。
        路径取**活动 config 目录**（打包态=可写 AITR_DATA_DIR/config，开发态=<repo>/config），
        与 ``selector-profiles/path``「一键打开热修」指向同一文件。
        返回: {ok, version, profiles: {platform: {selectorKey: value}}}
        """
        from src.web.desktop_selectors import (
            selector_profiles_payload, selector_overlay_path,
        )
        try:
            _path = selector_overlay_path(_active_config_dir(request.app))
            return selector_profiles_payload(_path)
        except Exception:
            logger.debug("[desktop] selector-profiles 读取失败", exc_info=True)
            return {"ok": True, "version": "empty", "profiles": {}}

    @app.get("/api/desktop/selector-profiles/path")
    async def api_desktop_selector_profiles_path(request: Request, _=Depends(api_auth)):
        """「一键热修」用：返回覆写文件的本地绝对路径，并确保其存在（首次写模板）。

        桌面壳据此 ``shell.openPath`` 用系统默认编辑器打开 → 运营改完保存，注入下次拉取即生效。
        与注入读取的 ``selector-profiles`` 指向同一文件（同一 config 目录），不会「打开 A 却读 B」。
        返回: {ok, path, created}
        """
        from src.web.desktop_selectors import (
            selector_overlay_path, ensure_overlay_file,
        )
        try:
            _path = selector_overlay_path(_active_config_dir(request.app))
            created = ensure_overlay_file(_path)
            return {"ok": True, "path": str(_path), "created": created}
        except Exception as exc:
            logger.warning("[desktop] selector-profiles/path 失败: %s", exc)
            return {"ok": False, "path": "", "created": False, "error": str(exc)}

    @app.get("/api/desktop/selector-profiles/validate")
    async def api_desktop_selector_profiles_validate(request: Request, _=Depends(api_auth)):
        """校验覆写文件，给运营显式反馈（手改 JSON 易出错；损坏现状是静默降级为空覆写）。

        与注入读取/「一键打开」同一文件（同 config 目录）。
        返回: {ok, exists, valid, profiles, platforms, dropped, error?}
        """
        from src.web.desktop_selectors import (
            selector_overlay_path, validate_overlay_file,
        )
        try:
            _path = selector_overlay_path(_active_config_dir(request.app))
            return validate_overlay_file(_path)
        except Exception as exc:
            logger.warning("[desktop] selector-profiles/validate 失败: %s", exc)
            return {"ok": False, "valid": False, "error": str(exc),
                    "profiles": 0, "platforms": [], "dropped": []}

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
            selector_failure_breakdown,
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
        alerts = store.persistent_mismatches(persist_sec)
        return {"ok": True, "persist_sec": persist_sec,
                "alerts": alerts,
                "selector_diagnosis": selector_failure_breakdown(alerts),
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

    def _outbound_preview(it):
        _t = str(it.get("text") or "")
        return {
            "id": it.get("id"), "platform": it.get("platform"),
            "account_id": it.get("account_id"), "chat_key": it.get("chat_key"),
            "status": it.get("status"), "attempts": it.get("attempts"),
            "preview": (_t[:24] + "…") if len(_t) > 24 else _t,
        }

    @app.get("/api/desktop/outbound/stats")
    async def api_desktop_outbound_stats(request: Request, _=Depends(api_auth)):
        """受控出站队列看板数据（D4b + P3）：状态概览 + 近期命令 + 待审(held)队列 + 近期拦截率。

        文本仅回 24 字预览（防泄露全文）。``review`` 为 FIFO 待审队列（人审介入用）；
        ``intercept_rate``/``intercept_sample`` = 近 7 日 cancelled/(sent+failed+cancelled)。
        返回: {ok, summary, recent:[...], review:[...], intercept_rate, intercept_sample}
        """
        from src.inbox.desktop_outbound import get_desktop_outbound_queue
        q = get_desktop_outbound_queue()
        try:
            limit = int(request.query_params.get("limit") or 30)
        except Exception:
            limit = 30
        recent = [_outbound_preview(it) for it in q.recent(limit=limit)]
        review = [_outbound_preview(it) for it in q.review_list(limit=50)]
        rate, sample = q.intercept_rate()
        cm = getattr(request.app.state, "config_manager", None)
        cfg = getattr(cm, "config", None) or {}
        bridge = (((cfg.get("inbox", {}) or {}).get("l2_autosend", {}) or {})
                  .get("desktop_bridge", {}) or {})
        sla_warn, sla_urgent = _sla_config(bridge)
        return {"ok": True, "summary": q.summary(), "recent": recent,
                "review": review, "intercept_rate": round(rate, 4),
                "intercept_sample": sample,
                "review_oldest_age_sec": round(q.review_oldest_age(), 1),
                "review_sla_sec": sla_warn, "review_sla_urgent_sec": sla_urgent,
                "corrections": q.corrections_summary(),
                "reason_clusters": q.corrections_reason_breakdown()}

    @app.get("/api/desktop/outbound/corrections")
    async def api_desktop_outbound_corrections(request: Request, _=Depends(api_auth)):
        """人审纠正样本集（P4.2/P4.4/P5）：改写三元组 + 拦截理由，供离线 prompt/KB 调优/导出。

        本端点回**全文**（非预览）——它就是数据资产本身；localhost + token 鉴权下由运营自取。
        query:
          - ``limit``（默认 100，导出可调大，上限 5000）
          - ``kind`` = edit|cancel；``source`` = human|ai_adopted|ai_edited；``since`` = 秒级时间戳（增量）
          - ``format=jsonl`` → 直出 application/x-ndjson 偏好对（rejected/chosen），可下载喂 DPO/eval
          - ``dedup=0`` → 关闭 (kind,rejected,chosen,reason) 去重（默认开）
        默认（无 format）返回: {ok, summary, items:[...原始行...]}
        """
        from src.inbox.desktop_outbound import (
            get_desktop_outbound_queue, corrections_to_export,
        )
        q = get_desktop_outbound_queue()
        qp = request.query_params
        try:
            limit = int(qp.get("limit") or 100)
        except Exception:
            limit = 100
        kind = qp.get("kind") or None
        source = qp.get("source") or None
        since = None
        if qp.get("since"):
            try:
                since = float(qp.get("since"))
            except Exception:
                since = None
        items = q.corrections(limit=limit, kind=kind, source=source, since=since)
        if (qp.get("format") or "").lower() == "jsonl":
            dedup = (qp.get("dedup") or "1") not in ("0", "false", "no")
            lines = [json.dumps(rec, ensure_ascii=False)
                     for rec in corrections_to_export(items, dedup=dedup)]
            body = "\n".join(lines)
            return Response(
                content=body, media_type="application/x-ndjson",
                headers={"Content-Disposition":
                         "attachment; filename=desktop_corrections.jsonl"},
            )
        return {"ok": True, "summary": q.corrections_summary(), "items": items}

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

    @app.post("/api/desktop/outbound/action")
    async def api_desktop_outbound_action(request: Request, _=Depends(api_auth)):
        """受控出站「人审介入」（P2）：对一条命令执行 拦截/暂停/放行/改写/重试。

        - ``cancel`` 拦截：pending/held→cancelled（永不发送）
        - ``hold`` 暂停：pending→held（不再被自动 pull）
        - ``release`` 放行：held→pending（重新进入自动发送）
        - ``edit`` 改写：仅 pending/held 可改文本（claimed/已终态拒，防改飞行中/已发）
        - ``retry`` 重试：failed→pending
        body: {id, action, text?}　返回: {ok, applied, action}
        """
        try:
            body = await request.json()
        except Exception:
            body = {}
        try:
            item_id = int((body or {}).get("id") or 0)
        except Exception:
            item_id = 0
        action = str((body or {}).get("action") or "").strip().lower()
        text = str((body or {}).get("text") or "")
        ids_raw = (body or {}).get("ids")
        from src.inbox.desktop_outbound import get_desktop_outbound_queue
        q = get_desktop_outbound_queue()

        reason = str((body or {}).get("reason") or "")
        ai_suggestion = str((body or {}).get("ai_suggestion") or "")
        source = str((body or {}).get("source") or "")

        def _apply(iid: int) -> bool:
            if action == "cancel":
                return q.cancel(iid, reason=reason)
            if action == "hold":
                return q.hold(iid)
            if action == "release":
                return q.release(iid)
            if action == "retry":
                return q.retry(iid)
            if action == "edit":
                return q.edit(iid, text, ai_suggestion=ai_suggestion, source=source)
            raise HTTPException(400, "未知 action：" + action)

        # 批量（P3：全部放行 / 全部拦截）——ids 给定时逐条执行，返回成功条数。
        if isinstance(ids_raw, list) and ids_raw:
            applied = 0
            for raw in ids_raw:
                try:
                    iid = int(raw)
                except Exception:
                    continue
                if iid and _apply(iid):
                    applied += 1
            return {"ok": True, "applied": applied, "action": action, "batch": True}

        if not item_id:
            raise HTTPException(400, "id 不能为空")
        return {"ok": True, "applied": _apply(item_id), "action": action}

    @app.post("/api/desktop/outbound/rewrite")
    async def api_desktop_outbound_rewrite(request: Request, _=Depends(api_auth)):
        """AI 重写助手（P4.1）：给一条待发/待审命令生成「更好的候选回复」供人审采纳。

        与全自动草稿同一条产线（``generate_persona_reply``，带人设/KB/语言守卫）。读 inbox store
        按 ``conv_id(platform,account_id,chat_key)`` 取最近会话上下文 → 客户最后一句为「待回复」。
        **只回候选、不落库**；运营在行内编辑器采纳 → 走既有 edit（自动留 before/after 纠正样本，
        形成自增强闭环）。
        body: {id}　返回: {ok, reply, reply_lang?, original} 或 {ok:false, detail}
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
        from src.inbox.desktop_outbound import get_desktop_outbound_queue
        cmd = get_desktop_outbound_queue().get(item_id)
        if not cmd:
            return {"ok": False, "detail": "命令不存在或已清理"}
        platform = str(cmd.get("platform") or "")
        account_id = str(cmd.get("account_id") or "")
        chat_key = str(cmd.get("chat_key") or "")

        store = getattr(request.app.state, "inbox_store", None)
        history: List[Dict[str, str]] = []
        last_inbound = ""
        if store is not None and chat_key:
            try:
                from src.inbox.normalizer import conv_id
                cid = (cmd.get("conversation_id")
                       or conv_id(platform, account_id, chat_key))
                rows = store.list_recent_messages(cid, limit=30)
                history, last_inbound = normalize_history(_msgs_from_store_rows(rows))
            except Exception:
                logger.debug("[desktop] rewrite 取会话上下文失败", exc_info=True)
        if not last_inbound:
            return {"ok": False,
                    "detail": "无客户会话上下文（inbox 未启用或该会话无入站消息），无法重写"}

        out = await generate_persona_reply(
            app=request.app, platform=platform, chat_key=chat_key,
            last_inbound=last_inbound, history=history,
        )
        if not (out.get("ok") and out.get("reply")):
            return {"ok": False, "detail": str(out.get("detail") or "生成失败")}
        return {"ok": True, "reply": out.get("reply"),
                "reply_lang": out.get("reply_lang", ""),
                "original": str(cmd.get("text") or "")}

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
