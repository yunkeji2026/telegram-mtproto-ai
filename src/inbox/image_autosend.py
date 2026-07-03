"""全自动「按需发图」（System Z autosend 的图片出站，与 ``voice_autosend`` 对称）。

统一收件箱 autosend 之前只会发**文本 / 语音**：对方在对话里要照片（"發個照片給我看看"、
"你煮的面拍张照给我看"）时，AI 只会**嘴上答应**却从不真发图——线上实测对方会质问
"你快拍啊，你是不是騙我的"。本模块给 **System Z 全自动 autosend** 补上「按客户请求出图并发出」
的能力，一处生效、全平台共用（经 ``orch.send_media(media_type="image")``）。

分工（复用既有纯逻辑，避免重复造轮子）：
- 意图判定：``companion_selfie.detect_selfie_request``（要人设自拍）/
  ``contextual_image.plan_contextual_image``（要对话里提到的东西的图）。
- 出图：``companion_selfie.SelfieProvider``——``album`` 后端从预制相册挑（人设自拍，零 API）；
  ``openai``/``command`` 后端 text2img/img2img（自拍可用相册基础图锁脸；物体图走 text2img）。
- 落盘：``protocol_bridge.save_outbound_media``（与坐席/语音出站同一出站媒体目录 → /static URL）。

**默认关**（``companion.selfie.enabled=false``）→ 全自动仍纯文本/语音，零行为变更。
任何环节失败/不满足都返回「不发图」让调用方回落文本/语音，绝不卡住全自动主流程。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

KIND_SELFIE = "selfie"
KIND_OBJECT = "object"

# ── 可观测性（进程内累计；与 voice_autosend 同风格，供 autosend-status 暴露）────────
# 只在「已判定该发图」之后计数：sent=真发出图；fallback=出图/投递失败回落文本/语音。
_METRICS: Dict[str, Any] = {
    "sent": 0, "fallback": 0, "last_reason": "", "last_kind": "", "last_ts": 0.0,
}
_METRICS_LOCK = threading.Lock()


def record_image_sent(kind: str = "") -> None:
    with _METRICS_LOCK:
        _METRICS["sent"] = int(_METRICS["sent"]) + 1
        _METRICS["last_kind"] = str(kind or "")
        _METRICS["last_ts"] = time.time()


def record_image_fallback(reason: str) -> None:
    with _METRICS_LOCK:
        _METRICS["fallback"] = int(_METRICS["fallback"]) + 1
        _METRICS["last_reason"] = str(reason or "")
        _METRICS["last_ts"] = time.time()


def metrics_snapshot() -> Dict[str, Any]:
    with _METRICS_LOCK:
        return dict(_METRICS)


def resolve_image_autosend_cfg(config: Dict[str, Any]) -> Dict[str, Any]:
    """取 ``companion.selfie`` 块（与 ``skill_manager._selfie_cfg`` 同口径；缺失返回 {}）。"""
    try:
        sc = ((config or {}).get("companion") or {}).get("selfie")
        return dict(sc) if isinstance(sc, dict) else {}
    except Exception:
        return {}


def plan_autosend_image(
    peer_text: str,
    history: Optional[List[Dict[str, Any]]],
    scfg: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """按客户最近一条入站文本判断该发什么图（纯函数）。None=不发图（回落文本/语音）。

    - 命中 ``detect_selfie_request`` → ``{kind: "selfie"}``（人设自拍，可走相册）。
    - 否则开了 ``contextual_images`` 且命中 ``plan_contextual_image`` →
      ``{kind: "object", subject, prompt}``（对话里提到的东西，需真出图后端）。
    自拍优先于物体图（``detect_selfie_request`` 已把"你煮的…"排除，二者互斥不重叠）。
    """
    if not scfg or not bool(scfg.get("enabled", False)):
        return None
    pt = str(peer_text or "")
    if not pt.strip():
        return None
    try:
        from src.ai.companion_selfie import detect_selfie_request
        if detect_selfie_request(pt):
            return {"kind": KIND_SELFIE}
    except Exception:
        logger.debug("[image_autosend] selfie 意图判定异常", exc_info=True)
    if bool(scfg.get("contextual_images", False)):
        try:
            from src.ai.contextual_image import plan_contextual_image
            plan = plan_contextual_image(pt, history, style=str(scfg.get("style") or ""))
            if plan:
                return {"kind": KIND_OBJECT, "subject": str(plan.get("subject") or ""),
                        "prompt": str(plan.get("prompt") or "")}
        except Exception:
            logger.debug("[image_autosend] 上下文要图判定异常", exc_info=True)
    return None


def _resolve_persona(persona_id: str) -> Any:
    """取出图用 persona（dict 含 name/appearance 等）；拿不到则回 persona_id 字符串/空。"""
    try:
        if persona_id:
            from src.utils.persona_manager import PersonaManager
            p = PersonaManager.get_instance().get_persona_by_id(str(persona_id))
            if isinstance(p, dict):
                return p
    except Exception:
        logger.debug("[image_autosend] persona 解析失败", exc_info=True)
    return str(persona_id or "")


def _album_key_for(persona_id: str) -> str:
    """album 分册键：多人设时用 persona id/name 选 ``album_dir/<key>`` 子目录；缺则空（用根）。"""
    p = _resolve_persona(persona_id)
    if isinstance(p, dict):
        return str(p.get("id") or p.get("persona_id") or p.get("name") or "").strip()
    return str(p or "").strip()


async def stage_image_file(
    config: Dict[str, Any],
    platform: str,
    account_id: str,
    persona_id: str,
    directive: Dict[str, Any],
    *,
    llm_refine: Optional[Callable[[], Awaitable[str]]] = None,
) -> Optional[Tuple[str, str, str]]:
    """按 ``directive`` 出图并落到出站媒体目录，返回 ``(本地路径, /static URL, kind)``；失败/不满足返回 None。

    调用方据此 ``orch.send_media(media_path=local, media_url=url, media_type="image")``。
    - selfie：``album`` 后端挑现成图；``openai``/``command`` 后端 build_selfie_prompt + 相册基础图 img2img。
    - object：仅真出图后端（非 disabled/album）；可选 ``llm_refine`` 把 prompt 提炼得更准。
    """
    scfg = resolve_image_autosend_cfg(config)
    try:
        from src.ai.companion_selfie import build_selfie_prompt, get_selfie_provider
        provider = get_selfie_provider(scfg.get("provider") or {})
    except Exception:
        logger.debug("[image_autosend] provider 构造失败", exc_info=True)
        return None
    backend = str(getattr(provider, "backend", "")).lower()
    if not bool(getattr(provider, "enabled", False)) or backend in ("", "disabled"):
        return None
    kind = str((directive or {}).get("kind") or "")
    album_key = _album_key_for(persona_id)
    # 出图预算护栏（护 API 账单，与 process_message 自拍/上下文共用同一份全局跟踪器）：
    # 仅真出图后端(openai/command)计数——album 挑现成图零成本不计。达上限→回落不发（不烧钱）。
    _tracker = None
    if backend != "album":
        try:
            _cap = int(scfg.get("daily_global_cap", 0) or 0)
        except Exception:
            _cap = 0
        if _cap > 0:
            try:
                from src.utils.selfie_cap import get_selfie_cap_tracker
                _tracker = get_selfie_cap_tracker(_cap)
            except Exception:
                _tracker = None
            if _tracker is not None and _tracker.would_exceed(1):
                logger.info("[image_autosend] daily_global_cap=%d 已达上限，回落不发", _cap)
                record_image_fallback("global_cap")
                return None
    res = None
    try:
        if kind == KIND_SELFIE:
            if backend == "album":
                res = await provider.generate("", album_key=album_key)
            else:
                prompt = build_selfie_prompt(
                    _resolve_persona(persona_id),
                    scene_hint=str(scfg.get("scene_hint") or ""),
                    style=str(scfg.get("style") or ""),
                    default_appearance=str(scfg.get("appearance") or ""),
                )
                base = ""
                try:
                    base = provider.reference_image(album_key)
                except Exception:
                    base = ""
                if _tracker is not None:
                    _tracker.record_sent(1)
                res = await provider.generate(prompt, album_key=album_key, base_image=base)
        elif kind == KIND_OBJECT:
            if backend == "album":
                # 相册无法凭空生成任意物体图 → 回落（不发图）。
                return None
            prompt = str((directive or {}).get("prompt") or "")
            if bool(scfg.get("contextual_images_llm_prompt", False)) and callable(llm_refine):
                try:
                    refined = str(await llm_refine() or "").strip().strip('"').strip()
                    if refined and len(refined) <= 400:
                        prompt = refined
                except Exception:
                    logger.debug("[image_autosend] prompt LLM 精炼跳过", exc_info=True)
            if not prompt.strip():
                return None
            if _tracker is not None:
                _tracker.record_sent(1)
            res = await provider.generate(prompt)  # 物体图走 text2img，不带人设的脸
        else:
            return None
    except Exception:
        logger.debug("[image_autosend] 出图异常", exc_info=True)
        return None
    if not (res is not None and getattr(res, "ok", False) and getattr(res, "image_path", "")):
        return None
    try:
        with open(res.image_path, "rb") as fh:
            data = fh.read()
    except Exception:
        logger.debug("[image_autosend] 读取出图文件失败", exc_info=True)
        return None
    if not data:
        return None
    try:
        from src.integrations.protocol_bridge import save_outbound_media
        local, url, _mt = save_outbound_media(
            platform, account_id, os.path.basename(res.image_path), data)
        return (local, url, kind)
    except Exception:
        logger.debug("[image_autosend] 落出站媒体失败", exc_info=True)
        return None


__all__ = [
    "KIND_SELFIE", "KIND_OBJECT",
    "resolve_image_autosend_cfg", "plan_autosend_image", "stage_image_file",
    "record_image_sent", "record_image_fallback", "metrics_snapshot",
]
