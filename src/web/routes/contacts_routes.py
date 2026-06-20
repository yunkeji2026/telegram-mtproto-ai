"""Contacts / Journey / Merge Review Web REST 路由。

按既有 routes/ 约定导出 `register_contacts_routes`，由 admin.py 按需挂载。
当前文件不做页面模板渲染——下阶段 W3 再做 `contacts.html` / `merge_reviews.html`。

端点清单：
- GET  /api/contacts                      列表（分页）
- GET  /api/contacts/{id}                 Contact 详情 + journey + 所有 channel_identity
- GET  /api/contacts/{id}/timeline        journey_events 时间线
- GET  /api/merge-reviews                 pending 合并审核队列
- POST /api/merge-reviews/{id}/approve    通过（触发 relink + 标 resolved）
- POST /api/merge-reviews/{id}/reject     拒绝（标 resolved，不动 ci）
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

_OPS_TPL_DIR = Path(__file__).resolve().parent.parent / "templates" / "ops"

_NAV_LINKS = [
    ("/ops/contacts", "联系人"),
    ("/ops/merge-reviews", "合并审核"),
    ("/ops/mobile-handoffs", "Mobile 交接单"),
]


def _make_nav_html(active: str = "") -> str:
    links = "".join(
        f'<a href="{h}" {"class=\"active\"" if h == active else ""}>'
        f"{label}</a>"
        for h, label in _NAV_LINKS
    )
    return f'<div class="nav">{links}</div>'


def _load_ops_html(name: str, active: str = "") -> str:
    """从 templates/ops/ 加载静态 HTML。
    active 为当前页 href，注入统一导航栏（替换 <!-- NAV_INJECT --> 占位符）。
    """
    p = _OPS_TPL_DIR / name
    try:
        html = p.read_text(encoding="utf-8")
        return html.replace("<!-- NAV_INJECT -->", _make_nav_html(active), 1)
    except FileNotFoundError:
        return f"<h1>Ops UI missing: {name}</h1>"

from src.contacts.merge import MergeService
from src.contacts.reunion_prompts import (
    get_registry as _get_reunion_registry,
    hash_prompt as _hash_prompt,
    load_persona_for_prompt as _load_persona_for_prompt,
)
from src.contacts.store import ContactStore

# 可选依赖：intimacy / reactivation。注入时才挂载对应 endpoint。
try:
    from src.skills.intimacy_engine import IntimacyEngine
except ImportError:
    IntimacyEngine = None  # type: ignore

try:
    from src.skills.reactivation_scheduler import ReactivationScheduler
except ImportError:
    ReactivationScheduler = None  # type: ignore

logger = logging.getLogger(__name__)


# W3-3B.4：合并信号的人话化标签 + emoji
# breakdown_json 来自 merge.score_signals()，每项是「权重 × 信号值」的乘积。
# 由于权重不同，单看数字会误导（lang_match=0.20 是满分但 name_match=0.20 只是 67%）。
# 这里把贡献翻成 0-100 的"实际相对价值"百分比 + 直觉标签。
_SIGNAL_LABELS = {
    "name_match":     ("\U0001F464", "\u540D\u5B57\u76F8\u4F3C", 0.30),  # 👤 名字相似
    "lang_match":     ("\U0001F310", "\u8BED\u8A00\u540C",       0.20),  # 🌐 语言同
    "tz_match":       ("\U0001F30F", "\u65F6\u533A\u540C",       0.15),  # 🌏 时区同
    "time_proximity": ("\u23F0",     "\u65F6\u5E8F\u63A5\u8FD1", 0.25),  # ⏰ 时序接近
    "style_match":    ("\u270D\uFE0F", "\u98CE\u683C\u76F8\u4F3C", 0.10),  # ✍️ 风格相似
}


def _humanize_breakdown(bd: Dict[str, float]) -> Dict[str, Any]:
    """把 breakdown dict 翻成 UI 友好结构。

    输出：
        {
          "items": [{"key":..., "icon":..., "label":..., "raw":..., "pct":..., "weight":...}, ...]
              # 按贡献降序
          "top": ["name_match", "time_proximity"]   # 高亮的 top-2
        }
    """
    items = []
    for key, contrib in (bd or {}).items():
        meta = _SIGNAL_LABELS.get(key)
        if not meta:
            continue
        icon, label, weight = meta
        c = float(contrib or 0.0)
        # 把贡献还原成「该信号本身的 0-100 分」（避免运营被加权值绕晕）
        raw_pct = round((c / weight) * 100, 0) if weight > 0 else 0
        items.append({
            "key": key,
            "icon": icon,
            "label": label,
            "contrib": round(c, 3),
            "weight": weight,
            "raw_pct": int(raw_pct),
        })
    items.sort(key=lambda x: x["contrib"], reverse=True)
    return {
        "items": items,
        "top": [it["key"] for it in items[:2]],
    }


# W3-3D.3 / W3-3E.1：trend + digest 共用 SingleEntryTTLCache（src/utils/cache.py）
# 60s TTL；写后失效（merge approve / handoff issue 等）会主动清空。
from src.utils.cache import SingleEntryTTLCache

_intimacy_trend_cache = SingleEntryTTLCache(ttl_s=60.0)
_relations_digest_cache = SingleEntryTTLCache(ttl_s=120.0)  # digest 数据更聚合，可缓存更久


def _intimacy_trend_cache_get(key):
    return _intimacy_trend_cache.get(key)


def _intimacy_trend_cache_put(key, payload):
    _intimacy_trend_cache.put(key, payload)


def _intimacy_trend_cache_clear() -> None:
    """供测试隔离 + 写后失效用。"""
    _intimacy_trend_cache.clear()
    _relations_digest_cache.clear()


# W3-3C.1：把 intimacy_score 派生成「陪伴 stage」给运营看
# 仅纯派生（不写回 journey），让 ai_studio 第一次能看到「AI 用什么语气对待该用户」。
# 这是单参数派生（仅 intimacy_score），不依赖 conversation state 的 exchange_count，
# 故可在批量列表中 enrich 不引发 N+1。完整 fuse_with_intimacy 留给单 journey 详情页。
try:
    from src.utils.companion_relationship import (
        derive_stage_from_intimacy,
        STAGE_LABEL_ZH as _COMPANION_STAGE_LABEL,
    )
except ImportError:
    derive_stage_from_intimacy = None  # type: ignore
    _COMPANION_STAGE_LABEL = {}  # type: ignore


def _intimacy_stage_info(intimacy_score: Optional[float]) -> Optional[Dict[str, Any]]:
    """从 intimacy_score 派生「陪伴 stage」+ 中文标签。score=None → None。"""
    if derive_stage_from_intimacy is None or intimacy_score is None:
        return None
    stage = derive_stage_from_intimacy(intimacy_score)
    if stage is None:
        return None
    return {
        "stage": stage,
        "label": _COMPANION_STAGE_LABEL.get(stage, stage),
    }


def _journey_to_dict(journey) -> Dict[str, Any]:
    return {
        "journey_id": journey.journey_id,
        "contact_id": journey.contact_id,
        "persona_id": journey.persona_id,
        "funnel_stage": journey.funnel_stage,
        "intimacy_score": journey.intimacy_score,
        "engagement_score": journey.engagement_score,
        "readiness_score": journey.readiness_score,
        "intimacy_updated_at": journey.intimacy_updated_at,
        "snapshot_refreshed_at": journey.snapshot_refreshed_at,
        "created_at": journey.created_at,
        "updated_at": journey.updated_at,
    }


def register_contacts_routes(
    app,
    *,
    api_auth,
    contacts_store: ContactStore,
    merge_service: MergeService,
    audit_store=None,
    intimacy_engine=None,
    reactivation_scheduler=None,
    eval_scheduler=None,
    gateway=None,
    account_limiter=None,
    mobile_bridge=None,
    fire_webhook=None,
    ai_client=None,
) -> None:
    """在 FastAPI app 上挂载 contacts 相关的 REST endpoint。

    参数：
      app            — FastAPI 实例
      api_auth       — 鉴权 Depends callable（由 admin.py 统一提供）
      contacts_store — ContactStore 实例（建议由上层做 singleton）
      merge_service  — MergeService 实例
      audit_store    — 可选，若提供则记录敏感操作（approve/reject）
    """

    @app.get("/api/contacts")
    async def list_contacts(
        limit: int = 50,
        offset: int = 0,
        expand: str = "",
        q: str = "",
        _=Depends(api_auth),
    ):
        """
        expand=journey 时，item 含 funnel_stage / intimacy_score 字段——
        消除 UI 的 N+1 请求。
        q 时按 contact_id / 姓名 / canonical_id 搜索。
        """
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        rows, total = contacts_store.search_contacts(q=q.strip(), limit=limit, offset=offset)
        expand_set = set((expand or "").split(","))
        include_journey = "journey" in expand_set
        include_channels = "channels" in expand_set  # W3-3L.4
        items: list = []
        # W3-3L.4：批量预取 channel_identities（单次 SQL，消除 N+1）
        ci_by_contact: Dict[str, list] = {}
        if include_channels and rows:
            cids = [c.contact_id for c in rows]
            ci_map = contacts_store.list_channel_identities_for_contacts(cids)
            ci_by_contact = {
                cid: [ci.to_dict() for ci in cis]
                for cid, cis in ci_map.items()
            }
        for c in rows:
            d = c.to_dict()
            if include_journey:
                j = contacts_store.get_journey_by_contact(c.contact_id)
                if j:
                    d["funnel_stage"] = j.funnel_stage
                    d["intimacy_score"] = j.intimacy_score
                    d["journey_id"] = j.journey_id
                    # W3-3C.1：暴露陪伴 stage（AI 对话语气视角）
                    d["intimacy_stage"] = _intimacy_stage_info(j.intimacy_score)
                    d["persona_id"] = j.persona_id  # P23-1: 人设效果看板
            if include_channels:
                d["channels"] = [ci["channel"] for ci in ci_by_contact.get(c.contact_id, [])]
            items.append(d)
        return {
            "total": total,
            "q": q.strip(),
            "items": items,
            "limit": limit,
            "offset": offset,
        }

    @app.get("/api/contacts/{contact_id}")
    async def get_contact(contact_id: str, _=Depends(api_auth)):
        c = contacts_store.get_contact(contact_id)
        if not c:
            raise HTTPException(status_code=404, detail="contact_not_found")
        journey = contacts_store.get_journey_by_contact(contact_id)
        cis = contacts_store.list_channel_identities_of(contact_id)
        return {
            "contact": c.to_dict(),
            "journey": _journey_to_dict(journey) if journey else None,
            "channel_identities": [ci.to_dict() for ci in cis],
        }

    @app.get("/api/contacts/{contact_id}/timeline")
    async def timeline(
        contact_id: str,
        limit: int = 100,
        _=Depends(api_auth),
    ):
        journey = contacts_store.get_journey_by_contact(contact_id)
        if not journey:
            raise HTTPException(status_code=404, detail="journey_not_found")
        limit = max(1, min(int(limit), 500))
        events = contacts_store.list_events(journey.journey_id, limit=limit)
        return {
            "journey_id": journey.journey_id,
            "funnel_stage": journey.funnel_stage,
            "events": events,
        }

    @app.get("/api/merge-reviews")
    async def list_reviews(limit: int = 100, _=Depends(api_auth)):
        limit = max(1, min(int(limit), 200))
        items = contacts_store.list_pending_reviews(limit=limit)
        # 丰富一下信息：返回候选 ci + target contact 的基本字段，便于 UI 展示
        enriched = []
        for r in items:
            ci = contacts_store.get_channel_identity(r["candidate_ci_id"])
            tgt = contacts_store.get_contact(r["target_contact_id"])
            # W3-3B.4：breakdown 人话化 — 把每个信号的加权贡献翻成 UI 友好结构，
            # 高亮 top-2 信号让运营 5 秒看出「为什么系统建议合并」
            bd = r.get("breakdown") or {}
            signals_human = _humanize_breakdown(bd)
            # 同时拉 target contact 的 journey 给 UI 展示（关系阶段+亲密度）
            target_journey = None
            if tgt:
                tj = contacts_store.get_journey_by_contact(tgt.contact_id)
                if tj:
                    target_journey = {
                        "funnel_stage": tj.funnel_stage,
                        "intimacy_score": round(tj.intimacy_score, 1),
                    }
            enriched.append({
                **r,
                "candidate_ci": ci.to_dict() if ci else None,
                "target_contact": tgt.to_dict() if tgt else None,
                "target_journey": target_journey,
                "signals_human": signals_human,
            })
        return {"items": enriched}

    @app.post("/api/merge-reviews/scan")
    async def scan_merge_candidates(request: Request, _=Depends(api_auth)):
        """W3-3B.4：主动扫描 — 遍历未合并的 LINE channel_identity，对每个调用
        ``MergeService.evaluate``。命中 manual_review 阈值就入队。

        弥补「被动触发」的 silent gap：历史孤立 LINE contacts 永远不会被评估。
        手动触发，避免 cron 失控。
        """
        if gateway is None:
            raise HTTPException(status_code=503, detail="gateway_not_wired")
        from src.contacts.models import (
            CHANNEL_LINE, DECISION_MANUAL_REVIEW, DECISION_AUTO_MERGE,
        )
        # 找所有 LINE 的 channel_identity 且其所在 contact 未合并过 messenger
        with contacts_store._lock:  # noqa: SLF001
            line_rows = contacts_store._conn.execute(  # noqa: SLF001
                "SELECT ci.channel_identity_id, ci.contact_id, ci.display_name, "
                "       c.language_hint, c.timezone_hint "
                "FROM channel_identities ci "
                "JOIN contacts c ON c.contact_id = ci.contact_id "
                "WHERE ci.channel = ? "
                "  AND ci.contact_id NOT IN ("
                "      SELECT DISTINCT contact_id FROM channel_identities "
                "      WHERE channel = 'messenger'"
                "  )",
                (CHANNEL_LINE,),
            ).fetchall()
        scanned = 0
        enqueued = 0
        auto_eligible = 0  # 仅记录数量，扫描不擅自 auto_merge（需运营操作）
        for row in line_rows:
            scanned += 1
            ci = contacts_store.get_channel_identity(row["channel_identity_id"])
            if ci is None:
                continue
            best, decision = merge_service.evaluate(
                line_ci=ci,
                line_display_name=row["display_name"] or "",
                line_lang=row["language_hint"] or "",
                line_tz=row["timezone_hint"] or "",
            )
            if best is None:
                continue
            # 主动扫描下：哪怕够 auto 阈值，也走 review（运营审核保护人设）
            if decision.decision in (DECISION_AUTO_MERGE, DECISION_MANUAL_REVIEW):
                if decision.decision == DECISION_AUTO_MERGE:
                    auto_eligible += 1
                rid = contacts_store.enqueue_merge_review(
                    candidate_ci_id=ci.channel_identity_id,
                    target_contact_id=best.messenger_ci.contact_id,
                    confidence=decision.confidence,
                    breakdown=decision.breakdown or {},
                )
                if rid:
                    enqueued += 1
        user = _extract_user(request)
        if audit_store:
            _safe_audit(
                audit_store, user, "merge_review_scan",
                f"scanned={scanned} enqueued={enqueued} auto_eligible={auto_eligible}",
            )
        # W3-3E.2：scan 入队了新 review → digest pending count 改变
        if enqueued:
            _intimacy_trend_cache_clear()
        return {
            "ok": True,
            "scanned": scanned,
            "enqueued": enqueued,
            "auto_eligible": auto_eligible,
        }

    @app.post("/api/merge-reviews/{review_id}/approve")
    async def approve_review(review_id: str, request: Request, _=Depends(api_auth)):
        user = _extract_user(request)
        ok = merge_service.approve_review(review_id, resolved_by=user)
        if audit_store and ok:
            _safe_audit(audit_store, user, "merge_review_approve", review_id)
        if not ok:
            raise HTTPException(status_code=400, detail="approve_failed_or_already_resolved")
        # W3-3E.2：写后失效——合并改变了关系拓扑，趋势/digest 缓存必须失效
        _intimacy_trend_cache_clear()
        return {"ok": True}

    @app.post("/api/merge-reviews/{review_id}/reject")
    async def reject_review(review_id: str, request: Request, _=Depends(api_auth)):
        user = _extract_user(request)
        ok = merge_service.reject_review(review_id, resolved_by=user)
        if audit_store and ok:
            _safe_audit(audit_store, user, "merge_review_reject", review_id)
        if not ok:
            raise HTTPException(status_code=400, detail="reject_failed_or_already_resolved")
        # W3-3E.2：reject 改变了 pending review 计数，digest 必须失效
        _intimacy_trend_cache_clear()
        return {"ok": True}

    @app.post("/api/merge-reviews/batch-approve")
    async def batch_approve_reviews(request: Request, _=Depends(api_auth)):
        """W3-3E.3：批量 approve 多个 review。

        Body: ``{"review_ids": ["rid1", "rid2", ...], "min_confidence": 0.85}``

        - ``min_confidence`` 可选门槛保护（默认 0.85）—— 即使运营全选，置信
          度低于阈值的也会被跳过，避免误操作
        - 部分失败不阻塞整体：每条独立 approve，失败计入 ``failed`` 列表
        - 一次审计日志（聚合 summary）
        """
        try:
            body = await request.json()
        except Exception:
            body = {}
        review_ids = body.get("review_ids") or []
        min_conf = float(body.get("min_confidence", 0.85))
        if not isinstance(review_ids, list):
            raise HTTPException(status_code=400, detail="review_ids must be a list")
        if not review_ids:
            raise HTTPException(status_code=400, detail="review_ids cannot be empty")

        user = _extract_user(request)
        approved: list = []
        failed: list = []
        skipped_low_conf: list = []
        # 先拉一次 pending 列表做置信度筛选（一次 SQL，避免 N+1）
        pending_map = {
            r["review_id"]: r
            for r in contacts_store.list_pending_reviews(limit=1000)
        }
        for rid in review_ids:
            r = pending_map.get(rid)
            if r is None:
                failed.append({"review_id": rid, "reason": "not_pending"})
                continue
            if float(r.get("confidence", 0)) < min_conf:
                skipped_low_conf.append({
                    "review_id": rid,
                    "confidence": r.get("confidence"),
                })
                continue
            ok = merge_service.approve_review(rid, resolved_by=user)
            if ok:
                approved.append(rid)
            else:
                failed.append({"review_id": rid, "reason": "approve_failed"})
        if audit_store:
            _safe_audit(
                audit_store, user, "merge_review_batch_approve",
                f"approved={len(approved)} skipped={len(skipped_low_conf)} "
                f"failed={len(failed)} min_conf={min_conf}",
            )
        # 写后失效（任何成功 approve 都改变拓扑）
        if approved:
            _intimacy_trend_cache_clear()
        return {
            "ok": True,
            "approved": approved,
            "skipped_low_conf": skipped_low_conf,
            "failed": failed,
            "min_confidence": min_conf,
        }

    # ── W3-3L.1：平台身份查询 ────────────────────────────
    @app.get("/api/channel-identities/lookup")
    async def lookup_channel_identity(
        channel: str,
        external_id: str,
        account_id: str = "",
        _=Depends(api_auth),
    ):
        """W3-3L.1：根据平台 ID 查找联系人。

        参数：
          ``channel``      平台名称（messenger / line / telegram）
          ``external_id``  该平台的用户 ID
          ``account_id``   可选，限定归属哪个运营账号；空则只按 channel+external_id 匹配

        返回：找到 → 200 ``{found, contact, journey, channel_identity}``；
               未找到 → 200 ``{found: false}``（非 404，方便调用方判断）
        """
        if account_id:
            ci = contacts_store.get_ci_by_external(channel, account_id, external_id)
        else:
            # 不指定 account_id 时，只按 channel+external_id 查（取最早建立的一个）
            with contacts_store._lock:  # noqa: SLF001
                row = contacts_store._conn.execute(  # noqa: SLF001
                    "SELECT * FROM channel_identities "
                    "WHERE channel=? AND external_id=? ORDER BY linked_at ASC LIMIT 1",
                    (channel, external_id),
                ).fetchone()
            from src.contacts.store import _row_to_ci
            ci = _row_to_ci(row) if row else None
        if ci is None:
            return {"found": False}
        contact = contacts_store.get_contact(ci.contact_id)
        journey = contacts_store.get_journey_by_contact(ci.contact_id)
        return {
            "found": True,
            "contact": contact.to_dict() if contact else None,
            "journey": _journey_to_dict(journey) if journey else None,
            "channel_identity": ci.to_dict(),
        }

    # ── W3-3L.3：Admin 手动关联 channel identity ────────
    @app.post("/api/contacts/{contact_id}/link-channel")
    async def admin_link_channel(
        contact_id: str,
        request: Request,
        _=Depends(api_auth),
    ):
        """W3-3L.3：手动把一个 ChannelIdentity 迁移到指定 Contact。

        Body: ``{"channel_identity_id": "...", "note": "optional"}``

        适用场景：运营确认两个平台身份属同一人，
        不想走完整 merge-review 流程时的快速通道。
        """
        try:
            body = await request.json()
        except Exception:
            body = {}
        ci_id = (body.get("channel_identity_id") or "").strip()
        if not ci_id:
            raise HTTPException(status_code=422, detail="channel_identity_id_required")
        tgt = contacts_store.get_contact(contact_id)
        if tgt is None:
            raise HTTPException(status_code=404, detail="contact_not_found")
        ci = contacts_store.get_channel_identity(ci_id)
        if ci is None:
            raise HTTPException(status_code=404, detail="channel_identity_not_found")
        if ci.contact_id == contact_id:
            return {"ok": True, "changed": False, "reason": "already_linked"}
        ok = contacts_store.relink_channel_identity(
            ci_id=ci_id,
            new_contact_id=contact_id,
            linked_via="manual",
            attribution_confidence=1.0,
        )
        user = _extract_user(request)
        if audit_store:
            _safe_audit(
                audit_store, user, "admin_link_channel",
                f"ci={ci_id} -> contact={contact_id} note={body.get('note', '')}",
            )
        return {"ok": True, "changed": ok}

    # ── 漏斗统计 & Journey 详情 ───────────────────────────
    @app.get("/api/funnel/stats")
    async def funnel_stats(channel: str = "", _=Depends(api_auth)):
        """漏斗统计。

        Query params:
            channel: 可选，按 channel 过滤 by_stage（messenger/line/
                telegram/mobile）。其他字段（total_contacts /
                by_channel / multi_platform）始终是全平台总览，不受
                channel 过滤影响——这是设计意图：让 UI 一眼看到
                "局部 funnel 阶段分布 + 全局生态宏观"。

        Returns:
            ``{total_contacts, by_stage, by_channel, multi_platform, scope}``
            ``scope`` 字段固定返回，前端用来区分当前展示的是全部还是
            某 channel（即使 by_stage 空也能知道是不是过滤后真无数据）。
        """
        from src.contacts.models import VALID_CHANNELS  # 延迟 import 避循环
        ch = (channel or "").strip().lower() or None
        if ch is not None and ch not in VALID_CHANNELS:
            raise HTTPException(
                status_code=400,
                detail=f"invalid channel '{channel}', must be one of {sorted(VALID_CHANNELS)}",
            )
        multi = contacts_store.count_multi_platform_contacts()
        return {
            "total_contacts": contacts_store.count_contacts(),
            "by_stage": contacts_store.count_journeys_by_stage(channel=ch),
            "by_channel": contacts_store.count_channel_identities_by_channel(),
            "multi_platform": multi,  # W3-3L.2
            "scope": ch or "all",     # Q4: 让前端知道当前是哪种 scope
        }

    @app.get("/api/funnel/timeseries")
    async def funnel_timeseries(
        days: int = 30, channel: str = "", _=Depends(api_auth),
    ):
        """B1：漏斗时序——按天聚合的 stage 进入流量 + 关键转化率。

        Query params:
            days: 1~365，默认 30。回看天数（含今天）。
            channel: 同 ``/api/funnel/stats``，可选过滤；'' 或缺省 = all。

        Returns: ``{days, channel/scope, series}``
            series 是每天一个 dict：
              ``{day, by_stage: {ENGAGED: n, HANDOFF_SENT: m, ...},
                 rates: {engaged_rate, handoff_rate, line_add_rate}}``
            rates 用当天的流量计算（"今天有多少 ENGAGED / 今天有多少
            INITIAL" 这种），前端可视化时再做 7 日滑动平均更稳定。

        设计：
          - 数据是"进入流量"而不是"存量"——更直观运营关心"今天进了多少"
          - 由 ``journey_events.event_type='stage_change'`` 重放，不依赖
            额外快照表或 cron job
          - 当天没有任何 stage_change 事件时，by_stage 为 ``{}``，rates
            为 ``null``，前端要做"无数据"渲染
        """
        from src.contacts.models import VALID_CHANNELS  # 延迟 import 避循环
        ch = (channel or "").strip().lower() or None
        if ch is not None and ch not in VALID_CHANNELS:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"invalid channel '{channel}', must be one of "
                    f"{sorted(VALID_CHANNELS)}"
                ),
            )
        try:
            d = int(days)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="days must be int")
        if d < 1 or d > 365:
            raise HTTPException(
                status_code=400, detail="days must be 1..365",
            )

        raw = contacts_store.count_stage_transitions_by_day(
            days=d, channel=ch,
        )
        # 计算每天的转化率（同天分母分子，无跨天前后对齐——B1 简化版本）
        # 注意：HANDOFF_SENT 的分母用"当天 ENGAGED 进入数"，是日内转化率；
        #   实际业务里 ENGAGED→HANDOFF_SENT 有时间差，所以这是"流量近似"。
        #   前端折线图加 7d 移动平均后能看出趋势。
        def _pct(num: int, den: int) -> Optional[float]:
            if den <= 0:
                return None
            return round(num / den * 100, 1)

        series = []
        for item in raw:
            by = item.get("by_stage") or {}
            engaged = int(by.get("ENGAGED", 0))
            handoff = int(by.get("HANDOFF_SENT", 0))
            line_added = int(by.get("LINE_ADDED", 0))
            bonded = int(by.get("BONDED", 0))
            series.append({
                "day": item["day"],
                "by_stage": by,
                "rates": {
                    # 互动率：当天 ENGAGED / 当天 INITIAL（流量比）
                    "engaged_rate": _pct(engaged, int(by.get("INITIAL", 0))),
                    # 引流率：当天 HANDOFF_SENT / 当天 ENGAGED
                    "handoff_rate": _pct(handoff, engaged),
                    # 加友率：当天 LINE_ADDED / 当天 HANDOFF_SENT
                    "line_add_rate": _pct(line_added, handoff),
                    # 成交率：当天 BONDED / 当天 LINE_ADDED
                    "bonded_rate": _pct(bonded, line_added),
                },
            })

        return {
            "days": d,
            "scope": ch or "all",
            "series": series,
        }

    # ── B2: KPI 漏斗告警 ──────────────────────────────────
    @app.get("/api/funnel/alerts")
    async def list_funnel_alerts(
        limit: int = 50,
        unacked_only: bool = False,
        _=Depends(api_auth),
    ):
        """B2：列出 KPI 漏斗告警。

        Query params:
            limit: 最多返回条数（默认 50，最大 200）。
            unacked_only: 仅返回未确认（acked=0）的告警（默认 false）。

        Returns:
            ``{items: [{id, ts, kind, severity, message, detail, acked,
              acked_at, acked_by}], unacked_count}``
        """
        limit = max(1, min(int(limit), 200))
        items = contacts_store.list_kpi_alerts(limit=limit, unacked_only=unacked_only)
        unacked_count = contacts_store.count_unacked_kpi_alerts()
        return {"items": items, "unacked_count": unacked_count}

    @app.post("/api/funnel/alerts/{alert_id}/ack")
    async def ack_funnel_alert(
        alert_id: int, request: Request, _=Depends(api_auth),
    ):
        """B2：确认单条 KPI 告警。"""
        user = _extract_user(request)
        ok = contacts_store.ack_kpi_alert(alert_id, acked_by=user or "")
        if not ok:
            raise HTTPException(
                status_code=404,
                detail="alert_not_found_or_already_acked",
            )
        return {"ok": True, "alert_id": alert_id}

    @app.post("/api/funnel/alerts/ack-all")
    async def ack_all_funnel_alerts(request: Request, _=Depends(api_auth)):
        """B2：批量确认所有未读 KPI 告警。"""
        user = _extract_user(request)
        count = contacts_store.ack_all_kpi_alerts(acked_by=user or "")
        return {"ok": True, "acked_count": count}

    @app.get("/api/journeys/{journey_id}")
    async def journey_detail(journey_id: str, _=Depends(api_auth)):
        j = contacts_store.get_journey(journey_id)
        if not j:
            raise HTTPException(status_code=404, detail="journey_not_found")
        return {"journey": _journey_to_dict(j)}

    # ── 可选：intimacy 重算 ───────────────────────────────
    if intimacy_engine is not None:
        @app.post("/api/journeys/{journey_id}/intimacy/refresh")
        async def refresh_intimacy(journey_id: str, _=Depends(api_auth)):
            j = contacts_store.get_journey(journey_id)
            if not j:
                raise HTTPException(status_code=404, detail="journey_not_found")
            bd = intimacy_engine.refresh_journey_intimacy(journey_id)
            return {"journey_id": journey_id, "intimacy": bd.to_dict()}

        @app.get("/api/journeys/{journey_id}/intimacy-history")
        async def intimacy_history(
            journey_id: str, days: int = 30, _=Depends(api_auth),
        ):
            """W3-3C.2：用 IntimacyEngine.compute_intimacy(now=day_ts) 即时重放历史。

            事件流是真相，不需要单独的 history 表。每天一个数据点（"日终" 23:59）。
            性能：O(days × len(events))。一般 <100 events × 30 days = 3000 次轻量循环。

            响应：
                {
                  "journey_id": ..., "days": 30,
                  "series": [{"day": "2026-05-01", "ts": 1714579199, "score": 42.3,
                              "stage": "warming", "stage_label": "试探/升温"}, ...]
                }
            """
            import time as _t
            j = contacts_store.get_journey(journey_id)
            if not j:
                raise HTTPException(status_code=404, detail="journey_not_found")
            days = max(1, min(int(days), 90))  # 防超大查询
            now = int(_t.time())
            day_secs = 86400
            # 对齐到当地"日终"，但用 UTC 简化（运营看趋势不需要绝对精度）
            today_end = (now // day_secs) * day_secs + day_secs - 1
            series = []
            for i in range(days - 1, -1, -1):
                day_ts = today_end - i * day_secs
                bd = intimacy_engine.compute_intimacy(journey_id, now=day_ts)
                stage_info = _intimacy_stage_info(bd.score)
                series.append({
                    "day": _t.strftime("%Y-%m-%d", _t.gmtime(day_ts)),
                    "ts": day_ts,
                    "score": bd.score,
                    "stage": stage_info["stage"] if stage_info else None,
                    "stage_label": stage_info["label"] if stage_info else None,
                })
            return {"journey_id": journey_id, "days": days, "series": series}

        @app.get("/api/relations/intimacy-trend")
        async def intimacy_trend_global(
            days: int = 30, top_n: int = 200, _=Depends(api_auth),
        ):
            """W3-3C.3 + W3-3D.3：全域 intimacy 日均 — 取最近活跃 top_n journey 重放历史均值。

            性能优化（W3-3D.3）：
              - 旧实现：N+1 查询，O(days × top_n) 次 SQL = 30×200 = 6000 次
              - 新实现：1 次 SQL 拿 jids + 1 次批量 SQL 拿全部 events + 内存重放
                       = 2 次 SQL，~30x 提速
              - 60 秒 TTL 单值 cache（key=(days, top_n, day_bucket)）
            """
            import time as _t
            days = max(1, min(int(days), 60))
            top_n = max(10, min(int(top_n), 1000))
            now = int(_t.time())
            day_secs = 86400
            today_end = (now // day_secs) * day_secs + day_secs - 1

            # ── 60s TTL cache ────────────────────────────────────
            # admin.py::_schedule_status_cache 同款轻量实现，避免引入新依赖
            cache_key = (days, top_n, today_end)
            cached = _intimacy_trend_cache_get(cache_key)
            if cached is not None:
                return cached

            # 1 次 SQL 取近 days 天有活动的 journey
            cutoff = now - days * day_secs
            with contacts_store._lock:  # noqa: SLF001
                rows = contacts_store._conn.execute(  # noqa: SLF001
                    "SELECT j.journey_id FROM journeys j "
                    "JOIN contacts c ON c.contact_id = j.contact_id "
                    "WHERE c.last_active_at >= ? "
                    "ORDER BY c.last_active_at DESC LIMIT ?",
                    (cutoff, top_n),
                ).fetchall()
            jids = [r[0] for r in rows]

            # 1 次批量 SQL 取所有 events（W3-3D.2）
            events_by_jid = contacts_store.list_events_for_journeys(
                jids, limit_per_journey=500,
            )

            # 内存里重放：每天 × 每 journey，调 from_events 纯函数版（W3-3D.1）
            from src.skills.intimacy_engine import IntimacyEngine
            series = []
            for i in range(days - 1, -1, -1):
                day_ts = today_end - i * day_secs
                scores = []
                for jid in jids:
                    bd = IntimacyEngine.compute_intimacy_from_events(
                        events_by_jid.get(jid, []), now=day_ts,
                    )
                    if bd.days_since_last_msg != float("inf"):
                        scores.append(bd.score)
                avg = round(sum(scores) / len(scores), 1) if scores else 0.0
                series.append({
                    "day": _t.strftime("%Y-%m-%d", _t.gmtime(day_ts)),
                    "ts": day_ts,
                    "avg_intimacy": avg,
                    "active_count": len(scores),
                })
            result = {
                "days": days,
                "sample_size": len(jids),
                "series": series,
            }
            _intimacy_trend_cache_put(cache_key, result)
            return result

        @app.get("/api/relations/digest")
        async def relations_digest(_=Depends(api_auth)):
            """W3-3E.4 + 3E.6：运营聚合数据摘要 — 一次拉所有 actionable 信号。

            响应包含：
              - ``stats``：pending reviews / reunion candidates / 今日活跃数等核心数字
              - ``trend_delta``：近 7 天 intimacy 日均与上 7 天的环比（百分点）
              - ``health_score``：关系健康度综合分（0-100）+ grade
              - ``insights``：人话解读（中文段落，最长 ~200 字）
              - ``text_summary``：可直接推送 Slack/Telegram 的多行文本

            数据源：复用 trend 端点的事件流重放 + funnel SQL 聚合（避免 N+1）。
            120s TTL 缓存（聚合数据短期不变，写后失效也会清空）。
            """
            import time as _t
            now = int(_t.time())
            day_secs = 86400
            today_end = (now // day_secs) * day_secs + day_secs - 1
            cache_key = ("digest", today_end)
            cached = _relations_digest_cache.get(cache_key)
            if cached is not None:
                return cached

            # ── 核心数字（1 次 SQL）─────────────────────────
            stats: Dict[str, Any] = {}
            with contacts_store._lock:  # noqa: SLF001
                cur = contacts_store._conn  # noqa: SLF001
                stats["total_contacts"] = cur.execute(
                    "SELECT COUNT(*) FROM contacts"
                ).fetchone()[0]
                stats["active_today"] = cur.execute(
                    "SELECT COUNT(*) FROM contacts WHERE last_active_at >= ?",
                    (now - day_secs,),
                ).fetchone()[0]
                stats["active_7d"] = cur.execute(
                    "SELECT COUNT(*) FROM contacts WHERE last_active_at >= ?",
                    (now - 7 * day_secs,),
                ).fetchone()[0]
                stats["pending_reviews"] = cur.execute(
                    "SELECT COUNT(*) FROM merge_review_queue WHERE status='pending'"
                ).fetchone()[0]
                stats["high_conf_reviews"] = cur.execute(
                    "SELECT COUNT(*) FROM merge_review_queue "
                    "WHERE status='pending' AND confidence >= 0.85"
                ).fetchone()[0]
                # reunion 候选：曾达 warming+ 但已沉默衰减回 stranger 区间
                # 启发式：funnel_stage in HANDOFF_SENT+ AND intimacy < 25
                stats["reunion_candidates"] = cur.execute(
                    "SELECT COUNT(*) FROM journeys j "
                    "WHERE j.funnel_stage IN ('HANDOFF_SENT','LINE_ADDED',"
                    "'LINE_ACCEPTED','LINE_ENGAGED','BONDED') "
                    "AND j.intimacy_score < 25"
                ).fetchone()[0]
                # 阶段分布
                fs_rows = cur.execute(
                    "SELECT funnel_stage, COUNT(*) FROM journeys GROUP BY funnel_stage"
                ).fetchall()
                stats["by_funnel_stage"] = {
                    str(r[0] or "INITIAL"): int(r[1] or 0) for r in fs_rows
                }

            # ── W3-3L.2：多平台统计（独立锁，不污染上方事务）────
            try:
                multi_platform = contacts_store.count_multi_platform_contacts()
                stats["multi_platform_contacts"] = multi_platform["multi_platform_contacts"]
                stats["by_channel_combo"] = multi_platform["by_channel_combo"]
            except Exception as _e:
                logger.debug("count_multi_platform_contacts failed: %s", _e)
                stats["multi_platform_contacts"] = 0
                stats["by_channel_combo"] = {}

            # ── 趋势 delta（复用 trend 计算）─────────────────
            trend_delta: Dict[str, Any] = {"available": False}
            if intimacy_engine is not None:
                try:
                    # 取 top_n=200, days=14 跑短窗口（不污染 trend 缓存的 30 天 key）
                    cutoff = now - 14 * day_secs
                    with contacts_store._lock:  # noqa: SLF001
                        rows = contacts_store._conn.execute(  # noqa: SLF001
                            "SELECT j.journey_id FROM journeys j "
                            "JOIN contacts c ON c.contact_id = j.contact_id "
                            "WHERE c.last_active_at >= ? "
                            "ORDER BY c.last_active_at DESC LIMIT 200",
                            (cutoff,),
                        ).fetchall()
                    jids = [r[0] for r in rows]
                    events_by_jid = contacts_store.list_events_for_journeys(
                        jids, limit_per_journey=500,
                    )
                    from src.skills.intimacy_engine import IntimacyEngine
                    recent_avgs = []
                    prev_avgs = []
                    for i in range(14):
                        day_ts = today_end - i * day_secs
                        scores = []
                        for jid in jids:
                            bd = IntimacyEngine.compute_intimacy_from_events(
                                events_by_jid.get(jid, []), now=day_ts,
                            )
                            if bd.days_since_last_msg != float("inf"):
                                scores.append(bd.score)
                        avg = sum(scores) / len(scores) if scores else 0.0
                        if i < 7:
                            recent_avgs.append(avg)
                        else:
                            prev_avgs.append(avg)
                    recent7 = sum(recent_avgs) / 7 if recent_avgs else 0.0
                    prev7 = sum(prev_avgs) / 7 if prev_avgs else 0.0
                    delta_pct = (
                        ((recent7 - prev7) / prev7 * 100) if prev7 > 0 else 0.0
                    )
                    trend_delta = {
                        "available": True,
                        "recent_7d_avg": round(recent7, 1),
                        "prev_7d_avg": round(prev7, 1),
                        "delta_pct": round(delta_pct, 1),
                        "sample_size": len(jids),
                    }
                except Exception as e:
                    logger.debug("digest trend delta failed: %s", e)

            # ── W3-3E.6：关系健康度综合分（0-100 + grade）────
            # 设计：5 个维度加权，每维都是"越大越健康"或"越小越健康"翻转
            #   1. 活跃率（active_7d / total）           权重 25
            #   2. trend delta（环比正向）               权重 20
            #   3. reunion 比率反（reunion / total，越低越好）  权重 20
            #   4. merge backlog 反（pending / total，越低越好）权重 15
            #   5. 漏斗深度（LINE+ stage 占比）          权重 20
            def _safe_div(a, b):
                return (a / b) if b else 0.0
            total = max(stats["total_contacts"], 1)
            active_rate = _safe_div(stats["active_7d"], total)
            # delta_pct 范围 [-100, +100]，映射到 [0, 1]
            delta_score = max(0.0, min(1.0, (trend_delta.get("delta_pct", 0) + 50) / 100))
            reunion_inv = 1.0 - min(1.0, _safe_div(stats["reunion_candidates"], total) * 4)
            merge_inv = 1.0 - min(1.0, _safe_div(stats["pending_reviews"], total) * 10)
            line_stages = sum(
                v for k, v in stats["by_funnel_stage"].items()
                if k in ("LINE_ADDED", "LINE_ACCEPTED", "LINE_ENGAGED",
                          "BONDED", "CONVERTED")
            )
            funnel_depth = _safe_div(line_stages, total)
            health = (
                active_rate * 25
                + delta_score * 20
                + reunion_inv * 20
                + merge_inv * 15
                + funnel_depth * 20
            )
            health = round(min(100.0, max(0.0, health)), 1)
            if health >= 80:
                grade = "A"
            elif health >= 65:
                grade = "B"
            elif health >= 45:
                grade = "C"
            else:
                grade = "D"
            health_score = {
                "score": health,
                "grade": grade,
                "components": {
                    "active_rate": round(active_rate * 100, 1),
                    "trend_delta_pct": trend_delta.get("delta_pct", 0),
                    "reunion_ratio": round(
                        _safe_div(stats["reunion_candidates"], total) * 100, 1,
                    ),
                    "merge_backlog_ratio": round(
                        _safe_div(stats["pending_reviews"], total) * 100, 1,
                    ),
                    "funnel_depth": round(funnel_depth * 100, 1),
                },
            }

            # ── W3-3E.4：自动洞察（人话解读）────────────────
            insights: list = []
            if trend_delta.get("available"):
                d = trend_delta["delta_pct"]
                if d > 5:
                    insights.append(
                        f"📈 近 7 天 intimacy 日均环比 +{d}%（{trend_delta['prev_7d_avg']} → "
                        f"{trend_delta['recent_7d_avg']}），关系整体在升温。"
                    )
                elif d < -5:
                    insights.append(
                        f"📉 近 7 天 intimacy 日均环比 {d}%（{trend_delta['prev_7d_avg']} → "
                        f"{trend_delta['recent_7d_avg']}），关系整体在降温——"
                        f"建议加大 reactivation 频率或检查最近策略变更。"
                    )
                else:
                    insights.append(
                        f"➡️ 近 7 天 intimacy 日均 {trend_delta['recent_7d_avg']}，"
                        f"环比基本持平。"
                    )
            if stats["reunion_candidates"] > 0:
                ratio = round(_safe_div(stats["reunion_candidates"], total) * 100, 1)
                insights.append(
                    f"🕒 当前 {stats['reunion_candidates']} 位用户曾深度互动后沉默"
                    f"（占比 {ratio}%）——这是 reactivation 的优先目标。"
                )
            if stats["high_conf_reviews"] >= 3:
                insights.append(
                    f"🔗 {stats['high_conf_reviews']} 条高置信合并候选待审核（≥0.85），"
                    f"建议批量审核——可用 batch-approve 一键处理。"
                )
            elif stats["pending_reviews"] >= 10:
                insights.append(
                    f"🔗 待审核合并 {stats['pending_reviews']} 条堆积，"
                    f"建议尽快清理避免 backlog 持续增长。"
                )
            if grade in ("C", "D"):
                insights.append(
                    f"⚠️ 关系健康度 {grade} 级（{health}/100）——多个维度同时承压，"
                    f"建议优先处理上面的具体告警。"
                )
            elif grade == "A":
                insights.append(
                    f"✅ 关系健康度 A 级（{health}/100）—— 各维度均衡良好。"
                )
            if not insights:
                insights.append("✅ 当前无显著告警。")

            # ── 文本摘要（可直接 Slack/Telegram 推送）─────
            lines = [f"📊 关系运营日报（{_t.strftime('%Y-%m-%d', _t.localtime(now))}）", ""]
            lines.append(
                f"健康度: {health}/100 ({grade}) · 总用户 {stats['total_contacts']} · "
                f"今日活跃 {stats['active_today']} · 7 日活跃 {stats['active_7d']}"
            )
            if trend_delta.get("available"):
                d = trend_delta["delta_pct"]
                arrow = "📈" if d > 1 else ("📉" if d < -1 else "➡️")
                lines.append(
                    f"亲密度趋势: {arrow} 近 7 天均值 {trend_delta['recent_7d_avg']}"
                    f"（环比 {'+' if d >= 0 else ''}{d}%）"
                )
            if stats["pending_reviews"]:
                lines.append(
                    f"待审核合并: {stats['pending_reviews']} 条"
                    f"（高置信 {stats['high_conf_reviews']}）"
                )
            if stats["reunion_candidates"]:
                lines.append(f"重逢候选: {stats['reunion_candidates']} 位")
            # W3-3L.2：多平台覆盖（仅有跨平台用户时才展示）
            if stats.get("multi_platform_contacts"):
                combo_str = "、".join(
                    f"{k}({v})" for k, v in
                    sorted(stats["by_channel_combo"].items(), key=lambda x: -x[1])[:3]
                )
                lines.append(
                    f"跨平台: {stats['multi_platform_contacts']} 位用户覆盖 2+ 平台"
                    + (f"（{combo_str}）" if combo_str else "")
                )
            # W3-3G：草稿质量摘要（仅在有 sent 草稿时才显示，避免冷启动期面板被空数据填）
            try:
                draft_q = contacts_store.draft_quality_stats(days=7)
            except Exception as e:
                logger.debug("draft_quality_stats failed: %s", e)
                draft_q = {
                    "window_days": 7, "generated": 0, "sent": 0,
                    "evaluated": 0, "success": 0, "success_rate": None,
                    "by_lang": {}, "by_variant": {},
                }
            if draft_q.get("sent"):
                rate_str = (
                    f"{int(round(draft_q['success_rate'] * 100))}%"
                    if draft_q.get("success_rate") is not None else "评估中"
                )
                lines.append(
                    f"草稿质量(7d): 生成 {draft_q['generated']} / 发出 {draft_q['sent']} "
                    f"/ 已评估 {draft_q['evaluated']} / 成功率 {rate_str}"
                )
                # 草稿质量 insight：用 Wilson 下界判断（避免低样本虚高）
                # 阈值 0.30：即使乐观情况下 95% CI 都低于 30% 才告警
                lower = draft_q.get("success_rate_lower")
                if (draft_q["evaluated"] >= 5
                        and lower is not None and lower < 0.30):
                    insights.append(
                        f"✍️ 重逢草稿成功率仅 {rate_str}（{draft_q['evaluated']} 条评估，"
                        f"95% CI 下界 {int(round(lower * 100))}%），"
                        f"建议复盘 prompt 文本或调整候选筛选阈值。"
                    )
                # W3-3I.1：A/B 显著性 — 95% CI 不重叠才宣布优胜者
                try:
                    winner = ContactStore.pick_winning_variant(
                        draft_q.get("by_variant") or {},
                    )
                except Exception as _we:
                    logger.debug("pick_winning_variant failed: %s", _we)
                    winner = None
                if winner:
                    draft_q["winning_variant"] = winner
                    insights.append(
                        f"🏆 prompt {winner['winner']} 显著优于 {winner['runner_up']}："
                        f"成功率 {int(round(winner['winner_rate']*100))}% vs "
                        f"{int(round(winner['runner_up_rate']*100))}% "
                        f"(差 +{winner['gap_pct']}pct, "
                        f"样本 {winner['winner_evaluated']}/{winner['runner_up_evaluated']})。"
                        f"考虑把 default_variant 切到 {winner['winner']}。"
                    )
            lines.append("")
            lines.extend(insights)
            text_summary = "\n".join(lines)

            result = {
                "generated_at": _t.strftime("%Y-%m-%d %H:%M:%S",
                                             _t.localtime(now)),
                "stats": stats,
                "trend_delta": trend_delta,
                "health_score": health_score,
                "draft_quality": draft_q,
                "insights": insights,
                "text_summary": text_summary,
            }
            _relations_digest_cache.put(cache_key, result)
            return result

        @app.post("/api/relations/digest/push")
        async def push_relations_digest(
            request: Request,
            only_winner: bool = False,
            _=Depends(api_auth),
        ):
            """W3-3E.5 + W3-3J.2：手动触发把当前 digest 推到 webhook。

            ``only_winner=true``：仅当 digest 里有 ``winning_variant`` 时才推，
            用于定期任务"有显著赢家才通知运营，否则静默"。
            返回 ``{"ok": True, "pushed": false, "reason": "no_winner"}`` 时
            表示条件不满足、跳过推送（非错误）。
            """
            import time as _t
            today_end = (int(_t.time()) // 86400) * 86400 + 86400 - 1
            cached = _relations_digest_cache.get(("digest", today_end))
            if cached is None:
                raise HTTPException(
                    status_code=400,
                    detail="digest_not_cached_yet_call_GET_first",
                )
            # W3-3J.2：only_winner 过滤 —— 先于 webhook check，无 winner 时静默跳过
            # 无需 webhook 也能正常返回"无需推送"，节省 ops 噪音
            if only_winner:
                has_winner = bool(
                    (cached.get("draft_quality") or {}).get("winning_variant")
                )
                if not has_winner:
                    return {
                        "ok": True, "pushed": False, "reason": "no_winner",
                    }
            # 到这里才真正要推送，检查 webhook 是否挂载
            if fire_webhook is None:
                raise HTTPException(
                    status_code=503,
                    detail="webhook_not_wired",
                )
            user = _extract_user(request)
            summary = cached.get("text_summary", "(no summary)")
            try:
                await fire_webhook(
                    "relations_digest", user or "manual",
                    "relations_digest", summary[:1500],
                )
            except Exception as e:
                logger.warning("relations digest webhook push failed: %s", e)
                raise HTTPException(status_code=502, detail=f"push_failed: {e}")
            if audit_store:
                _safe_audit(
                    audit_store, user, "relations_digest_push",
                    f"grade={cached.get('health_score', {}).get('grade')}"
                    f" only_winner={only_winner}",
                )
            return {
                "ok": True,
                "pushed": True,
                "summary_length": len(summary),
            }

    # ── Phase P：单人关系健康卡 + 流失预警榜 ───────────────
    # 复用 IntimacyEngine 事件重放（now / now-7d 两快照算趋势）+ 纯函数打分器。
    # 与全域 /api/relations/digest 的区别：digest 全盘聚合，本组逐联系人 + 排序 + 给建议。
    if intimacy_engine is not None:
        from src.contacts.relationship_health import (
            ContactHealthSignals as _CHS,
            score_contact_health as _score_health,
        )
        from src.contacts.identity_bridge import conversation_ids_for_identities as _conv_ids_for
        from src.contacts.inbox_enrichment import (
            health_board_sort_key as _board_sort_key,
            inbox_enrichment_batch_for_journeys as _inbox_batch,
            inbox_enrichment_for_conv_ids as _inbox_for_ids,
        )
        from src.skills.intimacy_engine import IntimacyEngine as _IE

        _SEVEN_D = 7 * 86400

        def _care_store():
            return getattr(app.state, "care_schedule_store", None)

        def _inbox_store():
            return getattr(app.state, "inbox_store", None)

        def _conv_ids_for_journey(j, *, extra_keys=None):
            """Phase Q：contact 的 channel_identities 反推候选 conversation_id（+ 显式补充）。

            三路合并（去重保序）：
            1. 前向镜像 ``conversation_ids_for_identities``（external_id≡chat_key 的平台）；
            2. Q 延伸·writeback 反查 ``list_conversation_ids_for_contact``（ingest 已回写时）；
            3. Q 延伸·前向后缀匹配 ``find_conversation_ids_by_external``（读 inbox 真实 chat_key，
               补 Messenger/WA 前缀差异，不依赖 writeback flag）。
            """
            keys = list(extra_keys or [])
            contact_id = getattr(j, "contact_id", "") or ""
            try:
                if contact_id:
                    cis = contacts_store.list_channel_identities_of(contact_id)
                    keys = _conv_ids_for(cis) + keys
                    inbox = _inbox_store()
                    if inbox is not None:
                        keys.extend(inbox.list_conversation_ids_for_contact(contact_id))
                        for ci in cis:
                            ch = getattr(ci, "channel", "")
                            ext = getattr(ci, "external_id", "")
                            if not ch or not ext:
                                continue
                            acc = getattr(ci, "account_id", "") or "default"
                            keys.extend(
                                inbox.find_conversation_ids_by_external(ch, acc, ext))
            except Exception:
                logger.debug("conv_ids CI resolve failed", exc_info=True)
            return list(dict.fromkeys(keys))  # 去重保序

        def _care_pending_for_keys(keys):
            cs = _care_store()
            if cs is None or not keys:
                return 0
            try:
                return int(sum(cs.pending_counts_by_contacts(list(keys)).values()))
            except Exception:
                return 0

        def _inbox_enrichment(conv_ids):
            """Phase R：跨域富集——从 contact 的会话取 inbox 侧语境（最近活跃那条为主）。"""
            store = _inbox_store()
            if store is None or not conv_ids:
                return None
            try:
                conv_map = store.get_conversations_for_ids(conv_ids)
                meta_map = store.get_conv_meta_for_ids(list(conv_map.keys()))
                return _inbox_for_ids(conv_ids, conv_map, meta_map, compact=False)
            except Exception:
                logger.debug("inbox enrichment lookup failed", exc_info=True)
                return None

        def _health_board_inbox_sort_enabled() -> bool:
            """R3：config companion.relations_health.health_board.inbox_sort_tiebreak（默认关）。"""
            cm = getattr(app.state, "config_manager", None)
            if cm is None:
                return False
            try:
                cfg = getattr(cm, "config", None) or {}
                hb = (
                    ((cfg.get("companion") or {}).get("relations_health") or {})
                    .get("health_board") or {}
                )
                return bool(hb.get("inbox_sort_tiebreak", False))
            except Exception:
                return False

        def _signals_from_events(events, *, now, funnel_stage,
                                 pending_care=0, recent_react=False):
            bd = _IE.compute_intimacy_from_events(events, now=now)
            bd_prev = _IE.compute_intimacy_from_events(events, now=now - _SEVEN_D)
            prev = (bd_prev.score
                    if bd_prev.days_since_last_msg != float("inf") else None)
            sig = _CHS(
                intimacy_score=bd.score,
                days_since_last_msg=bd.days_since_last_msg,
                prev_intimacy_score=prev,
                funnel_stage=funnel_stage or "INITIAL",
                turn_count_in=bd.turn_count_in,
                turn_count_out=bd.turn_count_out,
                pending_care=pending_care,
                has_recent_reactivation=recent_react,
            )
            return sig, bd

        @app.get("/api/relations/health/{journey_id}")
        async def relation_health_card(
            journey_id: str, contact_key: str = "", _=Depends(api_auth),
        ):
            """单人关系健康卡。

            Phase Q：自动从该 contact 的 channel_identities 反查 care `pending_care`，
            无需手传 contact_key；``contact_key`` 仍可作显式补充（叠加，不冲突）。
            """
            import time as _t
            j = contacts_store.get_journey(journey_id)
            if j is None:
                raise HTTPException(status_code=404, detail="journey_not_found")
            now = int(_t.time())
            conv_ids = _conv_ids_for_journey(
                j, extra_keys=([contact_key] if contact_key else None))
            pending = _care_pending_for_keys(conv_ids)
            events = contacts_store.list_events(journey_id, limit=500)
            sig, bd = _signals_from_events(
                events, now=now, funnel_stage=(j.funnel_stage or "INITIAL"),
                pending_care=pending)
            card = _score_health(sig)
            return {"ok": True, "journey_id": journey_id,
                    "card": card.as_dict(), "intimacy": bd.to_dict(),
                    "inbox": _inbox_enrichment(conv_ids)}

        @app.get("/api/relations/health-board")
        async def relation_health_board(
            limit: int = 20, risk: str = "", min_intimacy: float = 0.0,
            scan: int = 300, _=Depends(api_auth),
        ):
            """流失预警榜：按关系强度扫 top-N journey，逐个打健康分，排序输出最该干预的。

            排序：value_at_risk（高价值正流失）优先，再按健康分升序（越不健康越靠前）。
            R3（可选，config 开）：同分相邻行按 inbox 高流失 / 情绪恶化次级 tie-break。
            ``risk`` 可过滤 healthy/watch/at_risk/critical；``min_intimacy`` 过滤弱关系噪声。
            """
            import time as _t
            now = int(_t.time())
            lim = max(1, min(int(limit), 100))
            scan_n = max(lim, min(int(scan), 1000))
            try:
                with contacts_store._lock:  # noqa: SLF001
                    rows = contacts_store._conn.execute(  # noqa: SLF001
                        "SELECT j.journey_id, j.funnel_stage, j.contact_id FROM journeys j "
                        "WHERE j.intimacy_score >= ? "
                        "ORDER BY j.intimacy_score DESC LIMIT ?",
                        (float(min_intimacy), scan_n),
                    ).fetchall()
            except Exception as e:  # noqa: BLE001
                logger.debug("health-board query failed: %s", e)
                rows = []
            jids = [r[0] for r in rows]
            stage_by = {r[0]: (r[1] or "INITIAL") for r in rows}
            contact_by = {r[0]: (r[2] or "") for r in rows}
            events_by = contacts_store.list_events_for_journeys(
                jids, limit_per_journey=500) if jids else {}

            # Phase Q/R2：批量 CI 反查 conversation_id（care + inbox 共用）
            convkeys_by_contact: dict = {}
            if jids:
                try:
                    cids = [c for c in {contact_by.get(j) for j in jids} if c]
                    ci_map = (
                        contacts_store.list_channel_identities_for_contacts(cids)
                        if cids else {}
                    )
                    convkeys_by_contact = {
                        cid: _conv_ids_for(cilist) for cid, cilist in ci_map.items()
                    }
                    inbox = _inbox_store()
                    if inbox is not None:
                        for cid in {c for c in contact_by.values() if c}:
                            extra = inbox.list_conversation_ids_for_contact(cid)
                            if not extra:
                                continue
                            base = convkeys_by_contact.get(cid, [])
                            convkeys_by_contact[cid] = list(
                                dict.fromkeys(base + extra))
                except Exception:
                    logger.debug("health-board CI resolve failed", exc_info=True)

            # Phase Q：批量聚合 care pending
            pending_by_jid = {}
            cs = _care_store()
            if cs is not None and jids and convkeys_by_contact:
                try:
                    all_keys = [
                        k for ks in convkeys_by_contact.values() for k in ks]
                    care_counts = cs.pending_counts_by_contacts(
                        list(dict.fromkeys(all_keys)))
                    for jid in jids:
                        cid = contact_by.get(jid) or ""
                        keys = convkeys_by_contact.get(cid, [])
                        pending_by_jid[jid] = int(
                            sum(care_counts.get(k, 0) for k in keys))
                except Exception:
                    logger.debug("health-board care aggregate failed", exc_info=True)

            items = []
            for jid in jids:
                sig, bd = _signals_from_events(
                    events_by.get(jid, []), now=now,
                    funnel_stage=stage_by.get(jid, "INITIAL"),
                    pending_care=pending_by_jid.get(jid, 0))
                card = _score_health(sig)
                if risk and card.risk_level != risk:
                    continue
                d = bd.days_since_last_msg
                items.append({
                    "journey_id": jid,
                    **card.as_dict(),
                    "intimacy_score": bd.score,
                    "days_since_last_msg": (None if d == float("inf") else round(d, 1)),
                })
            inbox_sort = _health_board_inbox_sort_enabled()
            if inbox_sort and items:
                # R3：tie-break 需 inbox 信号参与排序 → 先批量富集全部候选再 sort
                inbox_all = _inbox_batch(
                    [it["journey_id"] for it in items],
                    contact_by, convkeys_by_contact, _inbox_store(), compact=True,
                )
                for it in items:
                    ib = inbox_all.get(it["journey_id"])
                    if ib:
                        it["inbox"] = ib
            items.sort(key=lambda c: _board_sort_key(c, inbox_tiebreak=inbox_sort))
            top = items[:lim]
            if not inbox_sort:
                # R2 默认：仅对最终上榜行富集（SQL 随 limit 而非 scan 增长）
                inbox_by = _inbox_batch(
                    [it["journey_id"] for it in top],
                    contact_by, convkeys_by_contact, _inbox_store(), compact=True,
                )
                for it in top:
                    ib = inbox_by.get(it["journey_id"])
                    if ib:
                        it["inbox"] = ib
            return {"ok": True, "items": top,
                    "scanned": len(jids), "count": min(len(items), lim),
                    "inbox_sort_tiebreak": inbox_sort}

    # ── Q 延伸·回填状态查询 + 按需 dry_run 触发 ───────────────
    @app.get("/api/relations/backfill-status")
    async def relation_backfill_status(_=Depends(api_auth)):
        """最近一次 contact_id 回填结果（启动自动跑 or 手动触发的）。"""
        st = getattr(app.state, "last_contact_backfill", None)
        if not st:
            return {"ok": True, "status": "not_run", "result": None}
        return {"ok": True, "status": "ok", "result": st}

    @app.post("/api/relations/backfill-run")
    async def relation_backfill_run(
        dry_run: bool = True, limit: int = 200, platform: str = "",
        _=Depends(api_auth),
    ):
        """按需触发 contact_id 回填。**默认 dry_run**（只评估命中率，不写库）。

        显式 ``dry_run=false`` 才真写。结果同时缓存到 app.state 供 status 端点读。
        """
        inbox = getattr(app.state, "inbox_store", None)
        if inbox is None:
            raise HTTPException(status_code=503, detail="inbox_store_unavailable")
        from starlette.concurrency import run_in_threadpool
        from src.contacts.contact_backfill import backfill_contact_ids
        from src.contacts.identity_bridge import resolve_contact_id

        def _resolver(p, a, c):
            return resolve_contact_id(
                contacts_store, platform=p, account_id=a, chat_key=c)

        lim = max(1, min(int(limit), 2000))
        res = await run_in_threadpool(
            backfill_contact_ids, inbox, _resolver,
            limit=lim, platform=str(platform or ""), dry_run=bool(dry_run),
        )
        import time as _t
        out = {**res.as_dict(), "trigger": "manual", "ts": _t.time()}
        app.state.last_contact_backfill = out
        return {"ok": True, "result": out}

    # ── 可选：reactivation 候选列表 ───────────────────────
    if reactivation_scheduler is not None:
        @app.get("/api/reactivation/candidates")
        async def list_reactivation(_=Depends(api_auth)):
            cands = reactivation_scheduler.list_candidates()
            return {
                "items": [{
                    "journey_id": c.journey_id,
                    "contact_id": c.contact_id,
                    "funnel_stage": c.funnel_stage,
                    "intimacy_score": c.intimacy_score,
                    "silent_days": c.silent_days,
                    "last_reactivation_ts": c.last_reactivation_ts,
                } for c in cands],
            }

        @app.post("/api/reactivation/{journey_id}/mark-sent")
        async def mark_reactivation(journey_id: str, request: Request,
                                     _=Depends(api_auth)):
            user = _extract_user(request)
            reactivation_scheduler.mark_sent(journey_id, note=f"by:{user or 'system'}")
            # W3-3G：联动 draft_log——如果该 journey 有最新未发草稿，标记已发
            linked_draft_id = ""
            try:
                draft = contacts_store.latest_unsent_draft_for(journey_id)
                if draft:
                    if contacts_store.mark_draft_sent(
                        draft["draft_id"], sent_by=user or "system",
                    ):
                        linked_draft_id = draft["draft_id"]
            except Exception as e:
                logger.warning("draft mark-sent link failed: %s", e)
            return {"ok": True, "linked_draft_id": linked_draft_id}

        @app.post("/api/reactivation/{journey_id}/draft-reunion")
        async def draft_reunion(journey_id: str, request: Request,
                                _=Depends(api_auth)):
            """W3-3F：为 reactivation 候选自动生成「久违重逢」开场草稿。

            **不自动发送**——返回 ``draft_text`` 给运营审核，确认后由人工
            通过现有渠道发送（短期内不开自动外呼通道，符合 contacts.enabled
            的灰度精神）。

            前置：
              - ``ai_client`` 已 wire（否则 503）
              - journey 存在（否则 404）

            响应：
              ``{draft_text, journey_id, intimacy_score, silent_days,
                 funnel_stage, prompt_signals}``
            """
            if ai_client is None:
                raise HTTPException(
                    status_code=503,
                    detail="ai_client_not_wired",
                )
            j = contacts_store.get_journey(journey_id)
            if j is None:
                raise HTTPException(status_code=404, detail="journey_not_found")
            # 取最近 5 条事件（足够抓最后一条 inbound 文字）
            events = contacts_store.list_events(journey_id, limit=10)
            last_inbound_text = ""
            for ev in events:  # 已按 ts DESC
                if ev.get("event_type") == "msg_in":
                    payload = ev.get("payload") or {}
                    # gateway 写的是 'preview'；first-text replay 也是 'preview'
                    txt = (payload.get("preview") or payload.get("text_preview") or "").strip()
                    if txt:
                        last_inbound_text = txt[:80]
                        break
            # 沉默天数（近似：now - last_active；从 reactivation_scheduler 拿更准）
            try:
                cand = next(
                    (c for c in reactivation_scheduler.list_candidates()
                     if c.journey_id == journey_id),
                    None,
                )
            except Exception:
                cand = None
            silent_days = int(cand.silent_days) if cand else 0
            intim = float(j.intimacy_score or 0)
            stage = j.funnel_stage or "INITIAL"
            # 语言感知：从 contact.language_hint 派生草稿语言（zh/en/ja 三档兜底中文）
            contact = contacts_store.get_contact(j.contact_id) if j.contact_id else None
            lang_hint = (getattr(contact, "language_hint", "") or "").lower().strip()
            if lang_hint.startswith("en"):
                draft_lang = "en"
            elif lang_hint.startswith("ja") or lang_hint.startswith("jp"):
                draft_lang = "ja"
            else:
                draft_lang = "zh"
            # W3-3H.2/3 + W3-3I.2：prompt registry + journey-aware persona + A/B variant 路由
            registry = _get_reunion_registry()
            variant_id = registry.select_variant(journey_id)
            persona_name, persona_role, forbidden_phrases = _load_persona_for_prompt(
                journey=j,
            )
            prompt, resolved_variant, resolved_lang = registry.render(
                variant=variant_id,
                lang=draft_lang,
                persona_name=persona_name,
                persona_role=persona_role,
                forbidden_phrases=forbidden_phrases,
                silent_days=silent_days,
                funnel_stage=stage,
                intim=intim,
                last_inbound=last_inbound_text,
            )
            draft_lang = resolved_lang  # registry 可能 fallback 改了 lang
            try:
                draft = await ai_client.chat(prompt)
            except Exception as e:
                logger.warning("reunion draft generation failed: %s", e)
                raise HTTPException(status_code=502, detail=f"ai_generation_failed: {e}")
            if not draft or not str(draft).strip():
                raise HTTPException(status_code=502, detail="ai_empty_response")
            draft_text = str(draft).strip().strip('"').strip("'")
            # 裁剪：防 AI 不听话回了一长段
            if len(draft_text) > 200:
                draft_text = draft_text[:200].rstrip() + "…"
            user = _extract_user(request)
            # W3-3G：写 draft_log，给反馈闭环铺底
            try:
                draft_id = contacts_store.record_draft(
                    journey_id=journey_id,
                    contact_id=j.contact_id or "",
                    draft_text=draft_text,
                    draft_lang=draft_lang,
                    intimacy_score=intim,
                    silent_days=silent_days,
                    funnel_stage=stage,
                    prompt_variant=resolved_variant,
                    prompt_snapshot_hash=_hash_prompt(prompt),
                )
            except Exception as e:
                logger.warning("draft_log write failed: %s", e)
                draft_id = ""
            if audit_store:
                _safe_audit(
                    audit_store, user, "reunion_draft_generated",
                    f"jid={journey_id} did={draft_id} silent_days={silent_days} "
                    f"intim={intim:.0f} lang={draft_lang} variant={resolved_variant} "
                    f"len={len(draft_text)}",
                )
            return {
                "ok": True,
                "draft_id": draft_id,
                "draft_text": draft_text,
                "journey_id": journey_id,
                "intimacy_score": intim,
                "silent_days": silent_days,
                "funnel_stage": stage,
                "draft_lang": draft_lang,
                "prompt_variant": resolved_variant,
                "persona_name": persona_name or "",
                "prompt_signals": {
                    "has_last_inbound": bool(last_inbound_text),
                    "has_persona": bool(persona_name),
                    "prompt_length": len(prompt),
                },
            }

    # ── W3-3G：draft 质量评估端点（手动触发；后续可接定时器） ─
    # NOTE：必须用 /api/drafts/* 而不是 /api/contacts/draft-*，
    #   因为 /api/contacts/{contact_id} GET 路由会先匹配，把 'draft-quality'
    #   当成 contact_id 一律 404。这是 FastAPI 按注册顺序匹配的副作用。
    @app.post("/api/drafts/eval-run")
    async def run_draft_eval(request: Request, _=Depends(api_auth)):
        """W3-3G / W3-3K：手动触发草稿成功率评估。

        优先通过 ``eval_scheduler.run_once()`` 执行（确保状态可观测）；
        若 scheduler 未注入，退化到直接创建 ``DraftSuccessEvaluator``。

        Body 可选：``{"window_secs": 86400}`` 覆盖默认窗口。
        """
        try:
            body = await request.json()
        except Exception:
            body = {}
        window = int(body.get("window_secs", 86400))
        if eval_scheduler is not None:
            result = eval_scheduler.run_once()
        else:
            from src.contacts.draft_eval import DraftSuccessEvaluator
            ev = DraftSuccessEvaluator(contacts_store, eval_window_secs=window)
            result = ev.evaluate_due()
        user = _extract_user(request)
        if audit_store:
            _safe_audit(
                audit_store, user, "draft_eval_run",
                f"window={window} evaluated={result['evaluated']} "
                f"success={result['success']} fail={result['fail']}",
            )
        return {"ok": True, **result, "window_secs": window}

    @app.get("/api/drafts/eval-scheduler/status")
    async def eval_scheduler_status(_=Depends(api_auth)):
        """W3-3K.3：草稿评估调度器当前状态。

        返回 ``{last_run_at, last_run_ago_secs, last_result, next_run_at,
        next_run_in_secs, current_interval_secs, base_interval_secs,
        total_runs, is_running, eval_window_secs}``。

        ``last_run_at = null`` 表示本次进程启动后尚未运行过（首次运行在启动 5min 后）。
        """
        if eval_scheduler is None:
            return {
                "available": False,
                "reason": "eval_scheduler_not_wired",
            }
        return {"available": True, **eval_scheduler.status()}

    @app.post("/api/drafts/{draft_id}/unmark-sent")
    async def unmark_draft_sent(draft_id: str, request: Request,
                                 _=Depends(api_auth)):
        """W3-3H.5：撤回 draft 的「已发」状态（运营点错时用）。

        - 已评估的 draft 不能撤回（避免 stats 历史分母变动）
        - 注意：这只清 draft_log 的 sent_ts；reactivation cooldown 仍生效，
          因为 cooldown 由 ``journey_events`` 上的 reactivation_sent 事件驱动，
          撤回它是另一回事（权衡：cooldown 是「保护被骚扰」，宁可保守）
        """
        ok = contacts_store.mark_draft_unsent(draft_id)
        if not ok:
            raise HTTPException(
                status_code=400,
                detail="unmark_failed: draft not found, not sent, or already evaluated",
            )
        user = _extract_user(request)
        if audit_store:
            _safe_audit(audit_store, user, "draft_unmark_sent", draft_id)
        return {"ok": True}

    @app.get("/api/drafts/quality")
    async def draft_quality(days: int = 7, _=Depends(api_auth)):
        """W3-3G：reunion 草稿质量统计（最近 N 天，按 lang/variant 分桶）。

        W3-3J.4：by_hash 分组。
        """
        days = max(1, min(int(days), 90))
        stats = contacts_store.draft_quality_stats(days=days)
        # W3-3J.4：附加 by_hash 桶（按 prompt_snapshot_hash 聚合）
        try:
            stats["by_hash"] = contacts_store.draft_quality_by_hash(days=days)
        except Exception as _he:
            logger.debug("draft_quality_by_hash failed: %s", _he)
            stats["by_hash"] = {}
        # W3-3I.1：附加 winning_variant（UI 直接读用）
        from src.contacts.store import ContactStore as _CS
        stats["winning_variant"] = _CS.pick_winning_variant(
            stats.get("by_variant") or {}
        )
        return stats

    @app.get("/api/reunion-prompts")
    async def reunion_prompts_list(_=Depends(api_auth)):
        """W3-3J.1：列出已加载的 prompt variants + 当前 default。"""
        reg = _get_reunion_registry()
        return {
            "variants": reg.variants,
            "default_variant": reg.default_variant,
        }

    @app.post("/api/reunion-prompts/set-default")
    async def set_default_variant(request: Request, _=Depends(api_auth)):
        """W3-3J.1：把指定 variant 写为 yaml default_variant（立即生效）。

        Body: ``{"variant": "v2"}``

        安全约束（在 Registry 层）：
          - variant 必须存在于当前 loaded variants 里
          - 写操作原子性 (tmp → rename)
          - inline-only 模式下返回 400（没有 yaml 可写）
        """
        body = await request.json()
        variant = str(body.get("variant") or "").strip()
        if not variant:
            raise HTTPException(status_code=422, detail="variant required")
        reg = _get_reunion_registry()
        if variant not in reg.variants:
            raise HTTPException(
                status_code=400,
                detail=f"variant_not_found: {variant!r} not in {reg.variants}",
            )
        ok = reg.promote_default_variant(variant)
        if not ok:
            raise HTTPException(
                status_code=400,
                detail="promote_failed: config file may be missing (inline-only mode)",
            )
        user = _extract_user(request)
        if audit_store:
            _safe_audit(
                audit_store, user, "reunion_prompt_default_changed",
                f"variant={variant}",
            )
        return {"ok": True, "default_variant": reg.default_variant}

    # ── 健康检查（feature flag 开时各子服务是否就绪） ─
    # NOTE：路径用 /api/contacts-health 而非 /api/contacts/health，避免被
    # /api/contacts/{contact_id} 路由 shadow（W3-3G 顺手修：之前一直 silent 404）。
    @app.get("/api/contacts-health")
    @app.get("/api/contacts/health")  # legacy alias（虽然 shadow，留着 fallback；新代码用上面那个）
    async def contacts_health(_=Depends(api_auth)):
        return {
            "ok": True,
            "services": {
                "contacts_store": contacts_store is not None,
                "merge_service": merge_service is not None,
                "intimacy_engine": intimacy_engine is not None,
                "reactivation_scheduler": reactivation_scheduler is not None,
                "gateway": gateway is not None,
                "account_limiter": account_limiter is not None,
            },
        }

    # ── 账号限额 ──────────────────────────────────────
    if account_limiter is not None:
        @app.get("/api/accounts/{account_id}/limit")
        async def get_limit(account_id: str, _=Depends(api_auth)):
            return account_limiter.get_counts(account_id)

        @app.post("/api/accounts/{account_id}/limit/reset")
        async def reset_limit(account_id: str, request: Request,
                               _=Depends(api_auth)):
            user = _extract_user(request)
            account_limiter.reset(account_id)
            if audit_store:
                _safe_audit(audit_store, user, "account_limit_reset", account_id)
            return {"ok": True}

    # ── 引流预览（dry_run） ──────────────────────────────
    if gateway is not None:
        @app.get("/api/handoff/preview")
        async def preview_handoff(
            messenger_ci_id: str,
            latest_in_text: str = "",
            tone: str = "",
            language_override: str = "",
            _=Depends(api_auth),
        ):
            r = gateway.maybe_issue_handoff(
                messenger_ci_id=messenger_ci_id,
                latest_in_text=latest_in_text,
                tone=tone,
                language_override=language_override,
                dry_run=True,
            )
            return {
                "success": r.success,
                "reason": r.reason,
                "text": r.text,
                "script_id": r.script_id,
                "language": r.language,
                "readiness_score": r.readiness_score,
                "remaining_today": r.remaining_today,
                "warn_hits": r.warn_hits,
                "details": r.details,
            }

    # ── 最小 Ops UI（纯静态 HTML + fetch，不走 Jinja2） ───
    @app.get("/ops/contacts", response_class=HTMLResponse)
    async def ops_contacts_page(_=Depends(api_auth)):
        return HTMLResponse(_load_ops_html("contacts.html", active="/ops/contacts"))

    @app.get("/ops/merge-reviews", response_class=HTMLResponse)
    async def ops_merge_reviews_page(_=Depends(api_auth)):
        return HTMLResponse(_load_ops_html("merge_reviews.html", active="/ops/merge-reviews"))

    # ── Mobile Bridge 路由（仅 mobile_bridge 注入时挂载） ────────────
    if mobile_bridge is not None:
        import asyncio as _asyncio
        import functools as _functools

        @app.get("/api/mobile-bridge/health")
        async def mobile_bridge_health(_=Depends(api_auth)):
            """Bridge 状态：同步计数、watermark、最近错误、dead_letter。"""
            return await _asyncio.to_thread(mobile_bridge.status)

        @app.get("/api/mobile-handoffs/summary")
        async def mobile_handoffs_summary(_=Depends(api_auth)):
            """各 state 的 handoff 计数（供 UI tab 徽章使用）。"""
            counts = await _asyncio.to_thread(mobile_bridge.count_by_state)
            total = sum(counts.values())
            return {"by_state": counts, "total": total}

        @app.get("/api/mobile-handoffs")
        async def list_mobile_handoffs(
            state: str = "",
            canonical_id: str = "",
            limit: int = 50,
            offset: int = 0,
            _=Depends(api_auth),
        ):
            """从 openclaw.db 实时查询 handoff 列表（只读）。"""
            limit = max(1, min(int(limit), 200))
            offset = max(0, int(offset))
            rows = await _asyncio.to_thread(
                _functools.partial(
                    mobile_bridge.list_mobile_handoffs,
                    state=state, canonical_id=canonical_id,
                    limit=limit, offset=offset,
                )
            )
            return {"items": rows, "count": len(rows)}

        @app.get("/api/mobile-handoffs/{handoff_id}")
        async def get_mobile_handoff(handoff_id: str, _=Depends(api_auth)):
            """查单条 handoff（来自 openclaw.db）。"""
            row = await _asyncio.to_thread(mobile_bridge.get_mobile_handoff, handoff_id)
            if not row:
                raise HTTPException(status_code=404, detail="handoff_not_found")
            return row

        @app.post("/api/mobile-handoffs/{handoff_id}/acknowledge")
        async def mobile_ack(handoff_id: str, request: Request, _=Depends(api_auth)):
            """Telegram 后台确认接单 → 回写 mobile API。mobile 不可达时入队重试。"""
            user = _extract_user(request)
            by = f"telegram_admin:{user or 'system'}"
            try:
                result = await _asyncio.to_thread(
                    _functools.partial(mobile_bridge.writeback_acknowledge, handoff_id, by=by)
                )
                if audit_store:
                    _safe_audit(audit_store, user, "mobile_handoff_acknowledge", handoff_id)
                return {"ok": True, "queued": False, "mobile_result": result}
            except Exception as exc:
                retry_id = await _asyncio.to_thread(
                    _functools.partial(mobile_bridge.enqueue_writeback,
                                       handoff_id, "acknowledge", by=by)
                )
                return JSONResponse(status_code=202, content={
                    "ok": False, "queued": True, "retry_id": retry_id,
                    "error": str(exc), "msg": "mobile 暂时不可达，已入队自动重试",
                })

        @app.post("/api/mobile-handoffs/{handoff_id}/complete")
        async def mobile_complete(handoff_id: str, request: Request, _=Depends(api_auth)):
            """Telegram 后台标记完成 → 回写 mobile API。mobile 不可达时入队重试。"""
            user = _extract_user(request)
            by = f"telegram_admin:{user or 'system'}"
            try:
                result = await _asyncio.to_thread(
                    _functools.partial(mobile_bridge.writeback_complete, handoff_id, by=by)
                )
                if audit_store:
                    _safe_audit(audit_store, user, "mobile_handoff_complete", handoff_id)
                return {"ok": True, "queued": False, "mobile_result": result}
            except Exception as exc:
                retry_id = await _asyncio.to_thread(
                    _functools.partial(mobile_bridge.enqueue_writeback,
                                       handoff_id, "complete", by=by)
                )
                return JSONResponse(status_code=202, content={
                    "ok": False, "queued": True, "retry_id": retry_id,
                    "error": str(exc), "msg": "mobile 暂时不可达，已入队自动重试",
                })

        @app.post("/api/mobile-handoffs/{handoff_id}/reject")
        async def mobile_reject(handoff_id: str, request: Request, _=Depends(api_auth)):
            """Telegram 后台拒绝 → 回写 mobile API。mobile 不可达时入队重试。"""
            user = _extract_user(request)
            by = f"telegram_admin:{user or 'system'}"
            try:
                result = await _asyncio.to_thread(
                    _functools.partial(mobile_bridge.writeback_reject, handoff_id, by=by)
                )
                if audit_store:
                    _safe_audit(audit_store, user, "mobile_handoff_reject", handoff_id)
                return {"ok": True, "queued": False, "mobile_result": result}
            except Exception as exc:
                retry_id = await _asyncio.to_thread(
                    _functools.partial(mobile_bridge.enqueue_writeback,
                                       handoff_id, "reject", by=by)
                )
                return JSONResponse(status_code=202, content={
                    "ok": False, "queued": True, "retry_id": retry_id,
                    "error": str(exc), "msg": "mobile 暂时不可达，已入队自动重试",
                })

        @app.get("/ops/mobile-handoffs", response_class=HTMLResponse)
        async def ops_mobile_handoffs_page(_=Depends(api_auth)):
            return HTMLResponse(_load_ops_html("mobile_handoffs.html", active="/ops/mobile-handoffs"))

        @app.get("/api/mobile-bridge/writeback-queue")
        async def list_writeback_queue(
            status: str = "dead_letter",
            limit: int = 50,
            _=Depends(api_auth),
        ):
            """列出 writeback 队列特定状态的条目（默认 dead_letter）。"""
            limit = max(1, min(int(limit), 200))
            rows = await _asyncio.to_thread(
                _functools.partial(mobile_bridge.list_writeback_queue, status=status, limit=limit)
            )
            return {"items": rows, "count": len(rows), "status": status}

        @app.post("/api/mobile-bridge/writeback-queue/{item_id}/retry")
        async def retry_dead_letter(item_id: int, _=Depends(api_auth)):
            """\u5c06 dead_letter 条目重置为 pending，下次轮询时自动重试。"""
            ok = await _asyncio.to_thread(
                _functools.partial(mobile_bridge.retry_dead_letter, item_id)
            )
            if not ok:
                raise HTTPException(status_code=404, detail="item_not_found_or_not_dead_letter")
            return {"ok": True, "item_id": item_id, "new_status": "pending"}


def _extract_user(request: Request) -> str:
    """从 request.state 拿登录用户名，缺失时回退空串。"""
    for attr in ("user_id", "username", "user"):
        val = getattr(request.state, attr, None)
        if val:
            return str(val)
    return ""


def _safe_audit(audit_store, user_id: str, action: str, target: str) -> None:
    try:
        audit_store.log(user_id or "system", action, target=target)
    except Exception as e:
        logger.debug("audit log skipped: %s", e)

