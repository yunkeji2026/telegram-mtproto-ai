"""统一收件箱——数据聚合 / 读路径 / 旁路 ingest（巨石拆分 slice 7）。

从 ``unified_inbox_routes.py`` 抽出的**列表/会话读路径与持久层旁路写入**族：
实时聚合（遍历 ChannelAdapter 注册表）、A1 灰度 store-backed 读视图、自动化模式
读写（持久优先回落进程内 dict）、best-effort ingest（冷启动不洪泛 SSE）。

依赖层级：仅依赖 services（_automation_store/_inbox_store）、helpers（AUTOMATION_MODES）
与 inbox 包内单一真源（channel_adapters/normalizer/ingest），不反向依赖 routes，故无
循环 import。``_INBOX_ADAPTERS`` 注册表实例（唯一使用者是本模块）一并下沉。
routes.py 等价重导出，对外引用路径保持不变。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import Request

from src.inbox.channel_adapters import collect_chats_via_adapters, default_inbox_adapters
from src.inbox.ingest import ingest_collected_chats, ingest_thread
from src.inbox.normalizer import store_message_to_obj, store_row_to_chat
from src.web.routes.unified_inbox_helpers import AUTOMATION_MODES
from src.web.routes.unified_inbox_services import _automation_store, _inbox_store

logger = logging.getLogger(__name__)

# A2：渠道适配器注册表（模块级，无状态可复用）。新增渠道在 channel_adapters 注册即可。
_INBOX_ADAPTERS = default_inbox_adapters()


def _read_automation_mode(request: Request, conversation_id: str) -> str:
    """优先读持久层，回落进程内 dict（修掉「重启即丢」生产阻断点）。"""
    store = _inbox_store(request)
    if store is not None:
        try:
            return store.get_automation_mode(conversation_id)
        except Exception:
            logger.debug("inbox_store.get_automation_mode 失败，回落进程内 dict", exc_info=True)
    return _automation_store(request).get(conversation_id, "review")


def _write_automation_mode(request: Request, conversation_id: str, mode: str) -> None:
    store = _inbox_store(request)
    if store is not None:
        try:
            store.set_automation_mode(conversation_id, mode)
            return
        except Exception:
            logger.debug("inbox_store.set_automation_mode 失败，回落进程内 dict", exc_info=True)
    _automation_store(request)[conversation_id] = mode


def _ingest_best_effort(request: Request, chats: List[Dict[str, Any]]) -> None:
    """旁路写入持久层，并在首轮冷启动后开启实时 SSE 事件发布。

    首次调用时 publish_events=False（冷启动不洪泛），之后切换为 True；
    仅有真正新消息（store.ingest_batch n>0）时才发 inbox_message 事件。
    """
    store = _inbox_store(request)
    if store is None or not chats:
        return
    try:
        # 首轮冷启动：向 store 写入存量数据但不发事件（避免把历史消息全部推送）
        first_done = getattr(request.app.state, "_inbox_first_ingest_done", False)
        ingest_collected_chats(store, chats, publish_events=first_done)
        if not first_done:
            request.app.state._inbox_first_ingest_done = True
    except Exception:
        logger.debug("统一收件箱旁路写入失败（已忽略）", exc_info=True)


def _ingest_thread_best_effort(request: Request, chat: Optional[Dict[str, Any]],
                               messages: List[Dict[str, Any]]) -> None:
    store = _inbox_store(request)
    if store is None or not chat or not messages:
        return
    try:
        ingest_thread(store, chat, messages)
    except Exception:
        logger.debug("统一收件箱会话历史写入失败（已忽略）", exc_info=True)


def _collect_all_chats(request: Request, limit: int = 20) -> List[Dict[str, Any]]:
    """从所有平台/账号收集最近对话，返回统一格式列表。

    A2：改为遍历 ChannelAdapter 注册表（src/inbox/channel_adapters.py）。
    新增渠道 = 新增一个适配器并注册，无需改本函数。各平台的取数/字段映射
    封装在各自适配器内，行为与抽取前一致。
    """
    out: List[Dict[str, Any]] = collect_chats_via_adapters(
        request, limit, _INBOX_ADAPTERS,
    )

    out.sort(key=lambda x: x.get("last_ts") or 0, reverse=True)
    out = out[:limit * 4]
    # 旁路写入持久层（best-effort，不改读路径行为）
    _ingest_best_effort(request, out)
    for row in out:
        cid = str(row.get("conversation_id") or "")
        mode = _read_automation_mode(request, cid)
        row["automation_mode"] = mode if mode in AUTOMATION_MODES else "review"
    return out


def _is_protocol_account(request: Request, platform: str, account_id: str) -> bool:
    """该账号是否为 store-backed 模式（消息 push 落库、线程/列表按 store 读出）。

    含两类：``protocol``（编排器接管的真 worker）与 ``desktop``（桌面壳同步桥，无 worker）。
    """
    try:
        from src.integrations.account_registry import get_account_registry
        row = get_account_registry().get(platform, account_id)
        return bool(row and row.get("mode") in ("protocol", "desktop"))
    except Exception:
        return False


def _read_from_store_enabled(request: Request) -> bool:
    """A1 读路径灰度开关：config.inbox.read_from_store（默认 false=实时聚合）。"""
    cm = getattr(request.app.state, "config_manager", None)
    cfg = getattr(cm, "config", None) if cm is not None else None
    if not isinstance(cfg, dict):
        return False
    return bool((cfg.get("inbox") or {}).get("read_from_store", False))


def _collect_chats_from_store(
    request: Request,
    limit: int = 30,
    label_map: Optional[Dict[tuple, str]] = None,
) -> List[Dict[str, Any]]:
    """A1 读路径：直接从 InboxStore（持久事实源）读会话列表，映射回 chat dict 形状。

    ``label_map``：实时聚合派生的 {(platform, account_id): account_label} 友好名映射
    （store 不持久 account_label，借 live 同源回填，消除「列表显示账号 id」可视回归；
    store-only 历史账号 live 无对应项则回落 account_id——live 本也无其 label）。
    返回 None 表示 store 不可用（调用方回落实时聚合）。
    """
    store = _inbox_store(request)
    if store is None:
        return None  # type: ignore[return-value]
    lmap = label_map or {}
    convs = store.list_conversations(limit=min(200, max(1, limit * 4)))
    out: List[Dict[str, Any]] = []
    for c in convs:
        cid = str(c.get("conversation_id") or "")
        mode = _read_automation_mode(request, cid)
        try:
            mc = store.count_messages(cid)
        except Exception:
            mc = 0
        key = (str(c.get("platform") or ""), str(c.get("account_id") or "default"))
        out.append(store_row_to_chat(
            c, automation_mode=mode, message_count=mc,
            account_label=lmap.get(key),
        ))
    return out


def _chats_for_listing(request: Request, limit: int = 30) -> List[Dict[str, Any]]:
    """收件箱列表数据源（A1 灰度）：

    - 始终先跑实时聚合 `_collect_all_chats`（同时旁路 ingest 进 store，保持 store 新鲜）；
    - flag 开 + store 可用：列表改用 store-backed 视图（跨平台/跨重启持久），
      实时聚合的副作用（ingest）已经发生；
    - 否则：返回实时聚合结果（原行为，零变化）。
    """
    live = _collect_all_chats(request, limit=limit)
    if _read_from_store_enabled(request):
        # 借实时聚合结果派生 account_label 友好名映射，store 读路径回填以与 live 等价
        label_map = {
            (str(r.get("platform") or ""), str(r.get("account_id") or "default")):
                str(r.get("account_label") or "")
            for r in live if r.get("account_label")
        }
        stored = _collect_chats_from_store(request, limit=limit, label_map=label_map)
        if stored is not None:
            return stored
    return live


def _thread_messages_from_store(
    request: Request, conversation_id: str, limit: int = 50,
) -> Optional[List[Dict[str, Any]]]:
    """A1 读路径收尾：从 InboxStore 读会话历史（持久事实源），映射回 thread 消息形状。

    返回 None=store 不可用；返回 []=store 中该会话无消息（调用方据此决定是否回落实时）。
    """
    store = _inbox_store(request)
    if store is None:
        return None
    try:
        rows = store.list_recent_messages(conversation_id, limit=limit)
    except Exception:
        logger.debug("store thread 读取失败（已忽略）", exc_info=True)
        return None
    return [store_message_to_obj(r) for r in rows]


def _store_conv_as_chat(request: Request, conversation_id: str) -> Optional[Dict[str, Any]]:
    """从 store 取持久会话行并映射为 chat dict（thread 在实时源已无该会话时兜底 header）。"""
    store = _inbox_store(request)
    if store is None:
        return None
    try:
        row = store.get_conversation(conversation_id)
    except Exception:
        return None
    if not row:
        return None
    mode = _read_automation_mode(request, conversation_id)
    try:
        mc = store.count_messages(conversation_id)
    except Exception:
        mc = 0
    return store_row_to_chat(row, automation_mode=mode, message_count=mc)


def _enrich_outbound_originals(
    request: Request, conversation_id: str, msgs: List[Dict[str, Any]]
) -> None:
    """P1：为出向消息富集坐席输入的中文原文（读 outbound_translations 旁路表）。

    一击直发后实发为译文（消息正文），此处把对应的中文原文 + 翻译质量挂到消息上
    （字段 ``agent_original`` / ``agent_xlate``），供前端持久渲染出向双行。
    best-effort、原地修改，失败/无数据不影响 thread 返回。
    """
    ibx = _inbox_store(request)
    if ibx is None or not conversation_id or not msgs:
        return
    try:
        xmap = ibx.get_outbound_translations(conversation_id)
    except Exception:
        logger.debug("读取 outbound_translations 失败（忽略）", exc_info=True)
        return
    if not xmap:
        return
    import hashlib as _hl
    for m in msgs:
        if not isinstance(m, dict) or str(m.get("direction") or "") != "out":
            continue
        sent = str(m.get("text") or "").strip()
        if not sent:
            continue
        row = xmap.get(_hl.sha256(sent.encode("utf-8")).hexdigest()[:16])
        if not row:
            continue
        orig = str(row.get("original_text") or "").strip()
        if orig and orig != sent:
            m["agent_original"] = orig
            m["agent_xlate"] = {
                "target_lang": row.get("target_lang") or "",
                "source_lang": row.get("source_lang") or "",
                "provider": row.get("provider") or "",
                "error": row.get("error") or "",
            }
