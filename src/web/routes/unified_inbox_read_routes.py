"""统一收件箱——主读路径路由域（巨石拆分 slice 37b）。

把 ``register_unified_inbox_routes`` 巨型闭包中物理分离的主读端点外移为
``register_read_routes(app, *, api_auth, config_manager=None)``，由主 register
在 chats 原位置调用：

- ``unified-inbox/chats``：会话列表（聚合 + contacts/SLA/tags/assignment 富集）
- ``unified-inbox/thread``：会话线程（store 读路径 + 入站自动翻译 + 出向原文富集）

端点路径/方法/响应零变化（admin_route_inventory URL 契约守卫 + slice 37b 端点契约断言）。

依赖全部朝下：aggregate 读路径族 + ``_enrich_outbound_originals``（slice 37b 下沉）、
services、sla、channel_adapters.status_via_adapters、normalizer、inbound_translate。
收 api_auth + config_manager（assignment / 入站翻译配置）。
"""

from __future__ import annotations

import logging
import asyncio
import time
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, Request

from src.inbox.channel_adapters import status_via_adapters
from src.inbox.normalizer import (
    candidate_messages_from_source,
    conv_id,
    message_obj,
    name_is_real,
)
from src.web.routes.unified_inbox_aggregate import (
    _INBOX_ADAPTERS,
    _chats_for_listing,
    _collect_all_chats,
    _enrich_outbound_originals,
    _ingest_thread_best_effort,
    _is_protocol_account,
    _overlay_store_identity,
    _read_from_store_enabled,
    _store_conv_as_chat,
    _thread_messages_from_store,
)
from src.web.routes.unified_inbox_services import (
    _contacts_store,
    _get_telegram_client,
    _get_translation_service,
    _inbox_store,
)
from src.web.routes.unified_inbox_sla import _sla_cfg
from src.web.web_i18n import tr

logger = logging.getLogger(__name__)

# ── F3：资料面板就绪度观测（打开会话时按平台记文字身份完整度）──────────────
# 去重集：同一 conversation_id 每进程只记一次（避免自适应轮询/加载更早重复计数把 opens 撑爆），
# bounded 防内存无界（超上限后新会话不再记，仅内存保护——是采样 gauge 而非精确总量）。
_PANEL_SEEN: set = set()
_PANEL_SEEN_MAX = 5000


_name_is_real = name_is_real   # 兼容别名（口径已上移到 normalizer.name_is_real，F3/F4 共用）


def _record_panel_identity(chat: Optional[Dict[str, Any]]) -> None:
    """打开会话时记一次资料面板文字身份完整度（去重 + best-effort，绝不影响主流程）。"""
    if not chat:
        return
    try:
        cid = str(chat.get("conversation_id") or "")
        if not cid or cid in _PANEL_SEEN:
            return
        if len(_PANEL_SEEN) >= _PANEL_SEEN_MAX:
            return  # 超上限：新会话不再记（内存保护），已记的仍去重
        _PANEL_SEEN.add(cid)
        from src.web.peer_identity_stats import get_peer_identity_stats
        get_peer_identity_stats().record_panel(
            str(chat.get("platform") or ""),
            has_name=_name_is_real(chat.get("name"), chat.get("chat_key")),
            has_username=bool(str(chat.get("username") or "").strip()),
            has_phone=bool(str(chat.get("phone") or "").strip()),
        )
    except Exception:
        logger.debug("[panel] 资料面板就绪度记录失败（已忽略）", exc_info=True)


def _merge_orchestrator_status(
    platform_status: Dict[str, Any], config_manager,
) -> None:
    """N4：把账号池编排器在管的 protocol/official 账号并入 platform_status。

    inbox 适配器只反映"单连接/RPA"运行态，**不含**扫码登入后由编排器拉起的协议多开号。
    若不并入，连接中心抽屉只会看到 A 线 config 账号（default），看不到扫码新增的号。
    """
    try:
        from src.integrations.account_orchestrator import get_orchestrator
        from src.integrations.account_registry import get_account_registry

        # P2：注册表 label 是账号「人格名」的权威来源（编排器 to_dict 不带 label）。
        # 取一份 label 映射，既给新并入条目命名，也回填既有条目的空 label，
        # 让前端账号切换条 / 会话角标显示用户起的人格名而非裸 account_id。
        label_map: Dict[str, str] = {}
        # P1 身份化：同一趟 registry.list() 里顺手收集自身资料（self_*），末尾注入 platform_status
        profile_map: Dict[str, Dict[str, str]] = {}
        try:
            from src.integrations.account_self_profile import (
                read_self_profile_from_meta,
            )
            for row in get_account_registry().list():
                key = f"{row.get('platform')}:{row.get('account_id')}"
                lbl = str(row.get("label") or "")
                if lbl:
                    label_map[key] = lbl
                prof = read_self_profile_from_meta(row.get("meta") or {})
                if prof:
                    profile_map[key] = prof
        except Exception:
            logger.debug("[chats] 读取注册表 label/self_profile 失败", exc_info=True)

        cfg = (config_manager.config if config_manager is not None else {}) or {}
        for oa in (get_orchestrator(cfg).status().get("accounts") or []):
            plat = oa.get("platform")
            aid = oa.get("account_id")
            if not plat or not aid:
                continue
            # 跳过 stopped 幽灵条目（已从注册表移除但仍滞留 _managed 的占位）；
            # 真正断线的 worker 状态为 error/starting，仍会进抽屉供重连。
            if oa.get("state") == "stopped":
                continue
            running = oa.get("state") == "running"
            key = f"{plat}:{aid}"
            label = label_map.get(key) or oa.get("label") or ""
            existing = platform_status.get(key)
            if existing is None:
                platform_status[key] = {
                    "platform": plat,
                    "account_id": aid,
                    "running": running,
                    "label": label,
                    "mode": oa.get("mode") or "",
                }
            else:
                existing["running"] = bool(existing.get("running")) or running
                # 注册表 label 是用户显式起的人格名 → 覆盖适配器的通用 label
                if label_map.get(key):
                    existing["label"] = label_map[key]

        # 收尾：对所有 platform_status 条目（含未经编排器的 A 线 default）统一用
        # 注册表 label / self_* 覆盖，确保改名 + 真实身份对每个号都即时反映。
        # 关键：适配器可能用裸平台名当 key（如 telegram 适配器 key="telegram"，
        # account_id="default"），而 label_map/profile_map 用 "平台:账号" 组合键，
        # 故这里以条目自身 platform+account_id 派生查找键（回落到字典 key），防漏配。
        for k, v in platform_status.items():
            if not isinstance(v, dict):
                continue
            pkey = f"{v.get('platform') or ''}:{v.get('account_id') or ''}"
            lk = pkey if label_map.get(pkey) else k
            pk = pkey if profile_map.get(pkey) else k
            if label_map.get(lk):
                v["label"] = label_map[lk]
            if profile_map.get(pk):
                for _sk, _sv in profile_map[pk].items():
                    v[_sk] = _sv
    except Exception:
        logger.debug("[chats] 并入编排器账号状态失败", exc_info=True)


def _enrich_chat_list(request: Request, chats: List[Dict[str, Any]], *, config_manager) -> None:
    """Best-effort 富集会话列表：contact 关联 / SLA / tags / 自动派单建议。"""
    try:
        cstore = _contacts_store(request)
        if cstore is not None and chats:
            pairs = [(str(c.get("platform") or ""), str(c.get("chat_key") or ""))
                     for c in chats]
            cmap = cstore.resolve_contacts_by_external(pairs)
            overdue = cstore.overdue_contact_ids()
            for c in chats:
                cid = cmap.get((str(c.get("platform") or ""),
                                str(c.get("chat_key") or "")))
                if cid:
                    c["contact_id"] = cid
                    c["follow_up_overdue"] = cid in overdue
    except Exception:
        logger.debug("会话列表 contact 关联失败（已忽略）", exc_info=True)

    try:
        ibx = _inbox_store(request)
        if ibx is not None and chats:
            sla = _sla_cfg(request)
            cids = [str(c.get("conversation_id") or "") for c in chats]
            dirs = ibx.last_message_dirs([x for x in cids if x])
            now = time.time()
            for c in chats:
                info = dirs.get(str(c.get("conversation_id") or ""))
                # P2：顺手挂最后一条消息方向（in=对方/out=我方），供列表预览前缀，零额外查询
                c["last_direction"] = (info.get("direction") or "") if info else ""
                if info and info.get("direction") == "in":
                    wait = max(0, int(now - (info.get("ts") or now)))
                    c["unanswered_sec"] = wait
                    c["sla_breach"] = wait >= sla["warn"]
                    c["sla_level"] = ("crit" if wait >= sla["crit"]
                                      else "warn" if wait >= sla["warn"] else "")
                else:
                    c["unanswered_sec"] = 0
                    c["sla_breach"] = False
                    c["sla_level"] = ""
    except Exception:
        logger.debug("会话列表 SLA 统计失败（已忽略）", exc_info=True)

    try:
        ibx2 = _inbox_store(request)
        if ibx2 is not None and chats:
            cids2 = [str(c.get("conversation_id") or "") for c in chats if c.get("conversation_id")]
            tags_map = ibx2.list_conv_tags_map(cids2)
            for c in chats:
                cid2 = str(c.get("conversation_id") or "")
                meta2 = tags_map.get(cid2, {})
                c["conv_tags"] = meta2.get("tags", [])
                c["archived"] = meta2.get("archived", False)
                # P0-companion：搁置到点（epoch 秒，0=未搁置）→ 前端「超时/待接管」视图隐藏 + header 搁置态
                c["snooze_until"] = meta2.get("snooze_until", 0)
    except Exception:
        logger.debug("会话列表 tags 加载失败（已忽略）", exc_info=True)

    try:
        # B-2 风控可视：批量标记今日命中风控转人工(blocked)的会话，供列表高亮，
        # 与全自动安全条形成闭环（看到拦截数 → 列表一眼定位被拦会话）。单次 IN 查询。
        ibx3 = _inbox_store(request)
        if ibx3 is not None and chats and hasattr(ibx3, "conversations_blocked_counts"):
            from datetime import datetime as _dt
            _now = _dt.now()
            _since = _dt(_now.year, _now.month, _now.day).timestamp()
            cids3 = [str(c.get("conversation_id") or "") for c in chats if c.get("conversation_id")]
            blocked_map = ibx3.conversations_blocked_counts(cids3, since_ts=_since)
            for c in chats:
                n = blocked_map.get(str(c.get("conversation_id") or ""), 0)
                c["risk_blocked"] = int(n)
    except Exception:
        logger.debug("会话列表风控拦截标记失败（已忽略）", exc_info=True)

    try:
        from src.workspace.assignment import AssignmentService
        asvc = AssignmentService.from_config(
            (config_manager.config if config_manager is not None else {}) or {}
        )
        if asvc.enabled and chats:
            from src.workspace.agent_coordinator import AgentCoordinator
            coord = AgentCoordinator.from_request(request, config_manager)
            sugg = asvc.suggest_for_chats(
                chats=chats,
                presence=coord.list_presence(),
                claims=coord.list_claims(),
            )
            for c in chats:
                s = sugg.get(str(c.get("conversation_id") or ""))
                if s:
                    c["suggested_agent"] = s
    except Exception:
        logger.debug("会话列表自动派单建议失败（已忽略）", exc_info=True)


def register_read_routes(app, *, api_auth, config_manager=None) -> None:
    """挂载主读路径端点（chats GET / thread GET）。"""

    @app.get("/api/unified-inbox/chats")
    async def api_unified_inbox_chats(request: Request, limit: int = 30):
        api_auth(request)
        limit = max(5, min(100, int(limit or 30)))
        chats = _chats_for_listing(request, limit=limit)
        platform_status: Dict[str, Any] = status_via_adapters(request, _INBOX_ADAPTERS)
        _merge_orchestrator_status(platform_status, config_manager)
        _enrich_chat_list(request, chats, config_manager=config_manager)
        return {
            "ok": True,
            "ts": time.time(),
            "chats": chats,
            "platform_status": platform_status,
        }

    @app.get("/api/unified-inbox/thread")
    async def api_unified_inbox_thread(
        request: Request,
        platform: str,
        account_id: str = "default",
        chat_key: str = "",
        limit: int = 50,
        before_ts: float = 0,
    ):
        api_auth(request)
        platform = str(platform or "").lower()
        account_id = str(account_id or "default")
        chat_key = str(chat_key or "")
        if not platform or not chat_key:
            raise HTTPException(400, tr(request, "err.ws.platform_chatkey_required"))
        limit = max(1, min(500, int(limit or 50)))
        before = float(before_ts or 0) or None

        cid = conv_id(platform, account_id, chat_key)
        # 性能修复（根治「加载超时」）：**无条件先按 cid 直读持久层**——只要库里有该会话
        # 历史，就走这条快路(毫秒级)，**跳过**昂贵的全平台 live 聚合（_collect_all_chats
        # 遍历所有适配器 + telegram get_dialogs(100) + 写库，机器负载高时会拖到十几秒→前端
        # 超时）。不再依赖注册表 protocol 判定（该判定失败时旧逻辑会误落慢路径）。
        # 库为空（冷启/纯 live 账号首次）才回落下方完整 live 路径，保证不丢历史、行为兼容。
        target: Optional[Dict[str, Any]] = None
        out_msgs: List[Dict[str, Any]] = []
        _fast = _thread_messages_from_store(
            request, cid, limit=limit, before_ts=before,
        )
        if _fast:
            out_msgs = _fast
            target = _store_conv_as_chat(request, cid)

        if not out_msgs:
            chats = _collect_all_chats(request, limit=100)
            target = next(
                (
                    c for c in chats
                    if c.get("platform") == platform
                    and str(c.get("account_id") or "default") == account_id
                    and str(c.get("chat_key") or "") == chat_key
                ),
                None,
            )
            messages: List[Dict[str, Any]] = []
            if platform == "telegram" and before is None:
                client = _get_telegram_client(request)
                recent = getattr(client, "_recent_messages", None) if client is not None else []
                for idx, m in enumerate(list(recent or [])[-limit:]):
                    if str(m.get("chat_id") or "") != chat_key:
                        continue
                    messages.append(message_obj(
                        text=m.get("text") or "",
                        ts=m.get("ts") or 0,
                        direction="out" if m.get("is_self") else "in",
                        message_id=str(m.get("id") or m.get("message_id") or idx),
                        source=m,
                    ))
            if not messages and target:
                messages = candidate_messages_from_source(target.get("source") or {})
            if not messages and target:
                messages = list(target.get("messages") or [])
            _ingest_thread_best_effort(request, target, messages)
            out_msgs = messages[-limit:]
            store_preferred = (
                _read_from_store_enabled(request)
                or bool(target and target.get("from_store"))
                or _is_protocol_account(request, platform, account_id)
            )
            if store_preferred:
                stored_msgs = _thread_messages_from_store(
                    request, cid, limit=limit, before_ts=before,
                )
                if stored_msgs:
                    out_msgs = stored_msgs
                if target is None:
                    target = _store_conv_as_chat(request, cid)
            elif not out_msgs:
                stored_msgs = _thread_messages_from_store(request, cid, limit=limit)
                if stored_msgs:
                    out_msgs = stored_msgs
                if target is None:
                    target = _store_conv_as_chat(request, cid)

        translate_stats: Dict[str, Any] = {"enabled": False}
        try:
            from src.workspace.inbound_translate import enrich_inbound_translations
            # 硬超时兜底：入站翻译无引擎时会逐条走 LLM(deepseek)，8 条可能 >20s 拖垮 /thread。
            # 限时 6s——译到多少算多少（已译的会缓存，下次打开命中），超时即返回原文，
            # **绝不让加载卡死**。这是 /thread「加载超时」的直接根治。
            out_msgs, translate_stats = await asyncio.wait_for(
                enrich_inbound_translations(
                    request,
                    out_msgs,
                    conversation_id=cid,
                    config_manager=config_manager,
                    translation_svc=_get_translation_service(request),
                ),
                timeout=6.0,
            )
        except asyncio.TimeoutError:
            logger.info("入站自动翻译超时(>6s)，本次返回原文（已译部分下次命中缓存）")
        except Exception:
            logger.debug("入站自动翻译失败（已忽略）", exc_info=True)

        _enrich_outbound_originals(request, cid, out_msgs)

        # F4：live 模式下 target 来自实时聚合（身份常为空），用 store 已持久身份「仅补空」富集，
        # 使 d.chat 携带最新昵称/头像 → 与前端 _mergePeerIdentity（F3）在 live 模式下也能合成。
        # store-backed 的 target（from_store）本就带身份，跳过避免多余回库。
        if target and not target.get("from_store"):
            _overlay_store_identity(request, [target])

        _record_panel_identity(target)   # F3：打开会话→记资料面板文字身份就绪度（去重）

        return {
            "ok": True,
            "chat": target,
            "messages": out_msgs,
            "count": len(out_msgs),
            "has_more": len(out_msgs) >= limit,
            "oldest_ts": out_msgs[0].get("ts") if out_msgs else None,
            "auto_translate": translate_stats,
        }
