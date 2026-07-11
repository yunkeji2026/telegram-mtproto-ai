"""陪伴能力就绪度聚合（纯函数，单一事实源）。

背景：代码核验显示陪伴栈大半「已建但默认 flag-off」。要安全「分阶段开启」，先得有一张
**看板**回答三件事：① 每个能力开没开 ② 开了能不能真生效（子系统挂没挂、是否还在 dry_run）
③ 开启它有多大行为风险（纯 prompt 提示 vs 真往客户发消息）。本模块把这张表算出来。

设计：
  - 纯函数 ``collect_capability_status(config, runtime=...)``——吃 config dict + 可选运行时信号，
    零副作用、可单测；路由层只做 config/state 取值这层薄适配。
  - **风险分档即「分阶段开启阶梯」**：tier0 提示层(零风险) → tier1 坐席/翻译 → tier2 全自动文本
    → tier3 主动触达 → tier4 全自动语音。从低风险往高风险逐档点亮。
  - 区分 ``feature``（开启=加行为/加风险）与 ``safeguard``（开启=加安全，关着才危险）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


def _dig(config: Any, path: str, default: Any = None) -> Any:
    """按点路径取嵌套 config 值；任一层缺失/非 dict 返回 default。"""
    cur = config
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


@dataclass(frozen=True)
class Capability:
    key: str
    label: str
    tier: int                       # 0..4，行为风险递增 = 分阶段开启顺序
    risk: str                       # none | low | high
    kind: str                       # feature | safeguard
    flag_path: str                  # 该能力的开关 config 路径
    desc: str = ""
    parent_path: str = ""           # 父总开关（关则全档失效），如 companion.enabled
    parent_default: bool = True     # 父开关缺省值
    dry_run_path: str = ""          # 灰度开关路径（开着=只计划不真发）
    runtime_dep: str = ""           # 依赖的运行时子系统键（app.state 是否挂载）
    critical: bool = False          # 是否「全自动真发」主开关（看板高亮）
    calibration: str = ""           # 开闸前校准端点（"去校准"深链）；空=无专属校准
    unset_follows_runtime: bool = False  # 三态：flag 未配置时跟随 runtime_dep 可用性
                                          # （对齐 P0-3 B8「有引擎即默认开」，防看板与运行时相悖）


# ── 能力注册表：即「唤醒陪伴栈」的分阶段开启阶梯 ────────────────────────────
CAPABILITIES: List[Capability] = [
    # Tier 0 —— 提示层安全栈（纯 prompt 注入，零行为风险；safeguard，关着才危险）
    Capability(
        "persona_guard", "人设一致性守卫", 0, "none", "safeguard",
        "companion.persona_guard.enabled", parent_path="companion.enabled",
        desc="回复后剥离 AI 自曝身份/禁用语，防人格崩。零行为风险。"),
    Capability(
        "empathy_strategy", "共情策略选择器", 0, "none", "safeguard",
        "companion.empathy_strategy.enabled", parent_path="companion.enabled",
        desc="情绪→回复间注入一行共情行动指令。纯提示。"),
    Capability(
        "wellbeing", "安全底线守卫(危机/反谄媚)", 0, "none", "safeguard",
        "companion.wellbeing.enabled", parent_path="companion.enabled",
        desc="危机识别+安全优先指令+反谄媚护栏。漏接危机是最坏结果。"),

    # Tier 1 —— 坐席可见 / 翻译 / 观测（低风险，读侧或运营侧）
    Capability(
        "auto_translate_inbound", "入站自动翻译", 1, "low", "feature",
        "workspace.auto_translate_inbound.enabled",
        runtime_dep="translation_service", unset_follows_runtime=True,
        desc="坐席打开会话时把外语客户消息译为中文展示。仅 API 成本，无对客行为。"
             "P0-3：未显式配置时跟随翻译引擎可用性自动开（有引擎即开）。"),
    Capability(
        "quality_trend", "质量趋势持久化", 1, "low", "feature",
        "companion.quality_trend.enabled", parent_path="companion.enabled",
        runtime_dep="quality_trend_store",
        desc="周期快照 care/reactivation 质量为时序，画趋势线。仅落库。"),
    Capability(
        "memory_vector_recall", "记忆向量召回", 1, "low", "feature",
        "memory.vector.enabled",
        desc="情节记忆按语义向量+关键词融合召回（vs 纯关键词），提升改写/近义查询命中。"
             "需 ai_client embed；失败自动回落关键词（零阻断）。开前先用记忆召回 eval 量化收益。"),

    # Tier 2 —— 全自动文本回复（高风险：真往客户发消息，烧号面在此）
    Capability(
        "l2_autosend_worker", "L2 自动发送 worker", 2, "low", "feature",
        "inbox.l2_autosend.enabled", runtime_dep="autosend_worker",
        desc="处置 L2 草稿的 worker。开着仅标记/审计，是否真发由下面 deliver 决定。"),
    Capability(
        "l2_autosend_deliver", "⚠ 全自动真发到客户", 2, "high", "feature",
        "inbox.l2_autosend.deliver", critical=True, runtime_dep="autosend_worker",
        calibration="/api/companion/capabilities/delivery-calibration",
        desc="主开关：true 才真把 L2 草稿发到客户平台。双重 opt-in（还需会话档=全自动）。"),
    Capability(
        "companion_send_gate", "出站安全闸(send-gate)", 2, "none", "safeguard",
        "companion_send_gate.enabled",
        desc="全自动出站前的内容/频率闸。safeguard：关着=裸奔，建议随真发一起开。"),
    Capability(
        "outbound_autosend_translate", "出站自动翻译", 2, "low", "feature",
        "inbox.l2_autosend.translate.enabled", runtime_dep="translation_service",
        desc="全自动真发 + 主动触达(care/reactivation) 投递前把消息译成客户语言（术语表+TM，"
             "自带「已是客户语言则跳过」检测护栏）。关则外语客户可能收到非其语言文本。"),

    # Tier 3 —— 主动触达（高风险：主动给客户发未经请求的消息）
    Capability(
        "proactive_topic", "主动话题(沉默回访)", 3, "high", "feature",
        "companion.proactive_topic.enabled", parent_path="companion.enabled",
        dry_run_path="companion.proactive_topic.dry_run",
        runtime_dep="companion_proactive_preview",
        calibration="/api/companion/proactive/preview",
        desc="久未说话的用户，从其记忆挑话题主动开场。开前先用 preview/sample 校准。"),
    Capability(
        "proactive_care", "主动关怀(承诺到点)", 3, "high", "feature",
        "companion.proactive_care.enabled", parent_path="companion.enabled",
        dry_run_path="companion.proactive_care.dry_run",
        runtime_dep="care_schedule_store",
        calibration="/api/companion/proactive/preview",
        desc="抽取入站约定(周五面试…)到点主动关心。复用 deferred 队列享 gate/pacing。"),
    Capability(
        "multiplatform_deferred", "多平台主动发送队列", 3, "high", "feature",
        "companion.multiplatform_deferred.enabled", parent_path="companion.enabled",
        runtime_dep="deferred_outbox_store",
        desc="care/reactivation 的非 messenger 主动消息发送闭环。关则非 messenger 约定被丢弃。"),

    # Tier 4 —— 全自动语音（高风险：真发语音消息 / 实时全双工通话）
    Capability(
        "voice_autosend", "全自动语音", 4, "high", "feature",
        "inbox.l2_autosend.voice.enabled", runtime_dep="autosend_worker",
        desc="把「全自动聊天+翻译+语音」凑齐成闭环：自动回复转语音/克隆音发出。"),
    Capability(
        "realtime_voice", "实时共情语音通话", 4, "high", "feature",
        "realtime_voice.enabled",
        calibration="/api/companion/capabilities/realtime-voice-calibration",
        desc="浏览器全双工实时语音（MiniCPM-o WS 网关）。开前确认主机可达、GPU 模型已载入、"
             "公网部署配 access_token；与全自动语音消息独立。"),
]


def _recommend(stage: str, cap: Capability, blocked_reason: str) -> str:
    if stage == "blocked":
        return blocked_reason
    if stage == "dry_run":
        return "灰度中：审采样/预览无误后关 dry_run 转真发"
    if stage == "active":
        return "运行中"
    # stage == off
    if cap.kind == "safeguard":
        return ("建议开启（安全栈，零行为风险）" if cap.tier == 0
                else "建议开启（出站安全闸，与真发一起开）")
    if cap.risk in ("none", "low"):
        return "可安全开启"
    return "高风险：开启前先 dry_run/预览校准，并确保 send-gate 已开"


def evaluate_capability(
    cap: Capability, config: Any, runtime: Optional[Dict[str, bool]],
) -> Dict[str, Any]:
    """对单个能力算就绪状态（纯函数）。"""
    parent_enabled = True
    if cap.parent_path:
        parent_enabled = bool(_dig(config, cap.parent_path, cap.parent_default))
    dry_run = bool(_dig(config, cap.dry_run_path, False)) if cap.dry_run_path else False

    # 运行时子系统是否挂载：runtime 为 None=未知(不判 blocked)；显式 False=未挂载
    runtime_known = runtime is not None and cap.runtime_dep != ""
    runtime_ok = bool((runtime or {}).get(cap.runtime_dep)) if runtime_known else None

    # 开关判定。默认：缺失即视为关。
    # 三态例外（unset_follows_runtime，对齐 P0-3 B8）：flag **未显式配置**时跟随
    # runtime_dep 可用性——有引擎即默认开，防「运行时按有引擎自动翻但看板显示 off」相悖。
    _raw_flag = _dig(config, cap.flag_path, None)
    if cap.unset_follows_runtime and _raw_flag is None:
        # 未配置 → 跟随运行时（未知则保守按关，与 inbound_translate 无引擎保持关一致）
        enabled = bool(runtime_ok) if runtime_known else False
    else:
        enabled = bool(_raw_flag)

    preconditions: List[Dict[str, Any]] = []
    if cap.parent_path:
        preconditions.append({
            "name": f"父开关 {cap.parent_path}", "ok": parent_enabled,
            "detail": "" if parent_enabled else "父开关关闭 → 本能力整档失效"})
    if cap.runtime_dep:
        preconditions.append({
            "name": f"子系统 {cap.runtime_dep}",
            "ok": (runtime_ok is True) if runtime_known else None,
            "detail": ("子系统已挂载" if runtime_ok else
                       ("子系统未挂载（重启/配置缺失）" if runtime_known else
                        "运行时未知（未传 runtime）"))})

    blocked_reason = ""
    if not parent_enabled:
        stage = "blocked"
        blocked_reason = f"父开关 {cap.parent_path} 关闭，先开父开关"
    elif not enabled:
        stage = "off"
    elif runtime_known and runtime_ok is False:
        stage = "blocked"
        blocked_reason = (f"开关已开但子系统 {cap.runtime_dep} 未挂载"
                          "（重启/配置缺失），修复后才真生效")
    elif dry_run:
        stage = "dry_run"
    else:
        stage = "active"

    return {
        "key": cap.key, "label": cap.label, "tier": cap.tier,
        "risk": cap.risk, "kind": cap.kind, "flag_path": cap.flag_path,
        "desc": cap.desc, "critical": cap.critical,
        "calibration": cap.calibration,
        "enabled": enabled, "parent_enabled": parent_enabled,
        "dry_run": dry_run, "dry_run_supported": bool(cap.dry_run_path),
        "stage": stage,
        "preconditions": preconditions,
        "recommended": _recommend(stage, cap, blocked_reason),
    }


_TIER_LABELS = {
    0: "提示层安全栈(零风险)", 1: "坐席/翻译/观测(低风险)",
    2: "全自动文本回复(高风险)", 3: "主动触达(高风险)", 4: "全自动语音(高风险)",
}


def collect_capability_status(
    config: Any, *, runtime: Optional[Dict[str, bool]] = None,
) -> Dict[str, Any]:
    """聚合全部陪伴能力就绪度 + 分阶段开启阶梯视图 + 摘要。

    config: 原始 config dict（``config_manager.config``）。
    runtime: 可选 ``{runtime_dep: bool}`` 子系统挂载信号；不传则不判 runtime-blocked。
    """
    caps = [evaluate_capability(c, config, runtime) for c in CAPABILITIES]

    by_stage: Dict[str, int] = {"off": 0, "active": 0, "dry_run": 0, "blocked": 0}
    for c in caps:
        by_stage[c["stage"]] = by_stage.get(c["stage"], 0) + 1

    # 分阶段阶梯：按 tier 分组，标该档是否「整档点亮」（active/dry_run 视为已推进）
    ladder: List[Dict[str, Any]] = []
    for tier in sorted(_TIER_LABELS):
        items = [c for c in caps if c["tier"] == tier]
        if not items:
            continue
        lit = sum(1 for c in items if c["stage"] in ("active", "dry_run"))
        ladder.append({
            "tier": tier, "label": _TIER_LABELS[tier],
            "lit": lit, "total": len(items),
            "complete": lit == len(items),
            "keys": [c["key"] for c in items],
        })

    # 行动建议高亮：安全栈未开 / 主开关状态 / 灰度待转 / 已开但 blocked
    safeguards_off = [c["key"] for c in caps
                      if c["kind"] == "safeguard" and c["stage"] == "off"]
    blocked = [{"key": c["key"], "why": c["recommended"]}
               for c in caps if c["stage"] == "blocked"]
    dry_running = [c["key"] for c in caps if c["stage"] == "dry_run"]
    master = next((c for c in caps if c["critical"]), None)

    return {
        "capabilities": caps,
        "ladder": ladder,
        "summary": {
            "total": len(caps),
            "by_stage": by_stage,
            "safeguards_off": safeguards_off,
            "blocked": blocked,
            "dry_running": dry_running,
            "master_delivery_on": bool(master and master["stage"] == "active"),
        },
    }


__all__ = [
    "Capability", "CAPABILITIES", "collect_capability_status",
    "evaluate_capability",
]
