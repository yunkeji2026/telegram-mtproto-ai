"""陪伴能力「分阶段开启」开闸护栏（纯函数）。

看板的「看→校→**开**」闭环里"开"那一步：把开/关意图喂进来，由本模块**权威**判定
是否放行，路由层只负责写 config overlay + 审计。护栏全在服务端，前端二次确认只是体验。

核心约束（开启方向）：
1. 未知能力 / 无对应开关档 → 拒。
2. 父总开关未开 → 拒（如开 proactive 前必须 companion.enabled）。
3. ⚠ 全自动真发主开关（critical）双重 opt-in：worker 必开 + 至少 1 个 auto_ai 会话，
   否则拒；send-gate 未开不拒但回 warn（裸奔强烈不建议）。
关闭方向一律放行（关真发永远安全；关安全闸危险但由前端二次确认兜底，并回 warn）。
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from .capability_status import CAPABILITIES, _dig
from .delivery_calibration import delivery_calibration

CAP_BY_KEY: Dict[str, Any] = {c.key: c for c in CAPABILITIES}
_VALID_FIELDS = ("enabled", "dry_run")


def resolve_flag_path(cap: Any, field: str) -> str:
    """字段→config 路径：enabled 用 flag_path，dry_run 用 dry_run_path。"""
    return cap.dry_run_path if field == "dry_run" else cap.flag_path


def check_toggle(
    config: Any,
    modes: Optional[Mapping[str, str]],
    key: str,
    field: str = "enabled",
    value: bool = True,
) -> Dict[str, Any]:
    """判定单次开关是否放行（纯函数）。

    返回 ``{allowed, reason, flag_path, warn}``：allowed=False 时 reason 为拒因；
    allowed=True 且 warn=True 时 reason 为风险提示（放行但应让运营知情）。
    """
    cap = CAP_BY_KEY.get(key)
    if cap is None:
        return {"allowed": False, "reason": f"未知能力: {key}", "flag_path": "", "warn": False}
    if field not in _VALID_FIELDS:
        return {"allowed": False, "reason": f"未知字段: {field}", "flag_path": "", "warn": False}
    path = resolve_flag_path(cap, field)
    if not path:
        return {"allowed": False, "reason": "该能力无此开关档", "flag_path": "", "warn": False}
    value = bool(value)

    # 关闭方向：永远放行；关安全防护回 warn 让运营知情
    if not value:
        if cap.kind == "safeguard" and field == "enabled":
            return {"allowed": True, "warn": True, "flag_path": path,
                    "reason": f"关闭安全防护「{cap.label}」会移除护栏，请确认风险"}
        return {"allowed": True, "warn": False, "flag_path": path, "reason": ""}

    # 开启方向：前置校验
    if cap.parent_path and not _dig(config, cap.parent_path, cap.parent_default):
        return {"allowed": False, "warn": False, "flag_path": path,
                "reason": f"请先开父总开关 {cap.parent_path}"}

    if cap.critical and field == "enabled":
        cal = delivery_calibration(config, modes)
        sw = cal["switches"]
        blockers = []
        if not sw["worker"]:
            blockers.append("需先开 inbox.l2_autosend.enabled（worker 不开则草稿无人处置）")
        if cal["automation_modes"]["auto_ai"] <= 0:
            blockers.append("需至少 1 个会话设为「🚀全自动(auto_ai)」，否则不会对任何人真发")
        if blockers:
            return {"allowed": False, "warn": False, "flag_path": path,
                    "reason": "；".join(blockers)}
        if not sw["send_gate"]:
            return {"allowed": True, "warn": True, "flag_path": path,
                    "reason": "真发将开启但出站安全闸 companion_send_gate 未开（内容/频率裸奔），"
                              "强烈建议同时开启"}

    if cap.key == "realtime_voice" and field == "enabled":
        from src.companion.realtime_voice_readiness import realtime_voice_host_configured, _rtv_cfg
        if not realtime_voice_host_configured(config):
            return {"allowed": False, "warn": False, "flag_path": path,
                    "reason": "请先配置 realtime_voice.base_url（MiniCPM-o 语音主机）"}
        if not str(_rtv_cfg(config).get("access_token") or "").strip():
            return {"allowed": True, "warn": True, "flag_path": path,
                    "reason": "未配置 access_token → 试拨/引擎 API 无口令保护（公网暴露前务必设置）"}

    return {"allowed": True, "warn": False, "flag_path": path, "reason": ""}


__all__ = ["CAP_BY_KEY", "resolve_flag_path", "check_toggle"]
