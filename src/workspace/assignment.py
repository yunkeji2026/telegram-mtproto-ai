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
  - ``match_language``（P3 已落地）：presence 行经 store LEFT JOIN agent_prefs 携带坐席技能
    语言（``languages`` CSV，坐席在工作台「我的偏好」声明）。开启后把会话语言优先派给会该
    语言的坐席；无人会该语言则回退全体（有人接 > 没人接）。坐席未声明语言则等价于旧行为。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


def _norm_lang(v: Any) -> str:
    """轻量语言码归一（zh-cn→zh / 大小写 / 去空白）。

    复用 translation_service.normalize_lang 保持与会话/出向译文同一套口径；
    导入失败（极端裁剪环境）时回落到「小写 + 取连字符前段」的朴素归一，绝不抛错。
    """
    s = str(v or "").strip()
    if not s:
        return ""
    try:
        from src.ai.translation_service import normalize_lang
        return normalize_lang(s)
    except Exception:
        return s.lower().split("-")[0]


def _agent_langs(p: Dict[str, Any]) -> Set[str]:
    """解析 presence 行携带的坐席技能语言（CSV）为规范码集合。"""
    raw = str((p or {}).get("languages") or "")
    return {x for x in (_norm_lang(t) for t in raw.split(",")) if x}

# 默认配置（与 config.example.yaml::workspace.auto_assign 对齐）
DEFAULTS: Dict[str, Any] = {
    "enabled": False,
    "strategy": "least_loaded",      # 目前仅 least_loaded（round_robin 预留）
    "max_claims_per_agent": 0,        # 0 = 不限；>0 时超过该负载的坐席不再被建议
    "online_only": True,              # True=仅 status=online；False=online/busy 皆可（仍排除 offline）
    "match_language": False,          # 开启后按会话语言优先派给会该语言的坐席（坐席语言在「我的偏好」声明）
}

# 后台自动认领（守护版）子配置。本轮仅落地决策逻辑（plan_auto_claims），后台 worker
# 接线见 ROADMAP 下一阶段。默认关。
AUTO_CLAIM_DEFAULTS: Dict[str, Any] = {
    "enabled": False,
    "active_within_sec": 60,          # 仅把会话自动分给近 N 秒有心跳的活跃坐席（0=不限）
    "ttl_sec": 0,                     # soft 认领 TTL（0=沿用全局 claim_ttl_sec；>0 用更短租约）
}

_VALID_STRATEGIES = {"least_loaded", "round_robin"}


def _coerce_nonneg_int(v: Any) -> int:
    try:
        return max(0, int(v or 0))
    except Exception:
        return 0


def _normalize_auto_assign(raw: Any) -> Dict[str, Any]:
    """把一份 auto_assign 配置（可能不完整 / 含 auto_claim 子段 / 仅给子集）
    归一化为完整、类型安全的 cfg。parse_auto_assign_config 与 __init__ 共用。"""
    out: Dict[str, Any] = dict(DEFAULTS)
    if isinstance(raw, dict):
        for k in DEFAULTS:
            if k in raw and raw[k] is not None:
                out[k] = raw[k]
    out["enabled"] = bool(out["enabled"])
    out["online_only"] = bool(out["online_only"])
    out["match_language"] = bool(out["match_language"])
    out["max_claims_per_agent"] = _coerce_nonneg_int(out["max_claims_per_agent"])
    if out.get("strategy") not in _VALID_STRATEGIES:
        out["strategy"] = "least_loaded"
    # auto_claim 子段
    ac_raw = raw.get("auto_claim") if isinstance(raw, dict) else None
    ac: Dict[str, Any] = dict(AUTO_CLAIM_DEFAULTS)
    if isinstance(ac_raw, dict):
        for k in AUTO_CLAIM_DEFAULTS:
            if k in ac_raw and ac_raw[k] is not None:
                ac[k] = ac_raw[k]
    ac["enabled"] = bool(ac["enabled"])
    ac["active_within_sec"] = _coerce_nonneg_int(ac["active_within_sec"])
    ac["ttl_sec"] = _coerce_nonneg_int(ac["ttl_sec"])
    out["auto_claim"] = ac
    return out


def parse_auto_assign_config(full_config: Any) -> Dict[str, Any]:
    """从完整 config（dict）解析 workspace.auto_assign，缺省回落 DEFAULTS。"""
    aa: Any = {}
    try:
        ws = (full_config or {}).get("workspace") or {}
        aa = ws.get("auto_assign") or {}
    except Exception:
        logger.debug("解析 auto_assign 配置失败，使用默认值", exc_info=True)
        aa = {}
    return _normalize_auto_assign(aa if isinstance(aa, dict) else {})


class AssignmentService:
    """坐席派单建议器（薄封装选择策略，无副作用）。"""

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        # config 可以是已解析的完整 cfg，也可直接给 DEFAULTS / auto_claim 子集
        self.cfg = _normalize_auto_assign(config if isinstance(config, dict) else {})

    @classmethod
    def from_config(cls, full_config: Any) -> "AssignmentService":
        return cls(parse_auto_assign_config(full_config))

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled"))

    @property
    def auto_claim_enabled(self) -> bool:
        return bool((self.cfg.get("auto_claim") or {}).get("enabled"))

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
        # match_language：把会话语言优先派给「会该语言」的坐席。命中则收窄候选到该组，
        # 无人会该语言则回退全体（绝不因语言不匹配把会话晾着——有人接 > 没人接）。
        matched_language = False
        conv_lang = _norm_lang((conv or {}).get("language")) if self.cfg.get("match_language") else ""
        if conv_lang and conv_lang != "unknown":
            speakers = [a for a in agents if conv_lang in _agent_langs(a)]
            if speakers:
                agents = speakers
                matched_language = True
        # least_loaded：负载升序，agent_id 升序做确定性 tiebreak
        agents.sort(key=lambda a: (load.get(str(a["agent_id"]), 0), str(a["agent_id"])))
        best = agents[0]
        aid = str(best["agent_id"])
        return {
            "agent_id": aid,
            "agent_name": str(best.get("display_name") or "") or aid,
            "load": load.get(aid, 0),
            "strategy": self.cfg["strategy"],
            "matched_language": matched_language,
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

    def plan_auto_claims(
        self,
        *,
        chats: List[Dict[str, Any]],
        presence: List[Dict[str, Any]],
        claims: List[Dict[str, Any]],
        now: Optional[float] = None,
        active_within_sec: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """产出后台自动认领计划：``[{conversation_id, agent_id, agent_name}, ...]``。

        守护版决策核心（纯逻辑、无副作用）——真正的 claim 由后台 worker 执行（下一阶段）：
          - 仅当 ``auto_claim.enabled`` 时返回非空；
          - 活跃窗口：``active_within_sec>0`` 时只保留 ``last_seen_at`` 在窗口内的坐席，
            避免把会话静默锁给挂机坐席（默认取 ``auto_claim.active_within_sec``）；
          - 复用 least_loaded 选择 + 批内累加负载，跳过已认领会话。
        """
        ac = self.cfg.get("auto_claim") or {}
        if not ac.get("enabled") or not chats:
            return []
        aw = active_within_sec if active_within_sec is not None else int(ac.get("active_within_sec") or 0)
        pres = list(presence or [])
        if aw and aw > 0:
            import time as _t
            n = now if now is not None else _t.time()
            cutoff = n - aw
            pres = [p for p in pres if float((p or {}).get("last_seen_at") or 0) >= cutoff]
        claimed = {
            str((c or {}).get("conversation_id") or "")
            for c in (claims or [])
            if (c or {}).get("conversation_id")
        }
        extra: Dict[str, int] = {}
        out: List[Dict[str, Any]] = []
        for c in chats:
            cid = str((c or {}).get("conversation_id") or "")
            if not cid or cid in claimed:
                continue
            sug = self.suggest(
                presence=pres, claims=claims, conv=c, extra_load=extra,
            )
            if sug:
                out.append({
                    "conversation_id": cid,
                    "agent_id": sug["agent_id"],
                    "agent_name": sug["agent_name"],
                    "matched_language": sug.get("matched_language", False),
                })
                extra[sug["agent_id"]] = extra.get(sug["agent_id"], 0) + 1
        return out
