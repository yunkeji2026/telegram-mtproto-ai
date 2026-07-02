"""实时共情语音通话的配置就绪度（开闸前 preflight，纯函数）。

与 ``embedding_readiness`` 同口径：把「开关开了但 host 没配 / 公网裸奔」等隐性坑显性化，
供能力看板一致性体检 + toggle 护栏 + ops 卡文案共用。
"""

from __future__ import annotations

from typing import Any, Dict


def _dig(config: Any, path: str, default: Any = None) -> Any:
    cur = config
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _rtv_cfg(config: Any) -> Dict[str, Any]:
    if isinstance(config, dict):
        rv = config.get("realtime_voice")
        if isinstance(rv, dict):
            return rv
    return {}


def realtime_voice_host_configured(config: Any) -> bool:
    """语音主机 base_url 是否有效（非空）。"""
    base = str(_rtv_cfg(config).get("base_url") or "").strip()
    return bool(base)


def check_realtime_voice_readiness(config: Any) -> Dict[str, Any]:
    """组合开关 + host 配置 + access_token 给出就绪结论（零网络）。

    返回 ``{enabled, configured, ready, severity, reason, fix, warn_public}``。
    """
    enabled = bool(_dig(config, "realtime_voice.enabled", False))
    configured = realtime_voice_host_configured(config)
    base_url = str(_rtv_cfg(config).get("base_url") or "").strip()
    token = str(_rtv_cfg(config).get("access_token") or "").strip()
    out: Dict[str, Any] = {
        "enabled": enabled,
        "configured": configured,
        "base_url": base_url,
        "has_access_token": bool(token),
        "ready": False,
        "severity": "ok",
        "reason": "",
        "fix": "",
        "warn_public": False,
    }
    if enabled and not configured:
        out["severity"] = "error"
        out["reason"] = ("实时语音已开但 realtime_voice.base_url 为空 → WS 网关无法连主机")
        out["fix"] = "配 realtime_voice.base_url（MiniCPM-o 语音主机），或先关 realtime_voice.enabled"
        return out
    if enabled and configured and not token:
        out["warn_public"] = True
        out["reason"] = "未配 access_token → 试拨/引擎 API 无口令保护（公网暴露前务必设置）"
    if enabled and configured:
        out["ready"] = True
        if not out["reason"]:
            out["reason"] = "主机已配；开前请在 ops 卡载入 GPU 模型并用试拨页验证"
        return out
    if not enabled and configured:
        out["reason"] = "主机已配但 realtime_voice.enabled=false → 功能未开"
        out["fix"] = "能力看板或 config.local.yaml 开启 realtime_voice.enabled"
        return out
    out["reason"] = "实时语音未启用（realtime_voice.enabled=false）"
    return out


__all__ = [
    "realtime_voice_host_configured",
    "check_realtime_voice_readiness",
]
