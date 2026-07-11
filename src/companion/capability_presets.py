"""陪伴能力「一键预设档」+ 快照/回滚的纯计划层。

把"沿风险阶梯整档切换"算成确定性意图列表（intentions），由路由层逐条过同一套护栏
（``capability_toggle.check_toggle``）后落 overlay。本模块零副作用、可单测。

四档预设（对应爬到阶梯第几层）：
  - ``safe_default``  安全默认：仅 tier0 安全栈开，其余全关（含真发/语音/主动触达）= 急停档。
  - ``dry_run_trial`` 灰度试运行：安全栈+翻译+观测+worker 开，主动触达走 dry_run，**不真发**。
  - ``nurture_mode``  养号模式：dry_run_trial 底座 + send-gate 预热爬坡 + 金丝雀白名单预先武装
    （``extras`` 落 ``ops.canary.enabled=true`` / ``mode=manual``；之后开真发时只有
    ``ops.canary.pinned_accounts`` 白名单账号真发，其余 hold——放量爆炸半径受控）。
  - ``full_auto``     全量真发：全部开（deliver 仍受 auto_ai 双重 opt-in 护栏约束）。

``extras``：能力注册表之外的原生 config 路径（如金丝雀开关），由路由层直接写 overlay。
只允许来自本注册表的白名单路径（非用户任意输入），不过 ``check_toggle`` 护栏。

回滚：apply 预设前先 ``capture_snapshot`` 当前各档值 + ``capture_extra_flags`` 抓 extras 路径
现值，rollback 时 ``snapshot_to_plan`` 还原（同样逐条过护栏，条件变了的项会被如实拦下并报告），
extras 路径按快照原值直写还原。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .capability_status import CAPABILITIES, _dig

CAP_BY_KEY = {c.key: c for c in CAPABILITIES}

# 每档每能力的目标态：off | on | dry_run（dry_run 仅对支持灰度的能力，其余按 on 处理）
PRESETS: Dict[str, Dict[str, Any]] = {
    "safe_default": {
        "label": "安全默认（急停：关全部外发，仅留安全栈）",
        "states": {
            "persona_guard": "on", "empathy_strategy": "on", "wellbeing": "on",
            "companion_send_gate": "on",
            "auto_translate_inbound": "off", "quality_trend": "off",
            "l2_autosend_worker": "off", "l2_autosend_deliver": "off",
            "proactive_topic": "off", "proactive_care": "off",
            "multiplatform_deferred": "off", "voice_autosend": "off",
            "realtime_voice": "off",
        },
    },
    "dry_run_trial": {
        "label": "灰度试运行（观测全开，主动触达只计划不真发）",
        "states": {
            "persona_guard": "on", "empathy_strategy": "on", "wellbeing": "on",
            "companion_send_gate": "on",
            "auto_translate_inbound": "on", "quality_trend": "on",
            "l2_autosend_worker": "on", "l2_autosend_deliver": "off",
            "proactive_topic": "dry_run", "proactive_care": "dry_run",
            "multiplatform_deferred": "on", "voice_autosend": "off",
            "realtime_voice": "off",
        },
    },
    "nurture_mode": {
        "label": "养号模式（send-gate 预热爬坡 + 金丝雀白名单，主动触达 dry_run 不真发）",
        "states": {
            # dry_run_trial 底座：观测/翻译/worker 开，deliver 关，主动触达只演练
            "persona_guard": "on", "empathy_strategy": "on", "wellbeing": "on",
            "companion_send_gate": "on",   # 预热爬坡：warmup_start_cap→target_cap + 红灯拒发
            "auto_translate_inbound": "on", "quality_trend": "on",
            "l2_autosend_worker": "on", "l2_autosend_deliver": "off",
            "proactive_topic": "dry_run", "proactive_care": "dry_run",
            "multiplatform_deferred": "on", "voice_autosend": "off",
            "realtime_voice": "off",
        },
        # 金丝雀预先武装（manual=白名单模式）：现在不影响任何发送（deliver 本就关），
        # 未来开真发时只放行 ops.canary.pinned_accounts，其余账号 hold。
        "extras": [
            {"path": "ops.canary.enabled", "value": True},
            {"path": "ops.canary.mode", "value": "manual"},
        ],
    },
    "full_auto": {
        "label": "全量真发（全部开；真发仍受 auto_ai 双重 opt-in 护栏）",
        "states": {
            "persona_guard": "on", "empathy_strategy": "on", "wellbeing": "on",
            "companion_send_gate": "on",
            "auto_translate_inbound": "on", "quality_trend": "on",
            "l2_autosend_worker": "on", "l2_autosend_deliver": "on",
            "proactive_topic": "on", "proactive_care": "on",
            "multiplatform_deferred": "on", "voice_autosend": "on",
            "realtime_voice": "off",
        },
    },
}


def _priority(cap: Any, field: str, value: bool) -> float:
    """计划执行序：关（永远安全）最先；开则安全栈→低风险→高风险，真发主开关压到最后。"""
    if not value:
        return -10.0
    if cap.kind == "safeguard":
        return 0.0
    if cap.critical:               # 全自动真发主开关：在 send_gate/worker 之后才开
        return 50.0
    base = float(cap.tier)
    if field == "dry_run":
        base += 0.5
    return 10.0 + base


def _intentions_for(cap: Any, state: str) -> List[Dict[str, Any]]:
    """单能力目标态 → 意图列表（enabled / 可选 dry_run）。"""
    out: List[Dict[str, Any]] = []
    if state == "off":
        out.append({"key": cap.key, "field": "enabled", "value": False})
        if cap.dry_run_path:
            out.append({"key": cap.key, "field": "dry_run", "value": False})
    elif state == "dry_run" and cap.dry_run_path:
        out.append({"key": cap.key, "field": "enabled", "value": True})
        out.append({"key": cap.key, "field": "dry_run", "value": True})
    else:  # on（或对不支持 dry_run 的能力把 dry_run 当 on）
        out.append({"key": cap.key, "field": "enabled", "value": True})
        if cap.dry_run_path:
            out.append({"key": cap.key, "field": "dry_run", "value": False})
    return out


def _order(intentions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(
        intentions,
        key=lambda it: _priority(CAP_BY_KEY[it["key"]], it["field"], it["value"]),
    )


def build_preset_plan(name: str) -> Optional[List[Dict[str, Any]]]:
    """预设名 → 有序意图列表；未知预设返回 None。"""
    spec = PRESETS.get(name)
    if not spec:
        return None
    plan: List[Dict[str, Any]] = []
    for cap in CAPABILITIES:
        state = spec["states"].get(cap.key)
        if state is None:
            continue
        plan.extend(_intentions_for(cap, state))
    return _order(plan)


def preset_extras(name: str) -> List[Dict[str, Any]]:
    """预设的注册表外原生 config 意图（白名单，来自 PRESETS 声明，非用户输入）。"""
    spec = PRESETS.get(name) or {}
    return [dict(e) for e in (spec.get("extras") or [])]


# extras 路径的「缺省语义值」：快照抓取时 config 未显式配置也能落一个确定值，
# 保证 rollback 直写还原是确定性的（写缺省值 == 行为不变）。
EXTRA_FLAG_DEFAULTS: Dict[str, Any] = {
    "ops.canary.enabled": False,
    "ops.canary.mode": "manual",
}


def capture_extra_flags(config: Any) -> Dict[str, Any]:
    """抓取所有预设 extras 路径的当前值（缺省按 EXTRA_FLAG_DEFAULTS），供回滚还原。"""
    return {path: _dig(config, path, default)
            for path, default in EXTRA_FLAG_DEFAULTS.items()}


def capture_snapshot(config: Any) -> Dict[str, Dict[str, bool]]:
    """抓取当前所有能力的 enabled/dry_run 值，供回滚还原。"""
    snap: Dict[str, Dict[str, bool]] = {}
    for cap in CAPABILITIES:
        entry: Dict[str, bool] = {"enabled": bool(_dig(config, cap.flag_path, False))}
        if cap.dry_run_path:
            entry["dry_run"] = bool(_dig(config, cap.dry_run_path, False))
        snap[cap.key] = entry
    return snap


def snapshot_to_plan(snapshot: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """快照 → 有序还原意图列表（忽略未知能力）。"""
    plan: List[Dict[str, Any]] = []
    for key, vals in (snapshot or {}).items():
        cap = CAP_BY_KEY.get(key)
        if cap is None or not isinstance(vals, dict):
            continue
        plan.append({"key": key, "field": "enabled", "value": bool(vals.get("enabled"))})
        if cap.dry_run_path and "dry_run" in vals:
            plan.append({"key": key, "field": "dry_run", "value": bool(vals.get("dry_run"))})
    return _order(plan)


__all__ = [
    "PRESETS", "EXTRA_FLAG_DEFAULTS", "build_preset_plan", "preset_extras",
    "capture_extra_flags", "capture_snapshot", "snapshot_to_plan",
]
