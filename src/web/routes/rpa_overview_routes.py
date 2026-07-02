"""跨平台 RPA 总览 —— Web 页面 + 聚合 API。

设计要点：

- 复用 `src.integrations.rpa_base.types.RpaStatusSummary.from_status_dict()`
  做"鸭子类型"映射：4 个 service 的 status() 字段不完全一致，但都被
  `from_status_dict` 兼容。
- Telegram 没有传统 RPA service（它走 MTProto 直发），所以单独处理：
  从 `request.app.state.telegram_client` 读 `running`，其它指标走
  config / 占位。
- 该路由以**读为主**，控制端点仅代理到各平台已有的 pause/resume/trigger。

页面：
    GET /rpa-overview                — 4 平台并排概览

REST：
    GET  /api/rpa-overview/status    — 聚合 4 平台 status（带平台名 / 健康标记）
    GET  /api/rpa-overview/pending   — 跨平台最近待审（每平台前 N 条）
    GET  /api/rpa-overview/alerts    — 跨平台未确认告警
    POST /api/rpa-overview/control   — 统一运行控制（start/stop/pause/resume/trigger）
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from src.integrations.rpa_base import (
    AlertItem,
    PendingItem,
    RpaPlatform,
    RpaStatusSummary,
)
from src.integrations.rpa_shared import (
    count_runs_for_chat_name,
    extract_chat_name,
)
from src.web.web_i18n import tr

logger = logging.getLogger(__name__)

# P14-A: in-process TTL cache for cross-platform-profile (chat_key → (ts, payload))
# P16-B: TTL/上限可通过 config.yaml::rpa.cache.{cross_profile_ttl,cross_profile_max} 覆写
_XPROF_CACHE: Dict[str, Any] = {}
_XPROF_CACHE_TTL_SEC = 60.0
_XPROF_CACHE_MAX = 256

# P6-B: lang-dist 端点 30s TTL 缓存（避免仪表板轮询频繁查 SQLite）
_LANG_DIST_CACHE: tuple = (0.0, None)  # (fetched_at_ts, payload_dict)
_LANG_DIST_CACHE_TTL = 30.0
_LANG_DIST_VERSION: int = 0  # P12-C: incremented each time cache is invalidated


def invalidate_lang_dist_cache() -> None:
    """P10-D/P12-C: 让 lang-dist 缓存立即过期并自增版本号。"""
    global _LANG_DIST_CACHE, _LANG_DIST_VERSION
    _LANG_DIST_CACHE = (0.0, None)
    _LANG_DIST_VERSION += 1


def _apply_cache_config(config_manager: Any) -> None:
    """读 config.yaml::rpa.cache 覆写默认值；任何失败保持默认。"""
    global _XPROF_CACHE_TTL_SEC, _XPROF_CACHE_MAX, _LANG_DIST_CACHE_TTL
    try:
        cm = (config_manager.config or {}) if config_manager else {}
        sect = (cm.get("rpa") or {}).get("cache") or {}
        ttl = sect.get("cross_profile_ttl")
        if ttl is not None:
            _XPROF_CACHE_TTL_SEC = max(1.0, float(ttl))
        mx = sect.get("cross_profile_max")
        if mx is not None:
            _XPROF_CACHE_MAX = max(8, int(mx))
        # P13-C: lang_dist_ttl 可配置（默认 30s）
        ld_ttl = sect.get("lang_dist_ttl")
        if ld_ttl is not None:
            _LANG_DIST_CACHE_TTL = max(5.0, float(ld_ttl))
        if ttl is not None or mx is not None or ld_ttl is not None:
            logger.info(
                "rpa cache config applied: ttl=%.1fs max=%d lang_dist_ttl=%.1fs",
                _XPROF_CACHE_TTL_SEC, _XPROF_CACHE_MAX, _LANG_DIST_CACHE_TTL,
            )
    except Exception as exc:
        logger.warning("rpa cache config parse failed (using defaults): %s", exc)
# P15-B: Prometheus-friendly counters
_XPROF_CACHE_STATS: Dict[str, int] = {"hits": 0, "misses": 0, "evictions": 0, "expired": 0}


def _xprof_cache_get(key: str) -> Any:
    import time as _t
    item = _XPROF_CACHE.get(key)
    if not item:
        _XPROF_CACHE_STATS["misses"] += 1
        return None
    if _t.time() - item[0] > _XPROF_CACHE_TTL_SEC:
        _XPROF_CACHE.pop(key, None)
        _XPROF_CACHE_STATS["expired"] += 1
        _XPROF_CACHE_STATS["misses"] += 1
        return None
    _XPROF_CACHE_STATS["hits"] += 1
    return item[1]


def _xprof_cache_put(key: str, value: Any) -> None:
    import time as _t
    if len(_XPROF_CACHE) >= _XPROF_CACHE_MAX:
        # FIFO 淘汰最早一个（dict 保留插入序）
        try:
            _XPROF_CACHE.pop(next(iter(_XPROF_CACHE)))
            _XPROF_CACHE_STATS["evictions"] += 1
        except StopIteration:
            pass
    _XPROF_CACHE[key] = (_t.time(), value)


# ════════════════════════════════════════════════════════════════════════
# Service getters
# ════════════════════════════════════════════════════════════════════════


def _get_line_service(request: Request):
    return getattr(request.app.state, "line_rpa_service", None)


def _get_line_services(request: Request) -> list:
    svcs = getattr(request.app.state, "line_rpa_services", None)
    if svcs:
        return list(svcs)
    primary = _get_line_service(request)
    return [primary] if primary else []


def _get_messenger_service(request: Request):
    return getattr(request.app.state, "messenger_rpa_service", None)


def _get_whatsapp_service(request: Request):
    return getattr(request.app.state, "whatsapp_rpa_service", None)


def _get_whatsapp_services(request: Request) -> list:
    svcs = getattr(request.app.state, "whatsapp_rpa_services", None)
    if svcs:
        return list(svcs)
    primary = _get_whatsapp_service(request)
    return [primary] if primary else []


def _get_telegram_client(request: Request):
    return getattr(request.app.state, "telegram_client", None)


# ════════════════════════════════════════════════════════════════════════
# Per-platform summary builders
# ════════════════════════════════════════════════════════════════════════


def _summarize_via_service(
    platform: RpaPlatform, svc: Any
) -> RpaStatusSummary:
    """通用 RPA service 摘要构造器。

    适用于 LINE / WhatsApp / Messenger（status() 都返回 dict）。
    若 svc 为 None，返回 unavailable summary。
    """
    if svc is None:
        return RpaStatusSummary(
            platform=platform,
            available=False,
            enabled=False,
            hint=f"{platform.display_name} RPA 服务未启用或未构建",
        )
    try:
        st = svc.status() or {}
    except Exception as ex:
        logger.debug("status() of %s failed: %s", platform.value, ex)
        return RpaStatusSummary(
            platform=platform,
            available=False,
            enabled=False,
            hint=f"获取 {platform.display_name} 状态失败：{type(ex).__name__}",
        )

    # Messenger 的 status() 没有 enabled 字段：但既然 service 存在，
    # 即 config.enabled=true（main.py 那段构建时已经做过门控）。
    if platform is RpaPlatform.MESSENGER:
        st.setdefault("enabled", True)
        # Messenger 用 send_counters，但我们要的是 stats_24h；做一次映射。
        sc = st.get("send_counters") or {}
        if "stats_24h" not in st and sc:
            st["stats_24h"] = {
                "sent": sc.get("sent_24h") or sc.get("sent_today") or 0,
                "total": sc.get("total_24h") or sc.get("attempts_24h") or 0,
                "avg_ms": sc.get("avg_ms_24h") or sc.get("avg_ms") or 0,
            }
        # Messenger pending_count 在 approval_sla 里
        if "pending_count" not in st:
            sla = st.get("approval_sla") or {}
            st["pending_count"] = sla.get("pending_count") or 0

    summary = RpaStatusSummary.from_status_dict(platform, st)
    summary.available = True
    return summary


def _summarize_telegram(request: Request) -> RpaStatusSummary:
    """Telegram 走 MTProto，没有 RPA service；从 telegram_client 拿 running。

    24h 统计交给 dashboard 走 audit_store，本总览只显示连接状态。
    """
    client = _get_telegram_client(request)
    if client is None:
        return RpaStatusSummary(
            platform=RpaPlatform.TELEGRAM,
            available=False,
            enabled=False,
            hint="Telegram 客户端未初始化",
        )

    cfg = {}
    try:
        cm = getattr(request.app.state, "config_manager", None)
        cfg = (cm.config or {}).get("telegram", {}) if cm else {}
    except Exception:
        cfg = {}

    running = bool(getattr(client, "running", False))
    last_send = float(getattr(client, "_last_send_wallclock", 0) or 0)
    # 简单从 client 上拿一些计数（不是所有 client 都有；缺失就是 0）
    sent_24h = 0
    try:
        # event_tracker 上的 24h 计数（如果挂了的话）
        et = getattr(request.app.state, "event_tracker", None)
        if et and hasattr(et, "count_recent"):
            sent_24h = int(et.count_recent("telegram_sent", hours=24) or 0)
    except Exception:
        sent_24h = 0

    return RpaStatusSummary(
        platform=RpaPlatform.TELEGRAM,
        available=True,
        enabled=bool(cfg.get("enabled", True)),  # telegram 默认是主入口
        running=running,
        paused=False,
        reply_mode="auto",  # MTProto 直发即"auto"
        sent_24h=sent_24h,
        total_24h=sent_24h,
        last_run_ts=last_send,
        hint="MTProto 直发模式（非 RPA 轮询）",
    )


def _svc_account_meta(svc) -> Dict[str, str]:
    """Return account_id + account_label for a service instance."""
    if svc is None:
        return {}
    aid = getattr(svc, "account_id", None) or ""
    if not aid or aid == "default":
        return {}
    label = ""
    try:
        label = (svc._merged_cfg if hasattr(svc, "_merged_cfg") else {}).get("label") or ""
    except Exception:
        pass
    return {"account_id": aid, "account_label": label or aid}


def _collect_summaries(request: Request) -> List[RpaStatusSummary]:
    """4 平台收集；LINE/WhatsApp 多账号时每个账号单独一条。"""
    out: List[RpaStatusSummary] = [_summarize_telegram(request)]
    for svc in _get_line_services(request):
        s = _summarize_via_service(RpaPlatform.LINE, svc)
        meta = _svc_account_meta(svc)
        if meta:
            s.hint = f"[{meta['account_label']}] {s.hint}".strip()
        out.append(s)
    if not _get_line_services(request):
        out.append(_summarize_via_service(RpaPlatform.LINE, None))
    out.append(_summarize_via_service(RpaPlatform.MESSENGER, _get_messenger_service(request)))
    for svc in _get_whatsapp_services(request):
        s = _summarize_via_service(RpaPlatform.WHATSAPP, svc)
        meta = _svc_account_meta(svc)
        if meta:
            s.hint = f"[{meta['account_label']}] {s.hint}".strip()
        out.append(s)
    if not _get_whatsapp_services(request):
        out.append(_summarize_via_service(RpaPlatform.WHATSAPP, None))
    return out


def _collect_pending(
    request: Request, *, limit_per_platform: int = 5
) -> List[Dict[str, Any]]:
    """收集 3 个 RPA 平台（Telegram 没有审核队列）的最近 pending 条目。

    返回 dict 列表，已加 platform / platform_name 字段。按 ts 倒序合并。
    """
    out: List[Dict[str, Any]] = []
    line_svcs = [(RpaPlatform.LINE, s) for s in _get_line_services(request)]
    wa_svcs = [(RpaPlatform.WHATSAPP, s) for s in _get_whatsapp_services(request)]
    if not line_svcs:
        line_svcs = [(RpaPlatform.LINE, None)]
    if not wa_svcs:
        wa_svcs = [(RpaPlatform.WHATSAPP, None)]
    targets = line_svcs + [(RpaPlatform.MESSENGER, _get_messenger_service(request))] + wa_svcs
    for platform, svc in targets:
        if svc is None:
            continue
        try:
            # Messenger 用 list_approvals；line / whatsapp 用 list_pending
            if hasattr(svc, "list_pending"):
                rows = svc.list_pending(status="pending", limit=limit_per_platform)
            elif hasattr(svc, "list_approvals"):
                rows = svc.list_approvals(status="pending", limit=limit_per_platform)
            else:
                rows = []
        except Exception as ex:
            logger.debug("list_pending(%s) failed: %s", platform.value, ex)
            rows = []
        for r in rows or []:
            try:
                pi = PendingItem.from_dict(r)
                d = pi.to_dict()
                d["platform"] = platform.value
                d["platform_name"] = platform.display_name
                out.append(d)
            except Exception:
                continue
    # 按 ts 倒序合并
    out.sort(key=lambda x: x.get("ts") or 0, reverse=True)
    return out


def _collect_alerts(
    request: Request, *, limit_per_platform: int = 5
) -> List[Dict[str, Any]]:
    """收集 2 个 RPA 平台（LINE / WhatsApp，Messenger 走 TG escalation）的未确认告警。"""
    out: List[Dict[str, Any]] = []
    targets = [
        (RpaPlatform.LINE, _get_line_service(request)),
        (RpaPlatform.WHATSAPP, _get_whatsapp_service(request)),
    ]
    for platform, svc in targets:
        if svc is None or not hasattr(svc, "list_alerts"):
            continue
        try:
            rows = svc.list_alerts(only_unacked=True, limit=limit_per_platform)
        except Exception as ex:
            logger.debug("list_alerts(%s) failed: %s", platform.value, ex)
            rows = []
        for r in rows or []:
            try:
                ai = AlertItem.from_dict(r)
                d = {
                    "id": ai.id,
                    "severity": ai.severity.value,
                    "ts": ai.ts,
                    "code": ai.code,
                    "title": ai.title,
                    "platform": platform.value,
                    "platform_name": platform.display_name,
                }
                out.append(d)
            except Exception:
                continue
    out.sort(key=lambda x: x.get("ts") or 0, reverse=True)
    return out


# ════════════════════════════════════════════════════════════════════════
# Route registration
# ════════════════════════════════════════════════════════════════════════


def register_rpa_overview_routes(
    app,
    *,
    page_auth,
    api_auth,
    templates,
    config_manager=None,
):
    """在 FastAPI app 上挂载 /rpa-overview + /api/rpa-overview/*。

    依赖（从 app.state 读取，不强耦合）：
        - line_rpa_service / messenger_rpa_service / whatsapp_rpa_service
        - telegram_client
    任何一个缺失只会让对应卡片显示 unavailable，不影响其它平台展示。
    """
    # P16-B: 启动时应用 cache 配置覆写
    _apply_cache_config(config_manager)

    @app.get("/rpa-overview", response_class=HTMLResponse)
    async def rpa_overview_page(request: Request, _=Depends(page_auth)):
        return templates.TemplateResponse(request, "rpa_overview.html", {})

    @app.get("/api/rpa-overview/status")
    async def api_rpa_overview_status(request: Request):
        api_auth(request)
        summaries = _collect_summaries(request)
        # Build service meta lists in same order as _collect_summaries
        _line_metas = [_svc_account_meta(s) for s in _get_line_services(request)] or [{}]
        _wa_metas = [_svc_account_meta(s) for s in _get_whatsapp_services(request)] or [{}]
        _metas: List[Dict] = ([{}]                            # telegram
            + _line_metas                                     # LINE accounts
            + [{}]                                            # messenger
            + _wa_metas)                                      # WA accounts
        # P10-B/P12-B: 构建与 summaries 等长的 svc 列表，一一对应，保证每账号独立 force_reply_lang
        def _cfg_force_lang(svc) -> str:
            try:
                cfg = getattr(svc, "_merged_cfg", None) or getattr(svc, "_cfg", None) or {}
                fl = str(cfg.get("force_reply_lang") or "").strip().lower()
                return fl if fl and fl not in ("auto", "detect") else ""
            except Exception:
                return ""

        _msvc = _get_messenger_service(request)
        _all_svcs_ordered: List[Any] = (
            [None]                                      # telegram（无 service 对象）
            + list(_get_line_services(request))         # LINE 各账号
            + [_msvc]                                   # Messenger
            + list(_get_whatsapp_services(request))     # WA 各账号
        )
        platforms = []
        for s, meta, svc in zip(summaries, _metas, _all_svcs_ordered):
            d = s.to_dict()
            if meta:
                d["account_id"] = meta["account_id"]
                d["account_label"] = meta["account_label"]
            d["force_reply_lang"] = _cfg_force_lang(svc) if svc else ""
            d["last_lang_lock_ts"] = float(getattr(svc, "_last_lang_lock_ts", 0) or 0) if svc else 0
            platforms.append(d)
        # 汇总指标
        agg = {
            "platforms_total": len(platforms),
            "platforms_running": sum(1 for s in summaries if s.running),
            "platforms_paused": sum(1 for s in summaries if s.paused),
            "platforms_offline": sum(
                1 for s in summaries if not s.available or not s.enabled
            ),
            "sent_24h": sum(s.sent_24h for s in summaries),
            "pending_total": sum(s.pending_count for s in summaries),
            "alerts_unacked_total": sum(s.unacked_alerts for s in summaries),
        }
        return {
            "ok": True,
            "ts": time.time(),
            "aggregate": agg,
            "platforms": platforms,
        }

    @app.get("/api/rpa-overview/pending")
    async def api_rpa_overview_pending(request: Request, limit: int = 5):
        api_auth(request)
        limit = max(1, min(50, int(limit or 5)))
        items = _collect_pending(request, limit_per_platform=limit)
        return {"ok": True, "ts": time.time(), "items": items}

    @app.get("/api/rpa-overview/alerts")
    async def api_rpa_overview_alerts(request: Request, limit: int = 5):
        api_auth(request)
        limit = max(1, min(50, int(limit or 5)))
        items = _collect_alerts(request, limit_per_platform=limit)
        return {"ok": True, "ts": time.time(), "items": items}

    @app.get("/api/rpa/global-search")
    async def api_rpa_global_search(
        request: Request,
        q: str = "",
        intent: str = "",
        days: int = 30,
        limit: int = 20,
        platforms: str = "",
    ):
        """P10-B: 跨平台聊天历史检索。

        Query params:
          q         关键词（必填，否则返回空）
          intent    意图过滤（可选）
          days      回溯天数（默认 30，上限 365）
          limit     每个平台返回上限（默认 20，全局会聚后按 ts 排序）
          platforms 逗号分隔白名单（line/messenger/whatsapp），空=全部
        """
        api_auth(request)
        q = (q or "").strip()
        if not q:
            return {"ok": True, "q": q, "results": [], "by_platform": {}}
        days = max(1, min(365, int(days or 30)))
        limit = max(1, min(50, int(limit or 20)))
        wanted = {p.strip() for p in (platforms or "").split(",") if p.strip()}

        results: List[Dict[str, Any]] = []
        by_platform: Dict[str, int] = {}

        def _try(platform_label: str, api_base: str, fetcher):
            if wanted and platform_label not in wanted:
                return
            try:
                rs = fetcher() or []
            except Exception as exc:
                logger.warning("global-search %s failed: %s", platform_label, exc)
                rs = []
            by_platform[platform_label] = len(rs)
            for r in rs:
                d = dict(r)
                d["platform"] = platform_label
                d["api_base"] = api_base
                results.append(d)

        # LINE：多账号合并
        for svc in _get_line_services(request):
            if svc is None:
                continue
            _try("line", "/api/line-rpa",
                 lambda s=svc: s.search_history(q, intent=intent, days=days, limit=limit))

        # Messenger：单实例 + 直接 store
        msg_store = getattr(request.app.state, "messenger_rpa_state_store", None)
        if msg_store is not None:
            _try("messenger", "/api/messenger-rpa",
                 lambda: msg_store.search_history(q, intent=intent, days=days, limit=limit))

        # WhatsApp：多账号合并
        for svc in _get_whatsapp_services(request):
            if svc is None:
                continue
            _try("whatsapp", "/api/whatsapp-rpa",
                 lambda s=svc: s.search_history(q, intent=intent, days=days, limit=limit))

        # 全局按 ts 倒序，整体上限 = limit * 3
        results.sort(key=lambda r: r.get("ts") or 0, reverse=True)
        return {
            "ok": True,
            "q": q,
            "intent": intent,
            "days": days,
            "by_platform": by_platform,
            "results": results[: limit * 3],
        }

    @app.get("/api/rpa/cross-platform-profile")
    async def api_rpa_cross_platform_profile(
        request: Request,
        chat_key: str = "",
        name: str = "",
    ):
        """P12-A: 跨平台身份合并 — 给定 chat_key（或显式 name），查同名联系人在其他平台的对话量。

        匹配策略：
          - 若 name 为空，自动从 chat_key 提取最后一段（去除 UUID/纯数字/占位词）
          - 在每个平台用 chat_key LIKE `%:name` 查
          - 返回 {platform: {total_turns, last_ts, sample_chat_key}}
        """
        api_auth(request)
        chat_key = (chat_key or "").strip()
        explicit_name = (name or "").strip()
        target_name = explicit_name or extract_chat_name(chat_key)
        if not target_name:
            return {
                "ok": True, "chat_key": chat_key, "name": "",
                "platforms": {}, "total_other_turns": 0,
            }

        # P14-A: 60s TTL 缓存（chat_key + name 唯一）
        cache_key = chat_key + "|" + target_name
        cached = _xprof_cache_get(cache_key)
        if cached is not None:
            return cached

        platforms: Dict[str, Any] = {}

        # LINE — 多账号合计
        line_total = 0
        line_last = 0.0
        line_sample = ""
        for svc in _get_line_services(request):
            if svc is None:
                continue
            try:
                d = svc.match_chat_name(target_name) or {}
                line_total += int(d.get("total_turns") or 0)
                lt = float(d.get("last_ts") or 0)
                if lt > line_last:
                    line_last = lt
                if not line_sample and d.get("sample_chat_key"):
                    line_sample = d["sample_chat_key"]
            except Exception as exc:
                logger.warning("cross-platform match line failed: %s", exc)
        platforms["line"] = {
            "total_turns": line_total, "last_ts": line_last,
            "sample_chat_key": line_sample, "api_base": "/api/line-rpa",
        }

        # Messenger — 单实例
        msg_store = getattr(request.app.state, "messenger_rpa_state_store", None)
        if msg_store is not None:
            try:
                d = msg_store.match_chat_name(target_name) or {}
                platforms["messenger"] = {
                    "total_turns": int(d.get("total_turns") or 0),
                    "last_ts": float(d.get("last_ts") or 0),
                    "sample_chat_key": str(d.get("sample_chat_key") or ""),
                    "api_base": "/api/messenger-rpa",
                }
            except Exception as exc:
                logger.warning("cross-platform match messenger failed: %s", exc)
                platforms["messenger"] = {"total_turns": 0, "last_ts": 0, "sample_chat_key": "", "api_base": "/api/messenger-rpa"}

        # WhatsApp — 多账号合计
        wa_total = 0
        wa_last = 0.0
        wa_sample = ""
        for svc in _get_whatsapp_services(request):
            if svc is None:
                continue
            try:
                d = svc.match_chat_name(target_name) or {}
                wa_total += int(d.get("total_turns") or 0)
                lt = float(d.get("last_ts") or 0)
                if lt > wa_last:
                    wa_last = lt
                if not wa_sample and d.get("sample_chat_key"):
                    wa_sample = d["sample_chat_key"]
            except Exception as exc:
                logger.warning("cross-platform match whatsapp failed: %s", exc)
        platforms["whatsapp"] = {
            "total_turns": wa_total, "last_ts": wa_last,
            "sample_chat_key": wa_sample, "api_base": "/api/whatsapp-rpa",
        }

        # 排除"自己"（与传入 chat_key 同 sample 的平台轮次记到 self_turns）
        self_turns = 0
        other_turns = 0
        for p, info in platforms.items():
            if info["sample_chat_key"] == chat_key:
                self_turns += info["total_turns"]
            else:
                other_turns += info["total_turns"]

        result = {
            "ok": True,
            "chat_key": chat_key,
            "name": target_name,
            "platforms": platforms,
            "self_turns": self_turns,
            "total_other_turns": other_turns,
        }
        _xprof_cache_put(cache_key, result)
        return result

    # ════════════════════════════════════════════════════════════════════
    # P15-A: 意图关键词字典 — 查看 + 热更
    # ════════════════════════════════════════════════════════════════════

    @app.get("/api/rpa/intent-tags")
    async def api_rpa_intent_tags_get(request: Request):
        """返回当前运行时加载的意图关键词字典 + yaml 文件路径。"""
        api_auth(request)
        from src.integrations import rpa_shared as _shr
        return {
            "ok": True,
            "tags": _shr._INTENT_TAGS,
            "yaml_path": str(_shr._intent_tags_yaml_path()),
            "yaml_exists": _shr._intent_tags_yaml_path().exists(),
            "category_count": len(_shr._INTENT_TAGS),
            "keyword_count": sum(len(v) for v in _shr._INTENT_TAGS.values()),
        }

    @app.get("/api/rpa/intent-tags/raw", response_class=PlainTextResponse)
    async def api_rpa_intent_tags_raw(request: Request):
        """P16-A: 原始 yaml 文本（UI 编辑器初始化用）。"""
        api_auth(request)
        from src.integrations.rpa_shared import read_intent_tags_yaml
        return read_intent_tags_yaml()

    @app.post("/api/rpa/intent-tags")
    async def api_rpa_intent_tags_write(request: Request):
        """P16-A: 写入 yaml 文件（含原子写 + 备份 + reload）。

        请求体: {"content": "<yaml 文本>"}
        失败 → HTTP 400，详情含行号

        P24-C: body 上限 4MB（防大 JSON 攻击）。
        """
        api_auth(request)
        body = await _read_json_body(request, _WRITE_MAX_BODY_BYTES)
        content = body.get("content")
        if not isinstance(content, str):
            raise HTTPException(400, "content (str) required")
        from src.integrations.rpa_shared import write_intent_tags_yaml
        try:
            result = write_intent_tags_yaml(content)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        # 审计
        audit_store = getattr(request.app.state, "audit_store", None)
        if audit_store is not None:
            try:
                actor = getattr(getattr(request.state, "user", None), "username", "unknown")
                audit_store.log(actor, "rpa_intent_tags_write",
                                f"cats={result['category_count']} kws={result['keyword_count']}")
            except Exception:
                pass
        return result

    # P20-B / P21-A/B/C / P23-A: per-IP token bucket — 防止 /diff 端点被恶意脚本刷
    # 默认 capacity=20, refill=2/s；可通过 config.yaml::rpa.rate_limits 调
    # P21-A: X-Forwarded-For 解析（反代场景）；trusted_proxies 防伪造
    # P23-A: 改 OrderedDict 让 LRU 驱逐 O(1)（旧版用 min() O(N) 扫描）
    from collections import OrderedDict as _OD
    _diff_buckets: "_OD[str, list]" = _OD()    # ip -> [tokens, last_ts]
    _restore_buckets: "_OD[str, list]" = _OD() # P21-B: /restore 也限流
    _metrics_buckets: "_OD[str, list]" = _OD() # P23-D: /metrics 端点也限流
    _rate_limited_counter: Dict[str, int] = {"diff": 0, "restore": 0, "metrics": 0}

    def _read_rate_cfg(name: str, default_cap: float, default_refill: float) -> tuple:
        try:
            cfg = (config_manager.config or {}) if config_manager else {}
            rl = ((cfg.get("rpa") or {}).get("rate_limits") or {}).get(name) or {}
            cap = float(rl.get("capacity") or default_cap)
            refill = float(rl.get("refill_per_sec") or default_refill)
            return (max(1.0, cap), max(0.1, refill))
        except Exception:
            return (default_cap, default_refill)

    # P22-A: trusted_proxies 缓存 — IP/CIDR 编译为 ip_network；非 IP 字符串保留精确匹配
    # 缓存结构：{ key: tuple(raw), nets: [ip_network], literals: set[str] }
    _trusted_cache: Dict[str, Any] = {"key": None, "nets": [], "literals": set()}

    def _build_trusted_nets(raw: list):
        """编译 raw → (nets[ip_network], literals[set])。失败的条目跳过并日志。"""
        import ipaddress as _ipa
        nets: list = []
        literals: set = set()
        for item in raw or []:
            if not isinstance(item, str) or not item.strip():
                continue
            entry = item.strip()
            try:
                nets.append(_ipa.ip_network(entry, strict=False))
            except (ValueError, TypeError):
                # 非 IP/CIDR（如 TestClient 的 'testclient'）→ 退回精确字符串匹配
                literals.add(entry)
        return nets, literals

    def _is_trusted_proxy(ip_str: str) -> bool:
        import ipaddress as _ipa
        try:
            cfg = (config_manager.config or {}) if config_manager else {}
            raw = (cfg.get("rpa") or {}).get("trusted_proxies") or []
        except Exception:
            raw = []
        cache_key = tuple(raw)
        if _trusted_cache["key"] != cache_key:
            _trusted_cache["nets"], _trusted_cache["literals"] = _build_trusted_nets(raw)
            _trusted_cache["key"] = cache_key
        # 1) 精确字符串匹配（非 IP 也可用，如测试 'testclient'）
        if ip_str in _trusted_cache["literals"]:
            return True
        # 2) IP/CIDR 匹配
        nets = _trusted_cache["nets"]
        if not nets:
            return False
        try:
            ip = _ipa.ip_address(ip_str)
        except (ValueError, TypeError):
            return False
        return any(ip in net for net in nets)

    def _client_ip_of(request: Request) -> str:
        """P21-A / P22-A: 反代后真实 IP — 仅当来源 IP 在 trusted_proxies (含 CIDR) 才信任 XFF。"""
        direct = request.client.host if request.client else "unknown"
        if _is_trusted_proxy(direct):
            xff = request.headers.get("x-forwarded-for", "")
            if xff:
                # 取最左：原始 client（链路最远端）
                first = xff.split(",")[0].strip()
                if first:
                    return first
        return direct

    # P22-B: TTL — 超过 _BUCKET_IDLE_TTL_SEC 未活动的 IP 在下次 check 时被批量清扫
    _BUCKET_IDLE_TTL_SEC = 3600.0
    _bucket_last_sweep: Dict[str, float] = {"diff": 0.0, "restore": 0.0, "metrics": 0.0}
    _BUCKET_SWEEP_INTERVAL_SEC = 60.0

    def _sweep_idle_buckets(buckets: Dict[str, list], label: str, now: float) -> None:
        """P22-B: 每 60s 最多一次，删除 > 1h 没活动的 bucket。O(N) 但 N 通常 < 100。"""
        if now - _bucket_last_sweep.get(label, 0.0) < _BUCKET_SWEEP_INTERVAL_SEC:
            return
        _bucket_last_sweep[label] = now
        cutoff = now - _BUCKET_IDLE_TTL_SEC
        stale = [ip for ip, ent in buckets.items() if ent[1] < cutoff]
        for ip in stale:
            buckets.pop(ip, None)
        if stale:
            logger.debug("rate-limit bucket sweep[%s]: removed %d idle IPs", label, len(stale))

    def _bucket_check(buckets: "_OD[str, list]", cap: float, refill: float,
                      client_ip: str, label: str) -> None:
        """P23-A: 使用 OrderedDict — popitem(last=False) 让驱逐 O(1)。
        每次活动把 client_ip 移到末尾（最近活动），左端 = 最久未活动。
        """
        import time as _t
        now = _t.monotonic()
        _sweep_idle_buckets(buckets, label, now)  # P22-B
        ent = buckets.get(client_ip)
        if ent is None:
            buckets[client_ip] = [cap - 1.0, now]
            # 容量保护：超 4096 时弹出最早条目（LRU 头部）
            if len(buckets) > 4096:
                try: buckets.popitem(last=False)
                except KeyError: pass
            return
        tokens, last = ent
        tokens = min(cap, tokens + (now - last) * refill)
        if tokens < 1.0:
            ent[0], ent[1] = tokens, now
            buckets.move_to_end(client_ip)  # P23-A: 仍然更新 LRU 位置
            _rate_limited_counter[label] = _rate_limited_counter.get(label, 0) + 1
            # P24-B: Retry-After = 距离 1 token 还需要多少秒（向上取整，最少 1）
            need = max(0.0, 1.0 - tokens)
            retry_after = max(1, int(need / refill) + 1) if refill > 0 else 1
            raise HTTPException(429, f"rate limit exceeded (intent-tags/{label})",
                                headers={"Retry-After": str(retry_after)})
        ent[0], ent[1] = tokens - 1.0, now
        buckets.move_to_end(client_ip)  # P23-A: 标记最近活动

    # P24-A / P25-C / P26-D: audit 防抖 — 同 (ip, endpoint) 在 1s 内最多记一次。
    # P26-D 把 OrderedDict + Lock 实现抽到 src/utils/audit_throttle.py 复用。
    from src.utils.audit_throttle import AuditThrottle
    _audit_throttle = AuditThrottle(window_sec=1.0, max_keys=4096)

    def _reset_rate_limit_state() -> None:
        """P22-B / P23-D / P24-A: 测试用 — 清空 buckets / counters / sweep / audit 防抖。"""
        _diff_buckets.clear()
        _restore_buckets.clear()
        _metrics_buckets.clear()
        _audit_throttle.clear()
        _rate_limited_counter["diff"] = 0
        _rate_limited_counter["restore"] = 0
        _rate_limited_counter["metrics"] = 0
        _bucket_last_sweep["diff"] = 0.0
        _bucket_last_sweep["restore"] = 0.0
        _bucket_last_sweep["metrics"] = 0.0

    # 暴露给测试（通过 app.state 挂载）
    app.state.intent_tags_rate_limit_reset = _reset_rate_limit_state

    # P24-C: body size 上限（防大 JSON 攻击）
    # /diff /restore 接收用户提交的 YAML/文件名，正常 < 1MB；超 2MB 直接拒绝
    # /write 接收完整字典 YAML，正常 < 1MB；上限放宽到 4MB
    _DEFAULT_MAX_BODY_BYTES = 2 * 1024 * 1024
    _WRITE_MAX_BODY_BYTES = 4 * 1024 * 1024

    async def _read_json_body(request: Request, max_bytes: int = _DEFAULT_MAX_BODY_BYTES) -> dict:
        """P24-C: 读取 JSON body 并强制大小上限。
        - 优先看 Content-Length header（快速失败，不浪费带宽）
        - 真实读取后再次确认 byte 长度（防伪 header）
        - 返回 dict（非 dict 抛 400）
        """
        import json as _json
        cl = request.headers.get("content-length")
        if cl:
            try:
                if int(cl) > max_bytes:
                    raise HTTPException(413,
                        f"request body too large (got {cl}, max {max_bytes})")
            except (ValueError, TypeError):
                pass  # 没 header 或不合法 → 读后再判
        raw = await request.body()
        if len(raw) > max_bytes:
            raise HTTPException(413,
                f"request body too large ({len(raw)}, max {max_bytes})")
        try:
            data = _json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            raise HTTPException(400, "invalid JSON body")
        if not isinstance(data, dict):
            raise HTTPException(400, "JSON body must be an object")
        return data

    def _audit_rate_limited(request: Request, label: str, client_ip: str) -> None:
        """P23-B / P24-A / P25-C / P26-D: 限流命中写 audit log（best-effort + 1s 防抖）。

        P26-D: throttle 逻辑下沉到 src.utils.audit_throttle.AuditThrottle。
        """
        try:
            if not _audit_throttle.should_emit((client_ip, label)):
                return
            audit_store = getattr(request.app.state, "audit_store", None)
            if audit_store is None:
                return
            actor = getattr(getattr(request.state, "user", None), "username", "unknown")
            audit_store.log(actor, "rpa_intent_tags_rate_limited",
                            f"endpoint={label} ip={client_ip}")
        except Exception:
            pass

    def _diff_rate_limit_check(request: Request, client_ip: str) -> None:
        cap, refill = _read_rate_cfg("diff", 20.0, 2.0)
        try:
            _bucket_check(_diff_buckets, cap, refill, client_ip, "diff")
        except HTTPException:
            _audit_rate_limited(request, "diff", client_ip)
            raise

    def _restore_rate_limit_check(request: Request, client_ip: str) -> None:
        # /restore 是写操作 — 严格得多：capacity=5, refill=0.2/s (每 5s 1 个)
        cap, refill = _read_rate_cfg("restore", 5.0, 0.2)
        try:
            _bucket_check(_restore_buckets, cap, refill, client_ip, "restore")
        except HTTPException:
            _audit_rate_limited(request, "restore", client_ip)
            raise

    def _metrics_rate_limit_check(request: Request, client_ip: str) -> None:
        # P23-D: /metrics 宽松限流 — Prometheus scrape 通常 15s/次很安全，
        # 默认 capacity=100, refill=1/s（足够多套监控系统拉，仍能挡 DoS）
        cap, refill = _read_rate_cfg("metrics", 100.0, 1.0)
        try:
            _bucket_check(_metrics_buckets, cap, refill, client_ip, "metrics")
        except HTTPException:
            _audit_rate_limited(request, "metrics", client_ip)
            raise

    @app.post("/api/rpa/intent-tags/diff")
    async def api_rpa_intent_tags_diff(request: Request):
        """P17-B: 保存前预览 — 计算提交内容 vs 当前运行时字典的差异。
        请求体: {"content": "<yaml 文本>"} → 不写入文件，只返回 added/removed。

        P20-B / P21-A/B: per-IP 限流（默认 capacity=20, refill=2/s；
        config.yaml::rpa.rate_limits.diff 可覆盖；P21-A 支持 XFF）。
        """
        api_auth(request)
        _diff_rate_limit_check(request, _client_ip_of(request))
        # P24-C: body 上限 2MB
        body = await _read_json_body(request)
        content = body.get("content")
        if not isinstance(content, str):
            raise HTTPException(400, "content (str) required")
        from src.integrations.rpa_shared import diff_intent_tags
        try:
            return diff_intent_tags(content)
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.get("/api/rpa/intent-tags/backups")
    async def api_rpa_intent_tags_backups(request: Request):
        """P17-D: 列出可恢复的备份（按 mtime 倒序）。"""
        api_auth(request)
        from src.integrations.rpa_shared import list_intent_tags_backups
        return {"ok": True, "backups": list_intent_tags_backups()}

    @app.post("/api/rpa/intent-tags/restore")
    async def api_rpa_intent_tags_restore(request: Request):
        """P17-D: 从指定备份文件恢复（写当前文件 + 旋转备份 + reload）。
        P18-A: 支持 dry_run=true → 只返回 diff，不写文件。
        P21-B: per-IP 限流（默认 capacity=5, refill=0.2/s — 远比 diff 严格）。

        请求体: {"filename": "intent_tags.yaml.bak20260521_054300", "dry_run": false}

        P23-A: dry_run 不计入 restore bucket（但走 diff bucket — 它本质是 diff 操作）。
        """
        api_auth(request)
        # P24-C: body 上限 2MB (filename + dry_run flag 通常 < 1KB)
        body = await _read_json_body(request)
        filename = body.get("filename")
        if not isinstance(filename, str) or not filename:
            raise HTTPException(400, "filename (str) required")
        dry_run = bool(body.get("dry_run", False)) if isinstance(body, dict) else False
        client_ip = _client_ip_of(request)
        # P23-A: dry_run 走 diff 限流（它本质是预览）；真恢复才走严格 restore 限流
        if dry_run:
            _diff_rate_limit_check(request, client_ip)
        else:
            _restore_rate_limit_check(request, client_ip)

        if dry_run:
            # P18-A: 仅 diff，不动文件
            from src.integrations.rpa_shared import read_intent_tags_backup, diff_intent_tags
            try:
                content = read_intent_tags_backup(filename)
                diff = diff_intent_tags(content)
            except ValueError as exc:
                raise HTTPException(400, str(exc))
            diff["dry_run"] = True
            diff["preview_from"] = filename
            return diff

        from src.integrations.rpa_shared import restore_intent_tags_backup
        try:
            result = restore_intent_tags_backup(filename)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        # 审计
        audit_store = getattr(request.app.state, "audit_store", None)
        if audit_store is not None:
            try:
                actor = getattr(getattr(request.state, "user", None), "username", "unknown")
                audit_store.log(actor, "rpa_intent_tags_restore", f"from={filename}")
            except Exception:
                pass
        result["restored_from"] = filename
        return result

    @app.post("/api/rpa/intent-tags/reload")
    async def api_rpa_intent_tags_reload(request: Request):
        """运营修改 config/intent_tags.yaml 后调一下，无需重启进程。"""
        api_auth(request)
        from src.integrations import rpa_shared as _shr
        before_kw = sum(len(v) for v in _shr._INTENT_TAGS.values())
        tags = _shr.reload_intent_tags()
        after_kw = sum(len(v) for v in tags.values())
        # 审计日志（如有 audit_store）
        audit_store = getattr(request.app.state, "audit_store", None)
        if audit_store is not None:
            try:
                actor = getattr(getattr(request.state, "user", None), "username", "unknown")
                audit_store.log(actor, "rpa_intent_tags_reload",
                                f"before={before_kw} after={after_kw}")
            except Exception:
                pass
        return {
            "ok": True, "category_count": len(tags),
            "keyword_count": after_kw, "delta": after_kw - before_kw,
        }

    # ════════════════════════════════════════════════════════════════════
    # P15-B: Prometheus 指标导出（overview 级 — 缓存命中率等）
    # ════════════════════════════════════════════════════════════════════

    @app.get("/api/rpa/metrics", response_class=PlainTextResponse)
    async def api_rpa_metrics(request: Request):
        """Prometheus 文本格式 — 暴露 cache/intent_tags/rate-limit/persistence 指标。

        P23-D: 宽松限流（默认 capacity=100, refill=1/s）— Prometheus scrape 友好但挡 DoS。
        """
        api_auth(request)
        _metrics_rate_limit_check(request, _client_ip_of(request))
        lines: List[str] = []
        st = _XPROF_CACHE_STATS
        total = st["hits"] + st["misses"]
        ratio = (st["hits"] / total) if total else 0.0
        lines += [
            "# HELP rpa_xprof_cache_hits_total Cache hits for cross-platform-profile",
            "# TYPE rpa_xprof_cache_hits_total counter",
            f"rpa_xprof_cache_hits_total {st['hits']}",
            "# HELP rpa_xprof_cache_misses_total Cache misses for cross-platform-profile",
            "# TYPE rpa_xprof_cache_misses_total counter",
            f"rpa_xprof_cache_misses_total {st['misses']}",
            "# HELP rpa_xprof_cache_evictions_total FIFO evictions when cache is full",
            "# TYPE rpa_xprof_cache_evictions_total counter",
            f"rpa_xprof_cache_evictions_total {st['evictions']}",
            "# HELP rpa_xprof_cache_expired_total Entries dropped because of TTL",
            "# TYPE rpa_xprof_cache_expired_total counter",
            f"rpa_xprof_cache_expired_total {st['expired']}",
            "# HELP rpa_xprof_cache_size Current entries in the in-process cache",
            "# TYPE rpa_xprof_cache_size gauge",
            f"rpa_xprof_cache_size {len(_XPROF_CACHE)}",
            "# HELP rpa_xprof_cache_hit_ratio Hit / (hit+miss); 0..1",
            "# TYPE rpa_xprof_cache_hit_ratio gauge",
            f"rpa_xprof_cache_hit_ratio {ratio:.4f}",
        ]
        # P18-B: intent_tags 编辑活动指标
        from src.integrations.rpa_shared import get_intent_tags_edit_stats, _INTENT_TAGS
        es = get_intent_tags_edit_stats()
        lines += [
            "# HELP rpa_intent_tags_writes_total Successful writes to intent_tags.yaml",
            "# TYPE rpa_intent_tags_writes_total counter",
            f"rpa_intent_tags_writes_total {es.get('writes', 0)}",
            "# HELP rpa_intent_tags_reloads_total Hot reloads of intent_tags",
            "# TYPE rpa_intent_tags_reloads_total counter",
            f"rpa_intent_tags_reloads_total {es.get('reloads', 0)}",
            "# HELP rpa_intent_tags_restores_total Restores from backup",
            "# TYPE rpa_intent_tags_restores_total counter",
            f"rpa_intent_tags_restores_total {es.get('restores', 0)}",
            "# HELP rpa_intent_tags_last_edit_ts Unix ts of last write (0 if never)",
            "# TYPE rpa_intent_tags_last_edit_ts gauge",
            f"rpa_intent_tags_last_edit_ts {es.get('last_edit_ts', 0.0):.0f}",
            "# HELP rpa_intent_tags_edits_1h Writes in the past 1 hour (sliding window)",
            "# TYPE rpa_intent_tags_edits_1h gauge",
            f"rpa_intent_tags_edits_1h {es.get('edits_1h', 0)}",
            "# HELP rpa_intent_tags_category_count Current runtime category count",
            "# TYPE rpa_intent_tags_category_count gauge",
            f"rpa_intent_tags_category_count {len(_INTENT_TAGS)}",
            "# HELP rpa_intent_tags_keyword_count Current runtime keyword total",
            "# TYPE rpa_intent_tags_keyword_count gauge",
            f"rpa_intent_tags_keyword_count {sum(len(v) for v in _INTENT_TAGS.values())}",
            # P21-C: rate-limit rejection counters (label = endpoint name)
            "# HELP rpa_intent_tags_rate_limited_total Requests rejected by per-IP rate limiter",
            "# TYPE rpa_intent_tags_rate_limited_total counter",
            f'rpa_intent_tags_rate_limited_total{{endpoint="diff"}} {_rate_limited_counter.get("diff", 0)}',
            f'rpa_intent_tags_rate_limited_total{{endpoint="restore"}} {_rate_limited_counter.get("restore", 0)}',
            f'rpa_intent_tags_rate_limited_total{{endpoint="metrics"}} {_rate_limited_counter.get("metrics", 0)}',
            # P22-C: 持久化失败指标
            "# HELP rpa_intent_tags_save_failures_total Cumulative stats sidecar save failures",
            "# TYPE rpa_intent_tags_save_failures_total counter",
            f"rpa_intent_tags_save_failures_total {es.get('save_failures_total', 0)}",
            "# HELP rpa_intent_tags_save_failures_consecutive Failures since last success",
            "# TYPE rpa_intent_tags_save_failures_consecutive gauge",
            f"rpa_intent_tags_save_failures_consecutive {es.get('save_failures_consecutive', 0)}",
        ]
        # P26-A: intent_tags 自动 reload (watchdog) 统计
        try:
            from src.integrations.intent_tags_watcher import get_reload_stats, is_running
            _wst = get_reload_stats()
            lines += [
                "# HELP rpa_intent_tags_auto_reloads_total Auto reloads triggered by watchdog",
                "# TYPE rpa_intent_tags_auto_reloads_total counter",
                f"rpa_intent_tags_auto_reloads_total {_wst.get('auto_reloads_total', 0)}",
                "# HELP rpa_intent_tags_auto_reload_failures_total Auto reload exceptions",
                "# TYPE rpa_intent_tags_auto_reload_failures_total counter",
                f"rpa_intent_tags_auto_reload_failures_total {_wst.get('auto_reload_failures', 0)}",
                "# HELP rpa_intent_tags_auto_reload_events_debounced Editor events coalesced",
                "# TYPE rpa_intent_tags_auto_reload_events_debounced counter",
                f"rpa_intent_tags_auto_reload_events_debounced {_wst.get('events_debounced', 0)}",
                "# HELP rpa_intent_tags_watcher_running 1 iff watchdog thread is up",
                "# TYPE rpa_intent_tags_watcher_running gauge",
                f"rpa_intent_tags_watcher_running {1 if is_running() else 0}",
            ]
        except Exception:
            pass
        # P26-B: web 中间件 413 拒绝计数（按 path label 聚合）
        oversize_counter = getattr(request.app.state, "web_body_oversize_counter", {}) or {}
        lines += [
            "# HELP web_body_oversize_rejected_total POST/PUT/PATCH requests rejected by body size middleware",
            "# TYPE web_body_oversize_rejected_total counter",
        ]
        if oversize_counter:
            for _path, _n in sorted(oversize_counter.items()):
                # Prometheus label sanitization: escape backslash + quote
                _safe = _path.replace("\\", "\\\\").replace('"', '\\"')
                lines.append(f'web_body_oversize_rejected_total{{path="{_safe}"}} {_n}')
        else:
            # 即使无攻击，也输出一行总计 0 — 让 Grafana 不抱怨缺指标
            lines.append('web_body_oversize_rejected_total{path=""} 0')
        return "\n".join(lines) + "\n"

    # ════════════════════════════════════════════════════════════════════
    # 跨平台运行控制 — 统一 toggle / pause / resume / trigger
    # ════════════════════════════════════════════════════════════════════

    @app.post("/api/rpa-overview/control")
    async def api_rpa_overview_control(request: Request):
        """跨平台运行控制代理。

        请求体::

            {
                "platform": "line" | "messenger" | "whatsapp",
                "action":   "start" | "stop" | "pause" | "resume" | "trigger",
                "seconds":  300          // 仅 pause 时使用，默认 300
            }

        - ``start``  — 强制拉起 service loop（绕过 autostart 检查）
        - ``stop``   — 停止 service loop
        - ``trigger`` — 若 loop 未运行先 force_start，再设 trigger event
        - ``pause``  — 暂停 N 秒
        - ``resume`` — 取消暂停

        返回 ``{"ok": true, "platform": ..., "action": ..., "is_running": ...}``。
        Telegram 走 MTProto 直连，不支持控制。
        """
        api_auth(request)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "invalid JSON")

        platform = str(body.get("platform", "")).lower().strip()
        action = str(body.get("action", "")).lower().strip()
        seconds = float(body.get("seconds", 300) or 300)

        valid_actions = {"start", "stop", "pause", "resume", "trigger"}
        if action not in valid_actions:
            raise HTTPException(400, tr(request, "err.rpa.action_must_be", actions='/'.join(sorted(valid_actions)), got=action))

        # 查找目标 service
        svc = None
        if platform == "line":
            svc = _get_line_service(request)
        elif platform == "messenger":
            svc = _get_messenger_service(request)
        elif platform == "whatsapp":
            svc = _get_whatsapp_service(request)
        elif platform == "telegram":
            raise HTTPException(400, tr(request, "err.rpa.telegram_no_control"))
        else:
            raise HTTPException(400, tr(request, "err.rpa.unknown_platform", platform=platform))

        if svc is None:
            raise HTTPException(503, tr(request, "err.rpa.service_not_built", platform=platform))

        result: Dict[str, Any] = {"ok": True, "platform": platform, "action": action}

        if action == "start":
            if hasattr(svc, "force_start"):
                started = await svc.force_start()
            else:
                started = await svc.start()
            result["started"] = started

        elif action == "stop":
            await svc.stop()

        elif action == "pause":
            svc.pause_for(max(0.0, seconds))
            result["pause_seconds"] = int(seconds)

        elif action == "resume":
            svc.resume()
            # 如果 loop 没跑，resume 只清 pause flag，还得拉起 loop
            if hasattr(svc, "is_running") and not svc.is_running:
                if hasattr(svc, "force_start"):
                    await svc.force_start()

        elif action == "trigger":
            # 如果 loop 没跑，先拉起再 trigger
            if hasattr(svc, "is_running") and not svc.is_running:
                if hasattr(svc, "force_start"):
                    started = await svc.force_start()
                    result["auto_started"] = started
            if hasattr(svc, "trigger_once"):
                import asyncio as _aio
                if _aio.iscoroutinefunction(svc.trigger_once):
                    await svc.trigger_once()
                else:
                    svc.trigger_once()

        # 返回当前运行状态
        result["is_running"] = bool(
            getattr(svc, "is_running", False)
            if hasattr(svc, "is_running")
            else (getattr(svc, "_task", None) and not svc._task.done())
        )

        # 审计
        audit_store = getattr(request.app.state, "audit_store", None)
        if audit_store:
            try:
                actor = getattr(getattr(request.state, "user", None), "username", "web")
                detail = f"platform={platform} action={action}"
                if action == "pause":
                    detail += f" seconds={int(seconds)}"
                audit_store.log(actor, "rpa_overview_control", detail)
            except Exception:
                pass

        return result

    # ════════════════════════════════════════════════════════════════════
    # 7. 设备状态 API（DeviceCoordinator + HotPlug）
    # ════════════════════════════════════════════════════════════════════

    @app.get("/api/rpa-overview/devices")
    async def api_rpa_overview_devices(request: Request):
        """返回所有设备的实时状态（静态+热插拔）。"""
        api_auth(request)
        result: Dict[str, Any] = {"static": [], "hotplug": []}

        dc_svc = getattr(request.app.state, "device_coordinator_service", None)
        if dc_svc:
            result["static"] = dc_svc.status()

        hp = getattr(request.app.state, "hotplug_watcher", None)
        if hp:
            result["hotplug"] = hp.status()

        return result

    @app.get("/api/rpa-overview/registry")
    async def api_rpa_overview_registry(request: Request):
        """返回设备注册表全量信息。"""
        api_auth(request)
        from src.shared.device_registry import get_device_registry
        reg = get_device_registry()
        return {"devices": reg.all()}

    @app.post("/api/rpa-overview/registry")
    async def api_rpa_overview_registry_upsert(request: Request):
        """注册或更新设备（serial + 平台分配）。变更后自动热重建 Coordinator。"""
        api_auth(request)
        body = await request.json()
        serial = (body.get("serial") or "").strip()
        if not serial:
            raise HTTPException(400, tr(request, "err.ws.field_required", field="serial"))
        from src.shared.device_registry import get_device_registry
        reg = get_device_registry()
        fields: Dict[str, Any] = {}
        for k in ("label", "group_name", "number", "wifi_ip",
                  "platform_messenger", "platform_line", "platform_whatsapp",
                  "persona_messenger", "persona_line", "persona_whatsapp"):
            if k in body:
                fields[k] = body[k]
        dev = reg.upsert(serial, **fields)

        # 热重建：平台或人设变更后立即重建 Coordinator（无需等待 15s 扫描周期）
        reload_result = None
        if any(k.startswith("platform_") or k.startswith("persona_") for k in fields):
            hp = getattr(request.app.state, "hotplug_watcher", None)
            dc = getattr(request.app.state, "device_coordinator_service", None)
            if hp:
                try:
                    reload_result = await hp.reload_device(serial)
                except Exception as ex:
                    reload_result = {"ok": False, "error": str(ex)}
            if (not reload_result or not reload_result.get("ok")) and dc:
                try:
                    reload_result = await dc.rebuild_from_registry(serial)
                except Exception as ex:
                    reload_result = {"ok": False, "error": str(ex)}

        return {"ok": True, "device": dev, "reload": reload_result}

    @app.post("/api/rpa-overview/registry/{serial}/auto-detect")
    async def api_auto_detect_platforms(serial: str, request: Request):
        """ADB 扫描指定设备上已安装的聊天 app，自动写入 registry 并热重建 Coordinator。"""
        api_auth(request)
        from src.shared.device_registry import get_device_registry
        from src.integrations.line_rpa.adb_helpers import (
            detect_installed_chat_apps,
            get_chat_account_name,
        )

        reg = get_device_registry()
        dev_info = reg.get(serial)
        if not dev_info:
            raise HTTPException(404, tr(request, "err.rpa.device_not_registered", serial=serial))

        label = (
            (dev_info.get("label") or serial[:8])
            .lower().replace("-", "_").replace(" ", "_")
        )

        try:
            installed = await asyncio.to_thread(detect_installed_chat_apps, serial)
        except Exception as exc:
            raise HTTPException(500, tr(request, "err.rpa.adb_check_failed", err=exc))

        if not any(installed.values()):
            return {"ok": True, "detected": {}, "message": "未检测到已安装的聊天 app（确认设备已连接且 ADB 授权）"}

        _PREFIX = {"messenger": "msg", "line": "line", "whatsapp": "wa"}
        updates: Dict[str, str] = {}
        detected: Dict[str, Any] = {}

        for ptype in ["messenger", "line", "whatsapp"]:
            if not installed.get(ptype):
                continue
            account_id = f"{_PREFIX[ptype]}_{label}"
            try:
                acct_name = await asyncio.to_thread(get_chat_account_name, serial, ptype)
            except Exception:
                acct_name = None
            updates[f"platform_{ptype}"] = account_id
            detected[ptype] = {"account_id": account_id, "account_name": acct_name}

        if updates:
            reg.upsert(serial, **updates)

        reload_result = None
        if updates:
            hp = getattr(request.app.state, "hotplug_watcher", None)
            dc = getattr(request.app.state, "device_coordinator_service", None)
            if hp:
                try:
                    reload_result = await hp.reload_device(serial)
                except Exception as ex:
                    reload_result = {"ok": False, "error": str(ex)}
            if (not reload_result or not reload_result.get("ok")) and dc:
                try:
                    reload_result = await dc.rebuild_from_registry(serial)
                except Exception as ex:
                    reload_result = {"ok": False, "error": str(ex)}

        return {"ok": True, "detected": detected, "reload": reload_result}

    @app.get("/api/personas/list")
    async def api_personas_list(request: Request):
        """返回所有可选人设 profile（供设备编排 UI 下拉框使用）。"""
        api_auth(request)
        try:
            from src.utils.persona_manager import PersonaManager
            pm = PersonaManager.get_instance()
            profiles = pm.list_profiles_summary()
            return {"ok": True, "profiles": profiles}
        except Exception as exc:
            return {"ok": False, "profiles": [], "error": str(exc)}

    # ════════════════════════════════════════════════════════════════════
    # 8. Registry 同步 API（供 tools/sync_registry.py pull 调用）
    # ════════════════════════════════════════════════════════════════════

    @app.get("/api/registry/export")
    async def api_registry_export(request: Request):
        """导出全量 registry（供从机同步拉取）。"""
        api_auth(request)
        import time as _time
        from src.shared.device_registry import get_device_registry
        reg = get_device_registry()
        return {
            "version": 1,
            "ts": _time.strftime("%Y-%m-%dT%H:%M:%S"),
            "devices": reg.all(),
        }

    @app.post("/api/registry/batch")
    async def api_registry_batch(request: Request):
        """批量更新平台分配（选中多台设备 + 统一 platform 配置）。

        Body: {
            "serials": ["SER1", "SER2", ...],
            "fields": {"platform_messenger": "msg_xxx", "platform_line": "", ...}
        }
        """
        api_auth(request)
        body = await request.json()
        serials = body.get("serials") or []
        fields = body.get("fields") or {}
        if not serials:
            raise HTTPException(400, tr(request, "err.ws.field_required", field="serials"))
        if not fields:
            raise HTTPException(400, tr(request, "err.ws.field_required", field="fields"))

        # 过滤允许的字段
        allowed = {"label", "group_name", "number", "wifi_ip",
                   "platform_messenger", "platform_line", "platform_whatsapp",
                   "persona_messenger", "persona_line", "persona_whatsapp"}
        clean_fields = {k: v for k, v in fields.items() if k in allowed}
        if not clean_fields:
            raise HTTPException(400, tr(request, "err.rpa.no_valid_fields"))

        from src.shared.device_registry import get_device_registry
        reg = get_device_registry()
        results = []
        for serial in serials:
            serial = str(serial).strip()
            if not serial:
                continue
            dev = reg.upsert(serial, **clean_fields)
            results.append({"serial": serial, "ok": True})

        # 批量触发热重建（平台或人设变更）
        reload_results = []
        hp = getattr(request.app.state, "hotplug_watcher", None)
        if hp and any(k.startswith("platform_") or k.startswith("persona_") for k in clean_fields):
            for serial in serials:
                serial = str(serial).strip()
                if not serial:
                    continue
                try:
                    r = await hp.reload_device(serial)
                    reload_results.append(r)
                except Exception as ex:
                    reload_results.append({"serial": serial, "ok": False, "error": str(ex)})

        return {
            "ok": True,
            "count": len(results),
            "results": results,
            "reloads": reload_results,
        }

    # ════════════════════════════════════════════════════════════════════
    # 9. 设备统计 API（24h 汇总 + 时间序列图表数据）
    # ════════════════════════════════════════════════════════════════════

    @app.get("/api/rpa-overview/device-stats")
    async def api_device_stats_all(request: Request):
        """所有设备 24h 汇总统计。"""
        api_auth(request)
        hours = float(request.query_params.get("hours", "24"))
        from src.integrations.shared.device_stats import get_device_stats
        return {"summaries": get_device_stats().all_summaries(hours)}

    @app.get("/api/rpa-overview/device-stats/{serial}")
    async def api_device_stats_detail(request: Request, serial: str):
        """单设备统计详情 + 时间序列。"""
        api_auth(request)
        hours = float(request.query_params.get("hours", "6"))
        from src.integrations.shared.device_stats import get_device_stats
        ds = get_device_stats()
        return {
            "summary": ds.device_summary(serial, 24.0),
            "timeseries": ds.device_timeseries(serial, hours),
        }

    # ════════════════════════════════════════════════════════════════════
    # 10. 设备模板 API（预定义平台配置一键应用）
    # ════════════════════════════════════════════════════════════════════

    @app.get("/api/registry/templates")
    async def api_registry_templates(request: Request):
        """返回可用的设备模板列表。"""
        api_auth(request)
        global_cfg = getattr(request.app.state, "config_manager", None)
        cfg = (global_cfg.config if global_cfg else {}) or {}
        templates = cfg.get("device_templates", []) or []
        return {"templates": templates}

    @app.post("/api/registry/apply-template")
    async def api_registry_apply_template(request: Request):
        """将模板应用到一组设备。

        Body: {
            "serials": ["SER1", "SER2"],
            "template_name": "3-platform-full"
        }
        也可直接传 fields 覆盖:
        {
            "serials": ["SER1"],
            "fields": {"platform_messenger": "msg_auto", "platform_line": "line_auto"}
        }
        """
        api_auth(request)
        body = await request.json()
        serials = body.get("serials") or []
        template_name = body.get("template_name", "")
        fields = body.get("fields") or {}

        if not serials:
            raise HTTPException(400, tr(request, "err.ws.field_required", field="serials"))

        # 从模板名查找配置
        if template_name and not fields:
            global_cfg = getattr(request.app.state, "config_manager", None)
            cfg = (global_cfg.config if global_cfg else {}) or {}
            templates = cfg.get("device_templates", []) or []
            tpl = next((t for t in templates if t.get("name") == template_name), None)
            if not tpl:
                raise HTTPException(404, tr(request, "err.rpa.template_not_found", template_name=template_name))
            fields = tpl.get("fields", {})

        if not fields:
            raise HTTPException(400, tr(request, "err.ws.field_required", field="fields"))

        # 过滤允许的字段
        allowed = {"label", "group_name", "number", "wifi_ip",
                   "platform_messenger", "platform_line", "platform_whatsapp",
                   "persona_messenger", "persona_line", "persona_whatsapp"}
        clean_fields = {k: v for k, v in fields.items() if k in allowed}
        if not clean_fields:
            raise HTTPException(400, tr(request, "err.rpa.no_valid_fields"))

        from src.shared.device_registry import get_device_registry
        reg = get_device_registry()
        results = []
        for serial in serials:
            serial = str(serial).strip()
            if not serial:
                continue
            reg.upsert(serial, **clean_fields)
            results.append({"serial": serial, "ok": True})

        # 批量触发热重建（平台或人设变更）
        reload_results = []
        hp = getattr(request.app.state, "hotplug_watcher", None)
        if hp and any(k.startswith("platform_") or k.startswith("persona_") for k in clean_fields):
            for serial in serials:
                serial = str(serial).strip()
                if not serial:
                    continue
                try:
                    r = await hp.reload_device(serial)
                    reload_results.append(r)
                except Exception as ex:
                    reload_results.append({"serial": serial, "ok": False, "error": str(ex)})

        return {
            "ok": True,
            "template": template_name or "(custom)",
            "count": len(results),
            "results": results,
            "reloads": reload_results,
        }

    # ════════════════════════════════════════════════════════════════════
    # 11. 跨主机负载均衡建议 API
    # ════════════════════════════════════════════════════════════════════

    @app.get("/api/rpa-overview/load-balance")
    async def api_load_balance(request: Request):
        """分析各主机设备负载，给出迁移建议。

        Load Score = device_count × (1 + fail_rate)
        如果某主机 score > 平均值 × 1.5，建议迁移低效设备到负载最低的主机。
        """
        api_auth(request)
        from src.shared.device_registry import get_device_registry
        from src.integrations.shared.device_stats import get_device_stats

        reg = get_device_registry()
        ds = get_device_stats()
        all_devices = reg.all()

        # 按 group_name 分组
        from collections import defaultdict
        host_devices: dict = defaultdict(list)
        for dev in all_devices:
            group = dev.get("group_name") or "(未分组)"
            host_devices[group].append(dev)

        # 计算每主机 load score
        host_scores = []
        for host, devices in host_devices.items():
            device_count = len(devices)
            total_runs = 0
            total_fail = 0
            for dev in devices:
                serial = dev.get("serial", "")
                summary = ds.device_summary(serial, 24.0)
                total_runs += summary.get("total_runs", 0)
                for p in summary.get("platforms", []):
                    total_fail += p.get("total_fail", 0)

            fail_rate = (total_fail / total_runs) if total_runs > 0 else 0.0
            score = device_count * (1.0 + fail_rate)
            host_scores.append({
                "host": host,
                "device_count": device_count,
                "total_runs_24h": total_runs,
                "total_fail_24h": total_fail,
                "fail_rate_pct": round(fail_rate * 100, 1),
                "load_score": round(score, 2),
            })

        # 迁移建议
        suggestions = []
        if len(host_scores) >= 2:
            avg_score = sum(h["load_score"] for h in host_scores) / len(host_scores)
            sorted_hosts = sorted(host_scores, key=lambda h: h["load_score"])
            lightest = sorted_hosts[0]
            for h in host_scores:
                if h["load_score"] > avg_score * 1.5 and h["device_count"] > 1:
                    # 建议迁移 1 台到最轻主机
                    suggestions.append({
                        "from_host": h["host"],
                        "to_host": lightest["host"],
                        "reason": f"负载 {h['load_score']:.1f} 超均值 {avg_score:.1f} 的 1.5x",
                        "action": f"建议从 {h['host']} 迁移 1 台设备到 {lightest['host']}",
                    })

        return {
            "hosts": host_scores,
            "suggestions": suggestions,
            "avg_load_score": round(
                sum(h["load_score"] for h in host_scores) / len(host_scores), 2
            ) if host_scores else 0,
        }

    # ════════════════════════════════════════════════════════════════════
    # 11-B. P5-B: 多平台语言分布统计
    # ════════════════════════════════════════════════════════════════════

    @app.get("/api/rpa-overview/lang-dist")
    async def api_rpa_lang_dist(request: Request, refresh: bool = False):
        """汇总所有平台检测到/锁定的语言分布。

        Returns:
          platforms.whatsapp  — {lang: chat_count}，来自 wa_rpa_chat_state.detected_lang
          platforms.messenger — {lang: chat_count}，来自 messenger_rpa_runs.reply_lang (7 天)
          merged              — 两平台合并后的 {lang: count}
          total_chats         — 所有平台有语言记录的对话总数

        Query: ?refresh=true 强制跳过缓存
        """
        api_auth(request)
        global _LANG_DIST_CACHE
        now = time.time()
        if not refresh and _LANG_DIST_CACHE[1] is not None:
            if now - _LANG_DIST_CACHE[0] < _LANG_DIST_CACHE_TTL:
                return _LANG_DIST_CACHE[1]
        dist: Dict[str, Dict[str, int]] = {}

        # ── WhatsApp ──────────────────────────────────────────────────
        for wa_svc in _get_whatsapp_services(request):
            store = getattr(wa_svc, "_state_store", None) or getattr(
                getattr(wa_svc, "_runner", None), "_state_store", None
            )
            if store is None:
                continue
            try:
                with store._lock:
                    rows = store._conn.execute(
                        """
                        SELECT COALESCE(forced_lang, detected_lang) AS lang, COUNT(*) AS cnt
                        FROM wa_rpa_chat_state
                        WHERE COALESCE(forced_lang, detected_lang) IS NOT NULL
                          AND COALESCE(forced_lang, detected_lang) != ''
                        GROUP BY 1
                        """
                    ).fetchall()
                wa_dist = dist.setdefault("whatsapp", {})
                for r in rows:
                    wa_dist[r["lang"]] = wa_dist.get(r["lang"], 0) + r["cnt"]
            except Exception:
                pass

        # ── Messenger ─────────────────────────────────────────────────
        msvc = _get_messenger_service(request)
        if msvc is not None:
            try:
                ss = msvc.state_store
                since = time.time() - 7 * 86400
                with ss._lock, ss._conn() as c:
                    rows = c.execute(
                        """
                        SELECT reply_lang AS lang, COUNT(DISTINCT chat_key) AS cnt
                        FROM messenger_rpa_runs
                        WHERE reply_lang != '' AND reply_lang IS NOT NULL AND ts >= ?
                        GROUP BY reply_lang
                        """,
                        (since,),
                    ).fetchall()
                ms_dist = dist.setdefault("messenger", {})
                for r in rows:
                    ms_dist[r["lang"]] = ms_dist.get(r["lang"], 0) + r["cnt"]
            except Exception:
                pass

        # ── LINE ──────────────────────────────────────────────────────
        line_svc = getattr(request.app.state, "line_rpa_service", None)
        if line_svc is not None:
            try:
                ls = getattr(line_svc, "_state_store", None) or getattr(
                    getattr(line_svc, "_runner", None), "_state_store", None
                )
                if ls is not None:
                    since_line = time.time() - 7 * 86400
                    with ls._lock:
                        rows = ls._conn.execute(
                            """
                            SELECT reply_lang AS lang, COUNT(DISTINCT chat_key) AS cnt
                            FROM line_rpa_runs
                            WHERE reply_lang != '' AND reply_lang IS NOT NULL AND ts >= ?
                            GROUP BY reply_lang
                            """,
                            (since_line,),
                        ).fetchall()
                    ln_dist = dist.setdefault("line", {})
                    for r in rows:
                        ln_dist[r["lang"]] = ln_dist.get(r["lang"], 0) + r["cnt"]
            except Exception:
                pass

        # ── Merge ─────────────────────────────────────────────────────
        merged: Dict[str, int] = {}
        for platform_dist in dist.values():
            for lang, cnt in platform_dist.items():
                merged[lang] = merged.get(lang, 0) + cnt
        merged_sorted = dict(sorted(merged.items(), key=lambda x: -x[1]))
        total_chats = sum(merged.values())

        # P11-E：各平台对话级锁定数（forced_lang IS NOT NULL）
        locked: Dict[str, int] = {}
        try:
            for wa_svc in _get_whatsapp_services(request):
                st = getattr(wa_svc, "_state_store", None) or getattr(
                    getattr(wa_svc, "_runner", None), "_state_store", None
                )
                if st is None:
                    continue
                with st._lock:
                    n = st._conn.execute(
                        "SELECT COUNT(*) AS n FROM wa_rpa_chat_state "
                        "WHERE forced_lang IS NOT NULL AND forced_lang != ''"
                    ).fetchone()["n"]
                locked["whatsapp"] = locked.get("whatsapp", 0) + (n or 0)
        except Exception:
            pass
        try:
            _ls = getattr(line_svc, "_state_store", None) or getattr(
                getattr(line_svc, "_runner", None) if line_svc else None, "_state_store", None
            ) if line_svc else None
            if _ls is not None:
                with _ls._lock:
                    n = _ls._conn.execute(
                        "SELECT COUNT(*) AS n FROM line_rpa_chat_state "
                        "WHERE forced_lang IS NOT NULL AND forced_lang != ''"
                    ).fetchone()["n"]
                locked["line"] = n or 0
        except Exception:
            pass
        try:
            _msvc = getattr(request.app.state, "messenger_rpa_service", None)
            if _msvc is not None:
                _mss = _msvc.state_store
                with _mss._lock, _mss._conn() as _mc:
                    n = _mc.execute(
                        "SELECT COUNT(*) AS n FROM messenger_rpa_chat_state "
                        "WHERE forced_lang IS NOT NULL AND forced_lang != ''"
                    ).fetchone()["n"]
                locked["messenger"] = n or 0
        except Exception:
            pass

        payload = {
            "ok": True,
            "ts": time.time(),
            "platforms": dist,
            "merged": merged_sorted,
            "total_chats": total_chats,
            "locked_chats": locked,
        }
        _LANG_DIST_CACHE = (time.time(), payload)
        return payload

    # ════════════════════════════════════════════════════════════════════
    # 11-D. P12-C: lang-dist 版本号（零 DB 查询，供 5s 轻量轮询）
    # ════════════════════════════════════════════════════════════════════

    @app.get("/api/rpa-overview/lang-dist-version")
    async def api_rpa_lang_dist_version(request: Request):
        """P12-C: 返回当前 lang-dist 版本号（纯内存，无 DB）。
        前端以 5s 间隔轮询，版本变化时触发完整 lang-dist 刷新。"""
        api_auth(request)
        return {"ok": True, "v": _LANG_DIST_VERSION}

    # ════════════════════════════════════════════════════════════════════
    # 11-C. P9-C: 7 天语言对话趋势（sparkline 数据源）
    # ════════════════════════════════════════════════════════════════════

    @app.get("/api/rpa-overview/lang-trend")
    async def api_rpa_lang_trend(request: Request):
        """7 天内每平台每天有效对话数（reply_lang != '' 或有 peer_text），
        供语言分布面板 sparkline 渲染。"""
        api_auth(request)
        import datetime as _dt
        now = time.time()
        since = now - 7 * 86400
        today = _dt.date.today()
        days = [(today - _dt.timedelta(days=i)).isoformat() for i in range(6, -1, -1)]
        by_platform: Dict[str, List[int]] = {}

        # ── WhatsApp ──────────────────────────────────────────────────
        for wa_svc in _get_whatsapp_services(request):
            store = getattr(wa_svc, "_state_store", None) or getattr(
                getattr(wa_svc, "_runner", None), "_state_store", None
            )
            if store is None:
                continue
            try:
                with store._lock:
                    rows = store._conn.execute(
                        """
                        SELECT date(ts,'unixepoch','localtime') AS day,
                               COUNT(DISTINCT chat_key) AS cnt
                        FROM wa_rpa_runs
                        WHERE ts >= ? AND peer_text != ''
                        GROUP BY day
                        """,
                        (since,),
                    ).fetchall()
                dm = {r["day"]: r["cnt"] for r in rows}
                wa = by_platform.setdefault("whatsapp", [0] * 7)
                for i, d in enumerate(days):
                    wa[i] += dm.get(d, 0)
            except Exception:
                pass

        # ── Messenger ─────────────────────────────────────────────────
        msvc = _get_messenger_service(request)
        if msvc is not None:
            try:
                ss = msvc.state_store
                with ss._lock, ss._conn() as c:
                    rows = c.execute(
                        """
                        SELECT date(ts,'unixepoch','localtime') AS day,
                               COUNT(DISTINCT chat_key) AS cnt
                        FROM messenger_rpa_runs
                        WHERE ts >= ? AND peer_text != ''
                        GROUP BY day
                        """,
                        (since,),
                    ).fetchall()
                dm = {r["day"]: r["cnt"] for r in rows}
                ms = by_platform.setdefault("messenger", [0] * 7)
                for i, d in enumerate(days):
                    ms[i] += dm.get(d, 0)
            except Exception:
                pass

        # ── LINE ──────────────────────────────────────────────────────
        line_svc = getattr(request.app.state, "line_rpa_service", None)
        if line_svc is not None:
            try:
                ls = getattr(line_svc, "_state_store", None) or getattr(
                    getattr(line_svc, "_runner", None), "_state_store", None
                )
                if ls is not None:
                    with ls._lock:
                        rows = ls._conn.execute(
                            """
                            SELECT date(ts,'unixepoch','localtime') AS day,
                                   COUNT(DISTINCT chat_key) AS cnt
                            FROM line_rpa_runs
                            WHERE ts >= ? AND peer_text != ''
                            GROUP BY day
                            """,
                            (since,),
                        ).fetchall()
                    dm = {r["day"]: r["cnt"] for r in rows}
                    ln = by_platform.setdefault("line", [0] * 7)
                    for i, d in enumerate(days):
                        ln[i] += dm.get(d, 0)
            except Exception:
                pass

        return {"ok": True, "days": days, "by_platform": by_platform}

    # ════════════════════════════════════════════════════════════════════
    # 12. SSE 实时事件流（替代轮询，推送设备状态变更/熔断/恢复）
    # ════════════════════════════════════════════════════════════════════

    @app.get("/api/rpa-overview/events")
    async def api_sse_events(request: Request):
        """Server-Sent Events 端点 — 实时推送设备事件。"""
        import json as _json
        from starlette.responses import StreamingResponse
        from src.integrations.shared.event_bus import get_event_bus

        bus = get_event_bus()
        queue = bus.subscribe()

        async def event_generator():
            try:
                # 先发送最近事件作为 replay
                for evt in bus.recent_events(10):
                    yield f"data: {_json.dumps(evt, ensure_ascii=False)}\n\n"
                # 持续推送新事件
                while True:
                    try:
                        evt = await asyncio.wait_for(queue.get(), timeout=30.0)
                        yield f"data: {_json.dumps(evt, ensure_ascii=False)}\n\n"
                    except asyncio.TimeoutError:
                        # 心跳保活
                        yield f": heartbeat\n\n"
                    # 检测客户端断开
                    if await request.is_disconnected():
                        break
            finally:
                bus.unsubscribe(queue)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
