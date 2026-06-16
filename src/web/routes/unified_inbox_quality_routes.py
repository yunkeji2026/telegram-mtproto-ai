"""统一收件箱——AI 回复质量闭环看板（P3-1）。

与 P0-3 ROI 看板互补：ROI 看「省了多少」，质量看「AI 答得对不对」。把 draft_audit_log
的动作处置（autosend/approved/edit_send/rejected/blocked）+ 风险等级（L1-L4）聚合成
质量指标——自动通过率 / 人工改写率 / 拒绝率 / 高风险拦截率 + 环比 + 趋势，供运营持续
优化 AI（调话术 / KB / 自动化阈值），而非只看产出。

数据全部来自 ``InboxStore.get_quality_stats``（真实审计口径，含 AI 无主 autosend）。
:func:`build_quality_summary` 为纯函数，便于单测；端点 ``GET /api/workspace/ai-quality``
（主管专属）。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

from fastapi import Request

from src.web.routes.unified_inbox_auth import _require_supervisor
from src.web.routes.unified_inbox_services import _inbox_store

logger = logging.getLogger(__name__)


def _pct_delta(cur: float, prev: float) -> Optional[float]:
    """环比变化（百分点；率本身是 0–1，这里返回 *100 后的点差）。prev 无数据→None。"""
    if prev is None:
        return None
    return round((cur - prev) * 100, 1)


def build_quality_summary(request: Request, span: int = 7) -> Dict[str, Any]:
    """聚合 AI 回复质量概览（纯逻辑；store 缺失时优雅降级为空）。"""
    span = 30 if int(span or 7) >= 30 else 7
    now = int(time.time())
    lt = time.localtime(now)
    midnight = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))
    since = midnight - (span - 1) * 86400
    prev_since = since - span * 86400

    out: Dict[str, Any] = {"ok": True, "days": span, "available": False}
    inbox = _inbox_store(request)
    if inbox is None or not hasattr(inbox, "get_quality_stats"):
        return out

    try:
        cur = inbox.get_quality_stats(since)
    except Exception:
        logger.debug("质量统计失败（已忽略）", exc_info=True)
        return out

    # 环比：上一等长窗口 [prev_since, since)，用 until_ts 干净切分（旧 store 无此参时跳过）。
    prev: Dict[str, Any] = {}
    try:
        prev = inbox.get_quality_stats(prev_since, until_ts=since)
    except TypeError:
        logger.debug("store 不支持 until_ts，跳过质量环比", exc_info=True)
    except Exception:
        logger.debug("质量环比统计失败（已忽略）", exc_info=True)

    out["available"] = True
    out["counts"] = cur.get("counts", {})
    out["levels"] = cur.get("levels", {})
    out["total"] = cur.get("total", 0)
    out["metrics"] = {
        "auto_pass_rate": round(cur.get("auto_pass_rate", 0.0) * 100, 1),
        "edit_rate": round(cur.get("edit_rate", 0.0) * 100, 1),
        "reject_rate": round(cur.get("reject_rate", 0.0) * 100, 1),
        "block_rate": round(cur.get("block_rate", 0.0) * 100, 1),
        "high_risk_rate": round(cur.get("high_risk_rate", 0.0) * 100, 1),
    }
    out["compare"] = {
        "auto_pass_delta_pp": _pct_delta(
            cur.get("auto_pass_rate", 0.0), prev.get("auto_pass_rate")) if prev else None,
        "edit_delta_pp": _pct_delta(
            cur.get("edit_rate", 0.0), prev.get("edit_rate")) if prev else None,
        "reject_delta_pp": _pct_delta(
            cur.get("reject_rate", 0.0), prev.get("reject_rate")) if prev else None,
    }
    out["trend"] = cur.get("trend", [])
    # 运营提示：把指标翻译成「该做什么」
    out["hints"] = _quality_hints(out["metrics"], out["total"])
    return out


def _quality_hints(m: Dict[str, Any], total: int) -> list:
    """把质量率转成可执行运营建议（阈值保守，仅在有足够样本时给）。"""
    hints = []
    if total < 20:
        hints.append({"level": "info",
                      "text": "样本较少，指标仅供参考；积累更多处置后更可信。"})
        return hints
    if m["edit_rate"] >= 40:
        hints.append({"level": "warn",
                      "text": f"人工改写率偏高（{m['edit_rate']}%）：AI 初稿质量待提升，建议补充 KB 话术或调整人设。"})
    if m["reject_rate"] >= 20:
        hints.append({"level": "warn",
                      "text": f"拒绝率偏高（{m['reject_rate']}%）：AI 频繁答错，检查知识库覆盖与触发词。"})
    if m["high_risk_rate"] >= 30:
        hints.append({"level": "warn",
                      "text": f"高风险（L3+L4）占比偏高（{m['high_risk_rate']}%）：复核风险规则或敏感场景话术。"})
    if m["auto_pass_rate"] >= 60 and m["edit_rate"] < 20 and m["reject_rate"] < 10:
        hints.append({"level": "good",
                      "text": f"AI 自动通过率 {m['auto_pass_rate']}% 且改写/拒绝低，质量健康，可考虑提高自动化档位。"})
    if not hints:
        hints.append({"level": "info", "text": "质量指标在正常区间。"})
    return hints


def register_quality_routes(app, *, api_auth) -> None:
    """挂载 AI 回复质量看板端点（GET /api/workspace/ai-quality，主管专属）。"""

    @app.get("/api/workspace/ai-quality")
    async def api_workspace_ai_quality(request: Request, days: int = 7):
        """AI 回复质量：自动通过/改写/拒绝/高风险率 + 环比 + 趋势 + 运营建议。"""
        api_auth(request)
        _require_supervisor(request)
        return build_quality_summary(request, days)
