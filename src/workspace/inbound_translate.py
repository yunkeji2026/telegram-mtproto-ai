"""坐席工作台入站消息自动翻译（Phase 5-3）。

策略：仅在坐席打开会话（/thread）时按需翻译，避免全量 ingest 时烧 API。
译文写入 InboxStore.translated_text，跨重启/重开命中缓存。

性能架构（2026-07 根治「/thread 每次 6 秒」）：
- **同步预算**：打开会话时只同步译最新 ``_SYNC_MAX_MSGS`` 条且不超 ``_SYNC_BUDGET_SEC``
  秒（最新消息秒见译文），其余候选交**后台任务**写库——前端 5s 自适应轮询下一拍经
  store overlay 自然取回，/thread 响应不再被整批翻译拖住。
- **已处理标记**：引擎产出==原文（emoji/人名/不可译短语）也回写 store
  （translated_text=原文 + target_lang=目标语）；下次按「行带目标语标记」直接跳过。
  此前这类消息被判「未译」而**每次打开都重译**（无限循环烧 GPU/API + 拖满超时）。
- **失败负缓存**：翻译异常/引擎全败的消息 10 分钟内不重试（防引擎宕机时被轮询打满）。
- **写库主键修正**：live 聚合路径的消息 message_id 是裸平台 id，与 store 主键
  （``cid:pid``/``cid:h:hash``）不匹配 → 译文写库静默 no-op、下次重译。现经 overlay
  查行时把真实 store 主键随消息携带（``_store_mid``），首开会话的译文也能持久化。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from src.ai.translation_service import TranslationService, detect_language, normalize_lang

logger = logging.getLogger(__name__)

_DEFAULT_CFG: Dict[str, Any] = {
    "enabled": False,
    "target_lang": "zh",
    "source_langs": [],       # 空=所有非 target 语言
    "max_per_thread": 8,      # 单次打开会话最多翻译条数（控成本）
    "max_chars": 400,         # 超长消息跳过
    "style": "chat",
}

# 同步预算：/thread 响应路径最多同步译几条 / 多久，其余转后台（见模块 docstring）。
_SYNC_MAX_MSGS = 2
_SYNC_BUDGET_SEC = 2.5
# 单条超时：同步侧 5s（引擎挂死时确保在外层 6s 兜底前返回并**留下负缓存**——否则被
# 外层 cancel 什么都不留，下次打开重蹈覆辙）；后台侧 30s（不拖响应，给慢引擎全额机会，
# 但防挂死引擎把会话级 in-flight 锁永久占住）。
_SYNC_PER_MSG_TIMEOUT = 5.0
_BG_PER_MSG_TIMEOUT = 30.0

# 失败负缓存：{message_id: 失败时刻}。TTL 内不重试，防「引擎宕机 × 5s 轮询」反复打满超时。
# bounded：超上限按最旧丢 1/5（摊薄逐出成本）。进程级（重启清零=重试，可接受）。
_FAILED_AT: Dict[str, float] = {}
_FAILED_TTL_SEC = 600.0
_FAILED_MAX = 5000

# 后台补译 in-flight：会话级锁（同会话只挂一个后台任务）+ 消息级锁（后台在译的消息
# 不被下一拍轮询的同步预算重复翻译）。任务收尾 finally 清理。
_BG_CONVS: set = set()
_INFLIGHT_MIDS: set = set()


def parse_auto_translate_cfg(config_manager) -> Dict[str, Any]:
    cfg = dict(_DEFAULT_CFG)
    try:
        root = (getattr(config_manager, "config", None) or {}) if config_manager else {}
        ws = (root.get("workspace") or {}) if isinstance(root, dict) else {}
        raw = ws.get("auto_translate_inbound") or {}
        if isinstance(raw, bool):
            cfg["enabled"] = bool(raw)
            return cfg
        if isinstance(raw, dict):
            cfg["enabled"] = bool(raw.get("enabled", cfg["enabled"]))
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
        logger.debug("parse auto_translate_inbound 失败，用默认关", exc_info=True)
    cfg["max_per_thread"] = max(1, min(30, int(cfg.get("max_per_thread") or 8)))
    cfg["max_chars"] = max(50, min(2000, int(cfg.get("max_chars") or 400)))
    return cfg


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


def _failed_recently(mid: str, now: Optional[float] = None) -> bool:
    if not mid:
        return False
    ts = _FAILED_AT.get(mid)
    if ts is None:
        return False
    now = time.monotonic() if now is None else now
    if (now - ts) > _FAILED_TTL_SEC:
        _FAILED_AT.pop(mid, None)
        return False
    return True


def _mark_failed(mid: str) -> None:
    if not mid:
        return
    if len(_FAILED_AT) >= _FAILED_MAX and mid not in _FAILED_AT:
        for k in sorted(_FAILED_AT, key=_FAILED_AT.get)[: max(1, _FAILED_MAX // 5)]:
            _FAILED_AT.pop(k, None)
    _FAILED_AT[mid] = time.monotonic()


def _overlay_store_translations(
    messages: List[Dict[str, Any]],
    store_rows: List[Dict[str, Any]],
    target: str,
) -> int:
    """用 store 已有译文覆盖消息（按 message_id 或 text+ts 兜底）。

    顺带在消息上做两件内部标注（返回前端前会被剥除）：
    - ``_store_mid``：该消息在 store 里的真实主键——live 聚合路径的 message_id 是
      裸平台 id，直接拿去 update_message_translation 会打不中行（译文白译不持久）。
    - ``_xlate_attempted``：store 行已带目标语标记（translated_text 非空 +
      target_lang==目标语），含「产出==原文」的 no-op 情形 → 候选筛选跳过，不再重译。
    """
    def _sig(text: Any, ts: Any) -> str:
        # ts 归一成 float：store 行是 REAL(100.0)、live 消息常是 int(100)，
        # 直接拼串会 "100.0"!="100" 永不命中
        try:
            fts = float(ts or 0)
        except (TypeError, ValueError):
            fts = 0.0
        return f"{text}|{fts}"

    by_id = {str(r.get("message_id") or ""): r for r in store_rows if r.get("message_id")}
    by_sig: Dict[str, Dict[str, Any]] = {}
    for r in store_rows:
        by_sig[_sig(r.get("text", ""), r.get("ts", 0))] = r
    n = 0
    for m in messages:
        row = by_id.get(str(m.get("message_id") or "")) or by_sig.get(
            _sig(m.get("text", ""), m.get("ts", 0))
        )
        if not row:
            continue
        row_mid = str(row.get("message_id") or "")
        if row_mid:
            m["_store_mid"] = row_mid
        text = str(m.get("text") or m.get("original_text") or "")
        trans = str(row.get("translated_text") or "")
        if trans and str(row.get("target_lang") or "") == target:
            m["_xlate_attempted"] = True
        if _already_translated(m, target):
            continue
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


def _persist_mid(conversation_id: str, m: Dict[str, Any]) -> str:
    """取写库用消息主键：overlay 标注的 store 真主键优先，回落 message_id。"""
    mid = str(m.get("_store_mid") or m.get("message_id") or "")
    # store 主键恒以 "cid:" 为前缀；非此形态（live 裸 id）也照传——最差与旧行为等价（no-op）
    return mid


async def _translate_one(
    store,
    translation_svc: TranslationService,
    conversation_id: str,
    m: Dict[str, Any],
    *,
    mid: str,
    target: str,
    style: str,
    timeout: float = _SYNC_PER_MSG_TIMEOUT,
) -> Tuple[str, Optional[Any]]:
    """译一条消息并回写库（``mid``＝store 真实主键，由调用方显式传入——后台任务
    运行时消息 dict 上的内部标注可能已被主流程剥除，不能再从 dict 反查）。

    返回 (outcome, result)，outcome ∈ ok/noop/fail：
    - ok：产出有效译文（≠原文），挂到消息 + 写库；
    - noop：引擎处理成功但产出==原文（emoji/人名/不可译）——写「已处理」标记
      （translated_text=原文 + target_lang），下次不再重译；
    - fail：异常/超时/引擎全败/空产出——记失败负缓存，TTL 内不重试。
    """
    text = str(m.get("text") or m.get("original_text") or "")
    src_lang = str(m.get("language") or detect_language(text))
    try:
        result = await asyncio.wait_for(
            translation_svc.translate(
                text, target_lang=target, source_lang=src_lang, style=style,
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        # 单条超时自己兜住并记负缓存——若等外层 /thread 6s 硬超时来掐，整个 enrich 被
        # cancel、什么都不留，下次打开同一条又卡满（修复前的死循环模式）。
        _mark_failed(mid)
        return "fail", None
    except Exception:
        _mark_failed(mid)
        return "fail", None
    if not result.ok or not result.translated_text:
        _mark_failed(mid)
        return "fail", None
    _FAILED_AT.pop(mid, None)
    if result.translated_text == text:
        if store is not None and conversation_id and mid:
            try:
                store.update_message_translation(
                    mid, translated_text=text, target_lang=target,
                    source_lang=result.source_lang,
                )
            except Exception:
                logger.debug("persist noop 标记失败", exc_info=True)
        return "noop", result
    m["translated_text"] = result.translated_text
    m["translation"] = result.to_dict()
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
    return "ok", result


def _record_funnel(store, translated: int, failed: int, by_lang: Dict[str, int],
                   *, noop: int = 0, deferred: int = 0) -> None:
    """把「新译出/失败/打标/转后台」按日累计进入站漏斗（best-effort）。

    旧版 store（无 noop/deferred 形参）→ TypeError 回落旧三参调用，计数不丢主干。
    """
    if store is None or not hasattr(store, "record_inbound_xlate"):
        return
    if not (translated or failed or noop or deferred):
        return
    try:
        store.record_inbound_xlate(
            translated=translated, failed=failed, by_lang=by_lang,
            noop=noop, deferred=deferred)
    except TypeError:
        try:
            store.record_inbound_xlate(
                translated=translated, failed=failed, by_lang=by_lang)
        except Exception:
            logger.debug("record_inbound_xlate 失败（已忽略）", exc_info=True)
    except Exception:
        logger.debug("record_inbound_xlate 失败（已忽略）", exc_info=True)


def _stats():
    """进程级观测单例（best-effort，取不到返回 None，绝不影响主流程）。"""
    try:
        from src.ai.inbound_translation_stats import get_inbound_translation_stats
        return get_inbound_translation_stats()
    except Exception:
        return None


def runtime_snapshot() -> Dict[str, Any]:
    """瞬时运行态（供 /api/workspace/metrics 注入 dump(runtime=...)）。

    - ``bg_convs``：当前有后台补译任务在跑的会话数（积压信号）；
    - ``inflight_mids``：后台在译消息数；
    - ``failed_cached``：失败负缓存当前条目数（引擎不健康的规模信号）。
    """
    return {
        "bg_convs": len(_BG_CONVS),
        "inflight_mids": len(_INFLIGHT_MIDS),
        "failed_cached": len(_FAILED_AT),
    }


def _spawn_bg_translate(
    store,
    translation_svc: TranslationService,
    conversation_id: str,
    pending: List[Dict[str, Any]],
    *,
    target: str,
    style: str,
) -> bool:
    """把剩余候选交给后台任务翻译落库（/thread 响应不等它）。

    并发防重：会话级 in-flight 锁（轮询高频调用时同会话只挂一个任务）+ 消息级
    in-flight 集（后台在译的消息不被下一拍同步预算重复翻译）。无运行中事件循环
    （纯同步测试上下文）→ 返回 False 放弃，行为无害。
    """
    if not pending or conversation_id in _BG_CONVS:
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    _BG_CONVS.add(conversation_id)
    mids = [_persist_mid(conversation_id, m) for m in pending]
    _INFLIGHT_MIDS.update(mid for mid in mids if mid)

    async def _run() -> None:
        n_ok = n_fail = n_noop = 0
        by_lang: Dict[str, int] = {}
        st = _stats()
        try:
            for m, mid in zip(pending, mids):
                try:
                    outcome, result = await _translate_one(
                        store, translation_svc, conversation_id, m,
                        mid=mid, target=target, style=style,
                        timeout=_BG_PER_MSG_TIMEOUT,
                    )
                except Exception:
                    outcome, result = "fail", None
                if mid:
                    _INFLIGHT_MIDS.discard(mid)
                if st:
                    st.record_bg(outcome)
                if outcome == "ok":
                    n_ok += 1
                    src = normalize_lang(str(getattr(result, "source_lang", "") or "")) or "unknown"
                    by_lang[src] = by_lang.get(src, 0) + 1
                elif outcome == "noop":
                    n_noop += 1
                elif outcome == "fail":
                    n_fail += 1
            _record_funnel(store, n_ok, n_fail, by_lang, noop=n_noop)
        except Exception:
            logger.debug("后台补译任务异常（已忽略）", exc_info=True)
        finally:
            _BG_CONVS.discard(conversation_id)
            for mid in mids:
                _INFLIGHT_MIDS.discard(mid)

    loop.create_task(_run())
    return True


async def enrich_inbound_translations(
    request,
    messages: List[Dict[str, Any]],
    *,
    conversation_id: str,
    config_manager=None,
    translation_svc: Optional[TranslationService] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """为入站消息补充译文。返回 (messages, stats)。

    stats：``from_store``（overlay 命中）/``translated``（本次同步新译）/``noop``
    （产出==原文，已打标）/``failed``/``skipped``/``deferred``（交后台补译条数——
    前端轮询下一拍经 store overlay 取回）。
    """
    cfg = parse_auto_translate_cfg(config_manager)
    stats = {
        "enabled": cfg["enabled"],
        "target_lang": cfg["target_lang"],
        "from_store": 0,
        "translated": 0,
        "noop": 0,
        "skipped": 0,
        "failed": 0,
        "deferred": 0,
    }
    if not cfg["enabled"] or not messages:
        return messages, stats

    target = str(cfg["target_lang"] or "zh")
    store = getattr(request.app.state, "inbox_store", None)
    if store is not None and conversation_id:
        try:
            rows = store.list_messages(conversation_id, limit=200)
            stats["from_store"] = _overlay_store_translations(messages, rows, target)
        except Exception:
            logger.debug("overlay store translations 失败", exc_info=True)

    if translation_svc is None:
        from src.web.routes.unified_inbox_services import _get_translation_service
        translation_svc = _get_translation_service(request)

    # 只处理最近的入站、未译、需译消息
    candidates: List[Dict[str, Any]] = []
    for m in reversed(messages):
        if str(m.get("direction") or "in") != "in":
            continue
        text = str(m.get("text") or m.get("original_text") or "").strip()
        if not text or len(text) > cfg["max_chars"]:
            stats["skipped"] += 1
            continue
        if m.get("_xlate_attempted"):
            stats["skipped"] += 1          # 引擎已处理过（含产出==原文的 no-op）
            continue
        mid = _persist_mid(conversation_id, m)
        if _failed_recently(mid) or (mid and mid in _INFLIGHT_MIDS):
            stats["skipped"] += 1          # 失败冷却中 / 后台正在译
            _st_cd = _stats()
            if _st_cd:
                _st_cd.record_skipped_cooldown()
            continue
        # 语言标签 'unknown'（protocol push 落库时未带 language）不可信——必须按正文重检，
        # 否则中文消息会被送去「译成中文」，LLM 对同语输入常自由发挥出闲聊句污染译文行。
        lang = str(m.get("language") or "").strip()
        if not lang or lang == "unknown":
            lang = detect_language(text)
        if not _lang_matches(lang, cfg):
            stats["skipped"] += 1
            continue
        if _already_translated(m, target):
            continue
        candidates.append(m)
        if len(candidates) >= cfg["max_per_thread"]:
            break

    # 同步预算内逐条译（最新消息打开即见译文），其余交后台任务
    st = _stats()
    by_src_lang: Dict[str, int] = {}  # P3：新译出消息的客户来源语言分布（喂跨语言总览）
    deferred: List[Dict[str, Any]] = []
    t0 = time.monotonic()
    for i, m in enumerate(candidates):
        if i >= _SYNC_MAX_MSGS or (time.monotonic() - t0) > _SYNC_BUDGET_SEC:
            deferred = candidates[i:]
            break
        outcome, result = await _translate_one(
            store, translation_svc, conversation_id, m,
            mid=_persist_mid(conversation_id, m), target=target,
            style=str(cfg.get("style") or "chat"),
        )
        if st:
            st.record_sync(outcome)
        if outcome == "ok":
            stats["translated"] += 1
            src = normalize_lang(str(getattr(result, "source_lang", "") or "")) or "unknown"
            by_src_lang[src] = by_src_lang.get(src, 0) + 1
        elif outcome == "noop":
            stats["noop"] += 1
        else:
            stats["failed"] += 1

    if deferred and store is not None and conversation_id:
        if _spawn_bg_translate(
            store, translation_svc, conversation_id, deferred,
            target=target, style=str(cfg.get("style") or "chat"),
        ):
            stats["deferred"] = len(deferred)
            if st:
                st.record_deferred(len(deferred))

    # P3：把本次「新译出 + 失败 + 打标 + 转后台」按日累计进入站漏斗
    # （命中缓存的 from_store 不计，避免重开重复；后台侧完成量由后台任务自行落账）。
    _record_funnel(store, stats["translated"], stats["failed"], by_src_lang,
                   noop=stats["noop"], deferred=stats["deferred"])

    # 剥除内部标注，不泄漏进 API 响应（后台任务已单独持有 mids，不受影响）
    for m in messages:
        m.pop("_store_mid", None)
        m.pop("_xlate_attempted", None)

    return messages, stats
