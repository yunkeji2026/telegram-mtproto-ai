"""全自动出站翻译（L2 autosend 投递前把 AI 中文回复译成客户语言）。

补「全自动聊天翻译」闭环的最后一环：此前 autosend worker 把 AI 生成的（中文）草稿
**原样**投递到客户平台——外语客户会直接收到中文。本模块在投递前把草稿文本经统一
``TranslationService``（术语表 + TM + 语检 + 多引擎 failover）译成会话客户语言，并记录
出向译文映射（供 thread 双行展示），译完再交回 worker 真发。

设计：
  - **纯决策函数**（``normalize_target`` / ``should_translate`` / ``parse_outbound_translate_cfg``）
    零副作用、可单测，路由/worker 只做薄适配。
  - ``translate_outbound_text`` 是「译 + 记录 + 降级回落」的可复用闭包体，依赖通过参数注入
    （translation_service / store），单测可塞 fake。
  - **绝不阻塞投递**：任何异常 / 不可译 / 译文与原文相同 → 回落发原文，保证全自动链路不断。
"""

from __future__ import annotations

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)

_DEFAULT_SOURCE = "zh"
# 不可作为翻译目标的「空/未知」语言标记（与 translation_service.normalize_lang 对齐）
_SKIP_TARGETS = {"", "unknown", "und", "auto"}


def parse_outbound_translate_cfg(config: Any) -> Dict[str, Any]:
    """读 config.inbox.l2_autosend.translate → {enabled, source_lang, style}。缺省全关。"""
    tr = (((config or {}).get("inbox", {}) or {}).get("l2_autosend", {}) or {}
          ).get("translate", {}) or {}
    return {
        "enabled": bool(tr.get("enabled", False)),
        "source_lang": str(tr.get("source_lang") or _DEFAULT_SOURCE).strip().lower(),
        "style": str(tr.get("style") or "chat"),
    }


def normalize_target(lang: str) -> str:
    """归一化语言码：zh-CN → zh；空/未知/auto → ""（表示「不可作为目标」）。"""
    low = str(lang or "").strip().lower()
    if low in _SKIP_TARGETS:
        return ""
    return low.split("-")[0]


def should_translate(text: str, target_lang: str, source_lang: str) -> bool:
    """是否需要翻译：有正文 + 目标语言有效 + 目标 != 源。否则跳过（发原文）。"""
    if not str(text or "").strip():
        return False
    tgt = normalize_target(target_lang)
    if not tgt:
        return False
    return tgt != normalize_target(source_lang)


def _conv_language(store: Any, conversation_id: str) -> str:
    """best-effort 取会话客户语言（conversations.language）。失败 → ""。"""
    if store is None or not conversation_id:
        return ""
    try:
        conv = store.get_conversation(conversation_id)
    except Exception:
        logger.debug("[outbound_translate] 读会话语言失败 conv=%s", conversation_id, exc_info=True)
        return ""
    return str((conv or {}).get("language") or "")


def _detect_source(translation_service: Any, text: str) -> str:
    """检测文本真实源语言（归一化）；检测器缺失/异常 → ""（表示未知）。"""
    fn = getattr(translation_service, "detect_language", None)
    if fn is None:
        return ""
    try:
        return normalize_target(fn(text))
    except Exception:
        logger.debug("[outbound_translate] detect_language 异常", exc_info=True)
        return ""


async def translate_outbound_text(
    item: Dict[str, Any],
    *,
    translation_service: Any,
    store: Any = None,
    source_lang: str = _DEFAULT_SOURCE,
    style: str = "chat",
) -> str:
    """把一条待投递文本译成会话客户语言；记录出向译文映射。**自带「已是客户语言则跳过」护栏**。

    item: ``{conversation_id, text, ...}``（AutosendWorker 的 to_deliver 载荷 / deferred 主动触达）。
    返回**应真正发出的文本**：成功译则返回译文，否则一律回落原文（绝不抛、绝不阻塞投递）。

    关键设计——**先检测真实源语言再决定是否翻译**：陪伴回复栈（skill_manager / reactivation）
    多按客户语言直接生成，盲目按 config 源语言（如 zh）翻译会把已是客户语言的文本 garble。
    故：检测文本实际语言，若已等于目标语言 → 跳过；否则用**检测到的源语言**翻译（比 config 假定更准）。
    """
    text = str(item.get("text") or "")
    cid = str(item.get("conversation_id") or "")
    if not text or translation_service is None:
        return text

    target = normalize_target(_conv_language(store, cid))
    if not target:
        return text  # 目标语言未知 → 不翻译（发原文）

    # 检测真实源语言；命中目标语言即「文本已是客户语言」→ 跳过（防 garble，覆盖主动触达已 in-lang 的消息）
    detected = _detect_source(translation_service, text)
    eff_source = detected or normalize_target(source_lang) or source_lang
    if eff_source == target:
        return text

    try:
        res = await translation_service.translate(
            text, target_lang=target, source_lang=eff_source, style=style,
        )
    except Exception:
        logger.warning("[outbound_translate] 翻译调用失败，发原文 conv=%s", cid, exc_info=True)
        return text

    translated = str(getattr(res, "translated_text", "") or "")
    provider = str(getattr(res, "provider", "") or "")
    err = str(getattr(res, "error", "") or "")
    ok = bool(getattr(res, "ok", False))

    # 失败 / 空 / 与原文相同（provider=identity/none 或未真译）→ 回落原文，不记录无意义副行
    if not ok or not translated or translated == text:
        if err:
            logger.debug("[outbound_translate] 译文降级 conv=%s provider=%s err=%s",
                         cid, provider, err)
        return text

    if store is not None and cid:
        try:
            store.record_outbound_translation(
                cid, translated, text,
                source_lang=eff_source, target_lang=target,
                provider=provider, error=err,
            )
        except Exception:
            logger.debug("[outbound_translate] 记录出向译文失败 conv=%s", cid, exc_info=True)
    return translated


__all__ = [
    "parse_outbound_translate_cfg",
    "normalize_target",
    "should_translate",
    "translate_outbound_text",
]
