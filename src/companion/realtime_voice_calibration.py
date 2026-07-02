"""实时语音开闸前校准（纯函数）。

服务于 capability 看板「去校准」与 ops 卡试拨引导：config + 参考音 + 功能链 + 引擎状态
→ 单一 verdict + 可执行建议。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from .realtime_voice_readiness import check_realtime_voice_readiness


def _chain_flags(config: Any) -> Dict[str, bool]:
    try:
        from src.ai.realtime_voice import RealtimeVoiceConfig
        rvc = RealtimeVoiceConfig.from_config(config)
        return {
            "opener_enabled": bool(rvc.opener_enabled),
            "subtitle_enabled": bool(rvc.subtitle_enabled),
        }
    except Exception:
        return {"opener_enabled": True, "subtitle_enabled": True}


def realtime_voice_calibration(
    config: Any,
    *,
    ref_summary: Optional[Dict[str, Any]] = None,
    engine_loaded: Optional[bool] = None,
    memory_store: Optional[bool] = None,
) -> Dict[str, Any]:
    """开闸/试拨前校准（纯函数，零网络）。"""
    cfg_chk = check_realtime_voice_readiness(config)
    refs = ref_summary or {}
    chain = _chain_flags(config)
    if memory_store is not None:
        chain["memory_store"] = bool(memory_store)
    warnings: list[str] = []
    enabled = bool(cfg_chk.get("enabled"))
    configured = bool(cfg_chk.get("configured"))

    if not enabled:
        verdict = "inactive"
        recommendation = "功能未开；配好 base_url 后在能力看板开启 realtime_voice"
    elif not configured:
        verdict = "misconfigured"
        recommendation = cfg_chk.get("fix") or "请先配置 realtime_voice.base_url"
        warnings.append(cfg_chk.get("reason") or "主机未配")
    elif engine_loaded is False:
        verdict = "warming"
        recommendation = "主机可达但 GPU 模型未载入 → ops 卡点「启动引擎」后再试拨"
    elif int(refs.get("with_reference") or 0) == 0 and int(refs.get("persona_count") or 0) > 0:
        verdict = "trial_builtin"
        recommendation = ("引擎可试拨（内置音色）；上传参考音后可验证克隆真声 + 开场白链路")
        warnings.append("无人设参考音 → 不走克隆，仅内置音色")
    elif str(refs.get("worst_grade") or "") == "red":
        verdict = "ref_poor"
        iss = (refs.get("sample_issues") or ["参考音质量不佳"])[0]
        recommendation = f"参考音需重录（{iss}）后再做克隆试拨"
        warnings.append("参考音体检红灯")
    else:
        verdict = "ready"
        recommendation = "引擎与参考音就绪 → 去试拨页验证 opener / 字幕 / 记忆"

    if enabled and cfg_chk.get("warn_public"):
        warnings.append("未配 access_token（公网暴露前务必设置）")
    if enabled and not chain.get("opener_enabled"):
        warnings.append("主动开场白已关 → 接通后可能沉默，可在 config 开 realtime_voice.opener")
    if enabled and not chain.get("subtitle_enabled"):
        warnings.append("双语字幕已关 → 运营侧看不到译文叠显")

    return {
        "config": cfg_chk,
        "refs": refs,
        "chain": chain,
        "engine_loaded": engine_loaded,
        "verdict": verdict,
        "recommendation": recommendation,
        "warnings": warnings,
        "trial_url": "/ops/voice-call",
    }


__all__ = ["realtime_voice_calibration"]
