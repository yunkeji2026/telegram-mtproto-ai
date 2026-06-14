"""坐席自动派单（建议接管）服务。

定位（MVP）：纯逻辑、可完整单测。给定在线坐席（presence）、现有认领（claims，用于
算每人负载）与会话元数据，为**未认领**会话挑选「建议接管」的坐席。

设计契约（勿误改）：
  - 默认关闭（``workspace.auto_assign.enabled``），遵循本仓新子系统 feature-flag 约定。
  - 本服务只产出「建议」（suggested_agent），**不改变 claim 状态**——真正接管仍由坐席/
    主管显式 claim。因此对现有手动认领流程零破坏、无误锁风险。
  - 选择策略默认 ``least_loaded``：在候选坐席里挑当前认领数最少者，agent_id 升序做
    确定性 tiebreak（便于测试与可预期）。批量建议时在批内累加负载，避免把同一批未认领
    会话全部挤给同一个坐席（自然产生轮转效果）。
  - 语言/平台匹配为预留能力：当前 presence 行不携带坐席技能数据，``match_language`` 仅作
    占位开关；真正按语言派单需后续补「坐席技能」数据源（见 ROADMAP）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 默认配置（与 config.example.yaml::workspace.auto_assign 对齐）
DEFAULTS: Dict[str, Any] = {
    "enabled": False,
    "strategy": "least_loaded",      # 目前仅 least_loaded（round_robin 预留）
    "max_claims_per_agent": 0,        # 0 = 不限；>0 时超过该负载的坐席不再被建议
    "online_only": True,              # True=仅 status=online；False=online/busy 皆可（仍排除 offline）
    "match_language": False,          # 预留：按会话语言匹配坐席技能（当前无技能数据，不生效）
}

_VALID_STRATEGIES = {"least_loaded", "round_robin"}


def parse_auto_assign_config(full_config: Any) -> Dict[str, Any]:
    """从完整 config（dict）解析 workspace.auto_assign，缺省回落 DEFAULTS。"""
    out: Dict[str, Any] = dict(DEFAULTS)
    try:
        ws = (full_config or {}).get("workspace") or {}
        aa = ws.get("auto_assign") or {}
        if isinstance(aa, dict):
            for k in DEFAULTS:
                if k in aa and aa[k] is not None:
                    out[k] = aa[k]
    except Exception:
        logger.debug("解析 auto_assign 配置失败，使用默认值", exc_info=True)
    # 归一化与防御
    out["enabled"] = bool(out["enabled"])
    out["online_only"] = bool(out["online_only"])
    out["match_language"] = bool(out["match_language"])
    try:
        out["max_claims_per_agent"] = max(0, int(out["max_claims_per_agent"] or 0))
    except Exception:
        out["max_claims_per_agent"] = 0
    if out.get("strategy") not in _VALID_STRATEGIES:
        out["strategy"] = "least_loaded"
    return out


class AssignmentService:
    """坐席派单建议器（薄封装选择策略，无副作用）。"""

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        # config 可以是已解析的 auto_assign dict，也可直接给 DEFAULTS 子集
        merged = dict(DEFAULTS)
        if isinstance(config, dict):
            for k in DEFAULTS:
                if k in config and config[k] is not None:
                    merged[k] = config[k]
        merged["enabled"] = bool(merged["enabled"])
        merged["online_only"] = bool(merged["online_only"])
        try:
            merged["max_claims_per_agent"] = max(0, int(merged["max_claims_per_agent"] or 0))
        except Exception:
            merged["max_claims_per_agent"] = 0
        if merged.get("strategy") not in _VALID_STRATEGIES:
            merged["strategy"] = "least_loaded"
        self.cfg = merged

    @classmethod
    def from_config(cls, full_config: Any) -> "AssignmentService":
        return cls(parse_auto_assign_config(full_config))

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled"))

    # ── 内部 ──────────────────────────────────────────────────

    def eligible_agents(self, presence: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """从 presence 列表筛出可被派单的坐席。"""
        out: List[Dict[str, Any]] = []
        for p in presence or []:
            if not p or not p.get("agent_id"):
                continue
            st = str(p.get("status") or "").lower()
            if st == "offline":
                continue
            if self.cfg["online_only"] and st != "online":
                continue
            out.append(p)
        return out

    @staticmethod
    def _load_map(claims: List[Dict[str, Any]]) -> Dict[str, int]:
        """统计每个坐席当前的活跃认领数。"""
        m: Dict[str, int] = {}
        for c in claims or []:
            a = str((c or {}).get("agent_id") or "")
            if a:
                m[a] = m.get(a, 0) + 1
        return m

    # ── 选择 ──────────────────────────────────────────────────

    def suggest(
        self,
        *,
        presence: List[Dict[str, Any]],
        claims: List[Dict[str, Any]],
        conv: Optional[Dict[str, Any]] = None,
        extra_load: Optional[Dict[str, int]] = None,
    ) -> Optional[Dict[str, Any]]:
        """为单个会话挑选建议坐席；无合格坐席返回 None。

        extra_load：批量场景下本批已建议的累加负载，叠加到真实 claim 负载之上。
        """
        agents = self.eligible_agents(presence)
        if not agents:
            return None
        load = self._load_map(claims)
        if extra_load:
            for k, v in extra_load.items():
                load[k] = load.get(k, 0) + int(v or 0)
        cap = int(self.cfg.get("max_claims_per_agent") or 0)
        if cap > 0:
            agents = [a for a in agents if load.get(str(a["agent_id"]), 0) < cap]
            if not agents:
                return None
        # least_loaded：负载升序，agent_id 升序做确定性 tiebreak
        agents.sort(key=lambda a: (load.get(str(a["agent_id"]), 0), str(a["agent_id"])))
        best = agents[0]
        aid = str(best["agent_id"])
        return {
            "agent_id": aid,
            "agent_name": str(best.get("display_name") or "") or aid,
            "load": load.get(aid, 0),
            "strategy": self.cfg["strategy"],
        }

    def suggest_for_chats(
        self,
        *,
        chats: List[Dict[str, Any]],
        presence: List[Dict[str, Any]],
        claims: List[Dict[str, Any]],
    ) -> Dict[str, Dict[str, Any]]:
        """批量为未认领会话建议坐席，返回 {conversation_id: suggestion}。

        - 已被认领的会话跳过（不覆盖现有 claim）。
        - 批内累加负载，避免把同一批会话挤给同一坐席。
        - 未启用时返回空 dict（调用方据此不附加任何字段）。
        """
        if not self.enabled or not chats:
            return {}
        claimed = {
            str((c or {}).get("conversation_id") or "")
            for c in (claims or [])
            if (c or {}).get("conversation_id")
        }
        extra: Dict[str, int] = {}
        out: Dict[str, Dict[str, Any]] = {}
        for c in chats:
            cid = str((c or {}).get("conversation_id") or "")
            if not cid or cid in claimed:
                continue
            sug = self.suggest(
                presence=presence, claims=claims, conv=c, extra_load=extra,
            )
            if sug:
                out[cid] = sug
                extra[sug["agent_id"]] = extra.get(sug["agent_id"], 0) + 1
        return out
