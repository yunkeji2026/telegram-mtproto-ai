"""坐席工作台入站消息自动翻译（Phase 5-3 · P0-3 默认策略）。

策略：仅在坐席打开会话（/thread）时按需翻译，避免全量 ingest 时烧 API。
译文写入 InboxStore.translated_text，跨重启/重开命中缓存。

P0-3 入站译「单一真相源」口径（B6，与 unified_inbox.html 前端约定一致）：
- **后端 enrich（本模块）= 共享预取 + 唯一持久化写路径**：打开会话时把最近 N 条
  外语入站消息译到 cfg.target_lang 并写 store，所有坐席 / 跨重启共享；
- **前端 ``_xlateIn`` = 坐席级显示决策 + 补位**：决定本坐席「要不要看译文 / 看哪种
  语言」，仅对后端未覆盖的消息（超出 max_per_thread 的旧消息滚入视口、两次加载
  之间实时到达的新消息、坐席个性化非默认目标语）逐条走 /translate 补译；
- **去重契约**：前端 ``_xlNeeds`` 对已带同目标语服务端译文的消息绝不重复请求；
  本模块 ``_overlay_store_translations`` + ``_already_translated`` 对 store 已有
  译文绝不重复翻译；两端共用同一 TranslationService（L1/L2 缓存），残余重叠 =
  缓存命中，不产生重复 API 调用。

P0-3 默认翻转（B8）：``enabled`` 未显式配置时不再硬编码 False，而是**跟随引擎
可用性**——存在可用翻译引擎（配了 key 的 DeepL/Google 或就绪的 AI client）默认开；
无引擎环境必须保持关，否则每次开会话都空跑攒 failed 计数 + 前端红徽标。
显式配置 true/false 始终优先于自动判定。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from src.ai.translation_service import TranslationService, detect_language, normalize_lang

logger = logging.getLogger(__name__)

_DEFAULT_CFG: Dict[str, Any] = {
    "enabled": None,          # B8 三态：None=未配置（跟随引擎可用性）；true/false=显式
    "target_lang": "zh",
    "source_langs": [],       # 空=所有非 target 语言
    "max_per_thread": 5,      # 单次打开会话最多翻译条数（控成本，B9 收紧 8→5）
    "max_chars": 400,         # 超长消息跳过
    "style": "chat",
}


def parse_auto_translate_cfg(config_manager) -> Dict[str, Any]:
    """解析 workspace.auto_translate_inbound。``enabled`` 为三态：显式 bool / None（未配置）。

    调用方须经 :func:`resolve_auto_translate_enabled` 把三态收敛成最终 bool，
    不要直接把 ``cfg["enabled"]`` 当 bool 用。
    """
    cfg = dict(_DEFAULT_CFG)
    try:
        root = (getattr(config_manager, "config", None) or {}) if config_manager else {}
        ws = (root.get("workspace") or {}) if isinstance(root, dict) else {}
        raw = ws.get("auto_translate_inbound") or {}
        if isinstance(raw, bool):
            cfg["enabled"] = bool(raw)
            return cfg
        if isinstance(raw, dict):
            if "enabled" in raw:
                cfg["enabled"] = bool(raw.get("enabled"))
            if raw.get("target_lang"):
                cfg["target_lang"] = normalize_lang(str(raw["target_lang"])) or "zh"
            langs = raw.get("source_langs")
            if isinstance(langs, list):
                cfg["source_langs"] = [
                    normalize_lang(str(x)) for x in langs if normalize_lang(str(x))
                ]
            for k in ("max_per_thread", "max_chars"):
                if raw.get(k) is not None:
                    cfg[k] = int(raw[k])
            if raw.get("style"):
                cfg["style"] = str(raw["style"])
    except Exception:
        logger.debug("parse auto_translate_inbound 失败，回落默认（未配置态）", exc_info=True)
    cfg["max_per_thread"] = max(1, min(30, int(cfg.get("max_per_thread") or 5)))
    cfg["max_chars"] = max(50, min(2000, int(cfg.get("max_chars") or 400)))
    return cfg


def resolve_auto_translate_enabled(cfg: Dict[str, Any], engines_available: bool) -> bool:
    """B8：入站自动译总开关三态解析（纯函数）。

    - 显式 true/false → 以配置为准（运营意志优先，包括「有引擎也要关」）；
    - 未配置（None）→ 跟随引擎可用性：有可用引擎默认开；**无引擎必须保持关**。
    """
    enabled = cfg.get("enabled")
    if isinstance(enabled, bool):
        return enabled
    return bool(engines_available)


def _engines_available(translation_svc: Optional[TranslationService]) -> bool:
    """探测服务是否有任一可用翻译引擎。探不到（异常/无 router）按无引擎处理（保守关）。"""
    try:
        router = getattr(translation_svc, "_router", None)
        return bool(router is not None and router.any_available())
    except Exception:
        return False


def _lang_matches(src: str, cfg: Dict[str, Any]) -> bool:
    target = normalize_lang(str(cfg.get("target_lang") or "zh")) or "zh"
    src = normalize_lang(src) or detect_language(src)
    if not src or src == "unknown":
        return True  # 未知语言也尝试译成中文
    if src == target:
        return False
    allow = cfg.get("source_langs") or []
    if allow:
        return src in allow
    return True


def _already_translated(msg: Dict[str, Any], target: str) -> bool:
    text = str(msg.get("text") or msg.get("original_text") or "")
    trans = str(msg.get("translated_text") or "")
    if not trans or trans == text:
        return False
    tr = msg.get("translation") if isinstance(msg.get("translation"), dict) else {}
    if tr.get("ok") and tr.get("target_lang") == target:
        return True
    # store 行：translated_text 已与原文不同即视为已译
    return bool(trans and trans != text)


def _overlay_store_translations(
    messages: List[Dict[str, Any]],
    store_rows: List[Dict[str, Any]],
    target: str,
) -> int:
    """用 store 已有译文覆盖消息（按 message_id 或 text+ts 兜底）。"""
    by_id = {str(r.get("message_id") or ""): r for r in store_rows if r.get("message_id")}
    by_sig: Dict[str, Dict[str, Any]] = {}
    for r in store_rows:
        sig = f"{r.get('text','')}|{r.get('ts',0)}"
        by_sig[sig] = r
    n = 0
    for m in messages:
        if _already_translated(m, target):
            continue
        row = by_id.get(str(m.get("message_id") or "")) or by_sig.get(
            f"{m.get('text','')}|{m.get('ts',0)}"
        )
        if not row:
            continue
        trans = str(row.get("translated_text") or "")
        text = str(m.get("text") or m.get("original_text") or "")
        if trans and trans != text:
            m["translated_text"] = trans
            m["translation"] = {
                "source_lang": row.get("source_lang") or m.get("language") or "unknown",
                "target_lang": row.get("target_lang") or target,
                "ok": True,
                "provider": "store",
                "cached": True,
            }
            n += 1
    return n


async def enrich_inbound_translations(
    request,
    messages: List[Dict[str, Any]],
    *,
    conversation_id: str,
    config_manager=None,
    translation_svc: Optional[TranslationService] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """为入站消息补充译文。返回 (messages, stats)。"""
    cfg = parse_auto_translate_cfg(config_manager)
    if translation_svc is None:
        from src.web.routes.unified_inbox_services import _get_translation_service
        translation_svc = _get_translation_service(request)
    # B8：未显式配置时跟随引擎可用性（有引擎默认开 / 无引擎必须关，防空跑攒 failed）
    enabled = resolve_auto_translate_enabled(cfg, _engines_available(translation_svc))
    stats = {
        "enabled": enabled,
        "target_lang": cfg["target_lang"],
        "from_store": 0,
        "translated": 0,
        "skipped": 0,
        "failed": 0,
    }
    if not enabled or not messages:
        return messages, stats

    target = str(cfg["target_lang"] or "zh")
    store = getattr(request.app.state, "inbox_store", None)
    if store is not None and conversation_id:
        try:
            rows = store.list_messages(conversation_id, limit=200)
            stats["from_store"] = _overlay_store_translations(messages, rows, target)
        except Exception:
            logger.debug("overlay store translations 失败", exc_info=True)

    # 只处理最近的入站、未译、需译消息
    candidates: List[Dict[str, Any]] = []
    for m in reversed(messages):
        if str(m.get("direction") or "in") != "in":
            continue
        text = str(m.get("text") or m.get("original_text") or "").strip()
        if not text or len(text) > cfg["max_chars"]:
            stats["skipped"] += 1
            continue
        lang = str(m.get("language") or detect_language(text))
        if not _lang_matches(lang, cfg):
            stats["skipped"] += 1
            continue
        if _already_translated(m, target):
            continue
        candidates.append(m)
        if len(candidates) >= cfg["max_per_thread"]:
            break

    by_src_lang: Dict[str, int] = {}  # P3：新译出消息的客户来源语言分布（喂跨语言总览）
    for m in candidates:
        text = str(m.get("text") or m.get("original_text") or "")
        src_lang = str(m.get("language") or detect_language(text))
        try:
            result = await translation_svc.translate(
                text,
                target_lang=target,
                source_lang=src_lang,
                style=str(cfg.get("style") or "chat"),
            )
        except Exception:
            stats["failed"] += 1
            continue
        if not result.ok or not result.translated_text:
            stats["failed"] += 1
            continue
        m["translated_text"] = result.translated_text
        m["translation"] = result.to_dict()
        stats["translated"] += 1
        _src = normalize_lang(str(result.source_lang or src_lang)) or "unknown"
        by_src_lang[_src] = by_src_lang.get(_src, 0) + 1
        mid = str(m.get("message_id") or "")
        if store is not None and conversation_id and mid:
            try:
                store.update_message_translation(
                    mid,
                    translated_text=result.translated_text,
                    target_lang=target,
                    source_lang=result.source_lang,
                )
            except Exception:
                logger.debug("persist translation 失败", exc_info=True)

    # P3：把本次「新译出 + 失败」按日累计进入站漏斗（命中缓存的 from_store 不计，避免重开重复）。
    if store is not None and hasattr(store, "record_inbound_xlate") and (
        stats["translated"] or stats["failed"]
    ):
        try:
            store.record_inbound_xlate(
                translated=stats["translated"],
                failed=stats["failed"],
                by_lang=by_src_lang,
            )
        except Exception:
            logger.debug("record_inbound_xlate 失败（已忽略）", exc_info=True)

    return messages, stats
