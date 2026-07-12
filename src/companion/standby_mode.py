"""「AI 值守」三档姿态：把专家级 autosend 配置收敛成收件箱里的一键开关（纯计划层）。

背景
====
全自动真发对运营是「专家配置」：要懂 ``inbox.l2_autosend.enabled``(worker) + ``.deliver``
主开关 + 会话档=全自动(auto_ai) 的双重 opt-in，还要配 send-gate。P1-2 把这层**反应式自动
回复轴**（收到消息→AI 处理）收敛成一个收件箱级的三档姿态，人只需选「AI 处于什么状态」：

  - ``off``       关闭：AI 不自动出草稿、也不自动发（坐席全手动）。
  - ``suggest``   仅建议：AI 自动出草稿供坐席审，**绝不自动发**（worker 开、deliver 关）。
  - ``watching``  值守中：AI 自动回复（worker+deliver 开）+ **自动开出站安全闸 send-gate**
                  （安全护栏，开着只会拦风险发送、绝无害 → 一键即得「受保护的自动回复」）；
                  真发仍受**会话档=全自动**护栏（沿用 ``capability_toggle.check_toggle`` 权威
                  判定，此处不放松）。send-gate 是 safeguard，``_order`` 保证它在 deliver **之前**
                  开——闸先立起来，再武装真发。

设计边界（刻意收窄，别把「值守」做成大杂烩）
==========================================
- **只治理反应式 autosend 轴**：``l2_autosend_worker`` + ``l2_autosend_deliver`` +（值守中额外)
  ``companion_send_gate`` 出站安全闸。每档**只写它需要的键**：off/suggest **不动 send-gate**
  （安全闸黏住不误关，nothing-sends 时它开着也无害）。
- **主动触达(proactive)/语音(voice)/翻译各自独立治理**——它们是不同风险轴（tier3/4），
  由能力看板或 nurture 预设分别开关；「值守」= AI 守着自动回复，不隐式打开「主动找客户搭话」，
  免得运营点「值守中」意外触发主动外呼。
- **复用而非另起炉灶**：意图排序 ``_intentions_for``/``_order``、护栏 ``check_toggle``、
  overlay 写入 ``_apply_plan``、快照回滚 ``capture_snapshot`` 全部沿用 presets 那套机制，
  本模块只提供「三档 → 该两能力目标态」的收敛映射 + 反推当前档位，零副作用、可单测。

与既有控件的关系（正交、互补，不重复）
====================================
- **AI 值守（本模块，workspace 全局）**：AI 整体姿态（关/仅建议/值守）。
- **会话档位（每会话 mode-select，已有）**：这个会话让 AI 自动到什么程度（含 auto_ai）。
  值守中把 deliver 全局打开，但真发仍只对被设为 auto_ai 的会话发生——两者组合，护栏强制。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .capability_presets import CAP_BY_KEY, _intentions_for, _order
from .capability_status import _dig

# 三档姿态（低→高风险有序；UI 分段控件按此序渲染）
STANDBY_MODES: List[str] = ["off", "suggest", "watching"]

STANDBY_LABELS: Dict[str, str] = {
    "off": "关闭",
    "suggest": "仅建议",
    "watching": "值守中",
}

# 每档 → 目标态映射（**只列该档要写的键**；未列的键不动，如 off/suggest 不碰 send-gate）。
# 键的书写顺序即关闭方向的插入序：deliver 写在 worker 前 → 关档时先撤真发再撤 worker；
# 开档方向由 _order 按 priority 重排（safeguard=0 < worker=12 < critical deliver=50），
# 故值守中开时：send-gate 先立 → worker → deliver 最后武装，任一时刻无危险窗口。
_STANDBY_STATES: Dict[str, Dict[str, str]] = {
    "off": {"l2_autosend_deliver": "off", "l2_autosend_worker": "off"},
    "suggest": {"l2_autosend_deliver": "off", "l2_autosend_worker": "on"},
    "watching": {"l2_autosend_deliver": "on", "l2_autosend_worker": "on",
                 "companion_send_gate": "on"},
}

# 本姿态可能触碰的全部能力键（用于「不越界」不变量测试/文档；infer 只看 worker/deliver）。
STANDBY_KEYS = ("l2_autosend_deliver", "l2_autosend_worker", "companion_send_gate")


def is_standby_mode(name: str) -> bool:
    return name in _STANDBY_STATES


def build_standby_plan(mode: str) -> Optional[List[Dict[str, Any]]]:
    """姿态名 → 有序意图列表（只含该档声明的键）；未知姿态返回 None。

    复用 presets 的 ``_intentions_for``（含 dry_run 字段归零）+ ``_order``（关先于开、
    critical 主开关压最后），确保与预设/回滚走完全一致的执行序与护栏。
    """
    spec = _STANDBY_STATES.get(mode)
    if spec is None:
        return None
    plan: List[Dict[str, Any]] = []
    for key, state in spec.items():   # 只写该档声明的键；未列键不动
        cap = CAP_BY_KEY.get(key)
        if cap is None:
            continue
        plan.extend(_intentions_for(cap, state))
    return _order(plan)


def infer_standby_mode(config: Any) -> str:
    """从当前 config 反推所处姿态：off|suggest|watching|custom。

    读两键 flag_path 现值（经能力注册表取路径，抗路径漂移）。deliver 开但 worker 关＝
    自相矛盾配置，归 ``custom``（UI 不高亮任何档，提示按需重设）。
    """
    worker_cap = CAP_BY_KEY.get("l2_autosend_worker")
    deliver_cap = CAP_BY_KEY.get("l2_autosend_deliver")
    worker = bool(_dig(config, worker_cap.flag_path, False)) if worker_cap else False
    deliver = bool(_dig(config, deliver_cap.flag_path, False)) if deliver_cap else False
    if worker and deliver:
        return "watching"
    if worker and not deliver:
        return "suggest"
    if not worker and not deliver:
        return "off"
    return "custom"


def standby_options() -> List[Dict[str, str]]:
    """给前端的有序档位选项 [{mode,label}]。"""
    return [{"mode": m, "label": STANDBY_LABELS[m]} for m in STANDBY_MODES]


__all__ = [
    "STANDBY_MODES", "STANDBY_LABELS", "STANDBY_KEYS",
    "is_standby_mode", "build_standby_plan", "infer_standby_mode",
    "standby_options",
]
