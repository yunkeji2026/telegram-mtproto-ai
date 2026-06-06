"""ReportGenerator — M2 工作日报/周报自动生成。

数据来源（全部来自已有 API，无额外 DB 查询）：
  - draft_audit_log  → 各坐席处置量 + 动作分布
  - conversation_meta → 情绪趋势 + CSAT 分布
  - reply_drafts      → 各 level 待审/已解决数
  - escalations       → 升级事件数

报告结构：
  period_label   : "今日（2026-06-06）" / "本周（06/01–06/06）"
  date_range     : {from_ts, to_ts}
  draft_stats    : {total, by_level, resolved, pending}
  agent_perf     : [{agent_id, total, approved, rejected, avg_csat}]
  sla_stats      : {breach_count, compliance_rate}
  top_intents    : [最多 5 个意图]
  csat_dist      : {excellent, good, fair, poor}  # 评分区间分布
  system         : {autosend_total, webhook_sent, ...}  # 来自 app.state 快照（可选）

输出格式：
  generate()    → dict（JSON API）
  format_text() → Markdown-like 文本（用于 Webhook 推送）
  format_html() → HTML 片段（用于 dashboard 展示）
"""

from __future__ import annotations

import datetime
import time
from collections import Counter
from typing import Any, Dict, List, Optional


class ReportGenerator:
    """M2：工作日报/周报生成器。

    Usage::

        gen = ReportGenerator(inbox_store=store)
        data = gen.generate(period="daily")
        text = gen.format_text(data)
    """

    def __init__(
        self,
        inbox_store: Any,
        draft_service: Any = None,
        app_state: Any = None,
    ) -> None:
        self._store = inbox_store
        self._svc = draft_service
        self._app_state = app_state  # 用于读取 AutosendWorker / SLAWatcher 快照

    # ── 数据生成 ──────────────────────────────────────────────────────────

    def generate(
        self,
        period: str = "daily",
        reference_ts: Optional[float] = None,
    ) -> Dict[str, Any]:
        """生成指定周期的工作报告数据字典。

        period: "daily"（过去 24h）| "weekly"（过去 7 天）
        reference_ts: 报告截止时间戳（默认 now）
        """
        now = float(reference_ts or time.time())
        period = str(period or "daily").lower()
        days = 7 if period == "weekly" else 1
        since_ts = now - days * 86400

        from_dt = datetime.datetime.fromtimestamp(since_ts)
        to_dt = datetime.datetime.fromtimestamp(now)
        if period == "weekly":
            period_label = f"本周（{from_dt.strftime('%m/%d')}–{to_dt.strftime('%m/%d')}）"
        else:
            period_label = f"今日（{to_dt.strftime('%Y-%m-%d')}）"

        report: Dict[str, Any] = {
            "period": period,
            "period_label": period_label,
            "generated_at": now,
            "date_range": {"from_ts": since_ts, "to_ts": now},
        }

        # ① 坐席绩效（draft_audit_log 聚合）
        try:
            perf = self._store.get_agent_perf(since_ts=since_ts)
            report["agent_perf"] = perf
            report["total_processed"] = sum(p.get("total", 0) for p in perf)
            report["total_approved"] = sum(p.get("approved", 0) for p in perf)
            report["total_rejected"] = sum(p.get("rejected", 0) for p in perf)
            report["total_autosend"] = sum(p.get("autosend", 0) for p in perf)
        except Exception:
            report["agent_perf"] = []
            report["total_processed"] = 0

        # ② 草稿统计
        try:
            if self._svc is not None:
                all_drafts = self._svc.list_drafts(limit=2000)
                # 当期创建的草稿
                period_drafts = [
                    d for d in all_drafts
                    if float(d.get("created_at") or d.get("created_ts") or 0) >= since_ts
                ]
                by_level: Dict[str, int] = Counter(
                    str(d.get("autopilot_level") or "?") for d in period_drafts
                )
                pending = sum(1 for d in period_drafts if d.get("status") == "pending")
                resolved = sum(1 for d in period_drafts if d.get("status") in ("approved", "rejected"))
                report["draft_stats"] = {
                    "total_in_period": len(period_drafts),
                    "by_level": dict(by_level),
                    "pending": pending,
                    "resolved": resolved,
                }
            else:
                report["draft_stats"] = {}
        except Exception:
            report["draft_stats"] = {}

        # ③ CSAT 分布（conversation_meta）
        try:
            with self._store._lock:
                rows = self._store._conn.execute(
                    "SELECT csat_score FROM conversation_meta "
                    "WHERE csat_score >= 0 AND updated_at >= ?",
                    (since_ts,),
                ).fetchall()
            scores = [float(r[0]) for r in rows if r[0] is not None]
            csat_dist = {
                "excellent": sum(1 for s in scores if s >= 4.5),
                "good": sum(1 for s in scores if 3.5 <= s < 4.5),
                "fair": sum(1 for s in scores if 2.5 <= s < 3.5),
                "poor": sum(1 for s in scores if s < 2.5),
            }
            avg_csat = round(sum(scores) / len(scores), 1) if scores else None
            report["csat"] = {
                "distribution": csat_dist,
                "avg": avg_csat,
                "count": len(scores),
            }
        except Exception:
            report["csat"] = {}

        # ④ 意图 Top 5
        try:
            with self._store._lock:
                rows = self._store._conn.execute(
                    "SELECT last_intent, COUNT(*) AS cnt FROM conversation_meta "
                    "WHERE updated_at >= ? AND last_intent != '' "
                    "GROUP BY last_intent ORDER BY cnt DESC LIMIT 5",
                    (since_ts,),
                ).fetchall()
            report["top_intents"] = [{"intent": r[0], "count": r[1]} for r in rows]
        except Exception:
            report["top_intents"] = []

        # ⑤ SLA 数据
        try:
            audit_logs = self._store.list_draft_audit(limit=2000)
            period_audit = [r for r in audit_logs if float(r.get("ts") or 0) >= since_ts]
            breach_count = sum(1 for r in period_audit if r.get("action") == "auto_reassigned")
            total_decisions = len([r for r in period_audit if r.get("action") in ("approved", "rejected", "autosend", "force_override")])
            # SLA 达标率：近似用"审批中无 force_override 占比"
            forced = sum(1 for r in period_audit if r.get("action") == "force_override")
            compliance_rate = round(
                (1 - forced / total_decisions) * 100, 1
            ) if total_decisions > 0 else 100.0
            report["sla_stats"] = {
                "breach_count": breach_count,
                "compliance_rate": compliance_rate,
                "total_decisions": total_decisions,
                "force_overrides": forced,
            }
        except Exception:
            report["sla_stats"] = {}

        # ⑥ 系统子系统快照（来自 app_state，可选）
        sys_snap: Dict[str, Any] = {}
        try:
            if self._app_state is not None:
                w = getattr(self._app_state, "autosend_worker", None)
                if w:
                    sys_snap["autosend"] = {
                        "total_sent": w.total_sent,
                        "total_errors": w.total_errors,
                    }
                sw = getattr(self._app_state, "sla_watcher", None)
                if sw:
                    sys_snap["sla_watcher"] = {
                        "breach_events": sw.total_breach_events,
                        "reassigned": sw.total_reassigned,
                    }
                whn = getattr(self._app_state, "webhook_notifier", None)
                if whn:
                    sys_snap["webhook"] = {
                        "sent": whn.total_sent,
                        "errors": whn.total_errors,
                    }
        except Exception:
            pass
        report["system"] = sys_snap

        return report

    # ── 格式化 ──────────────────────────────────────────────────────────

    def format_text(self, report: Dict[str, Any]) -> str:
        """将报告数据格式化为 Markdown-like 文本（用于 Webhook 推送）。"""
        lines = [f"📊 **{report.get('period_label', '工作报告')}**\n"]

        # 总处理量
        lines.append(
            f"处理草稿：{report.get('total_processed', 0)} 条"
            f"（批准 {report.get('total_approved', 0)} / "
            f"拒绝 {report.get('total_rejected', 0)} / "
            f"自动发 {report.get('total_autosend', 0)}）"
        )

        # CSAT
        csat = report.get("csat") or {}
        if csat.get("avg") is not None:
            dist = csat.get("distribution") or {}
            lines.append(
                f"CSAT 均分：{csat['avg']:.1f}⭐ "
                f"（{csat.get('count', 0)} 条会话 | "
                f"优{dist.get('excellent',0)} 良{dist.get('good',0)} "
                f"中{dist.get('fair',0)} 差{dist.get('poor',0)}）"
            )

        # SLA
        sla = report.get("sla_stats") or {}
        if sla:
            lines.append(
                f"SLA 达标率：{sla.get('compliance_rate', 100)}%"
                f"（强制放行 {sla.get('force_overrides', 0)} 次 / "
                f"再分配 {sla.get('breach_count', 0)} 次）"
            )

        # 草稿分布
        ds = report.get("draft_stats") or {}
        if ds.get("by_level"):
            lvl_parts = " / ".join(f"{k}: {v}" for k, v in sorted(ds["by_level"].items()))
            lines.append(f"草稿分布：{lvl_parts}")

        # Top 意图
        intents = report.get("top_intents") or []
        if intents:
            intent_str = " · ".join(f"{i['intent']}({i['count']})" for i in intents[:3])
            lines.append(f"热门意图：{intent_str}")

        # 坐席绩效 Top 3
        perf = report.get("agent_perf") or []
        if perf:
            lines.append("坐席绩效 Top 3：")
            for p in perf[:3]:
                csat_s = f" | CSAT {p['avg_csat']:.1f}⭐" if p.get("avg_csat") is not None else ""
                lines.append(
                    f"  · {p.get('agent_id', '?')}: "
                    f"处理 {p.get('total', 0)} 批准 {p.get('approved', 0)}{csat_s}"
                )

        # 系统
        sys = report.get("system") or {}
        if sys.get("webhook"):
            lines.append(f"Webhook 推送：{sys['webhook'].get('sent', 0)} 次")

        lines.append("\n[📋 查看工作台](/workspace/dashboard)")
        return "\n".join(lines)

    def format_html(self, report: Dict[str, Any]) -> str:
        """将报告数据格式化为 HTML 片段（用于 dashboard 模态框展示）。"""
        def _row(label: str, value: str) -> str:
            return (
                f'<div style="display:flex;justify-content:space-between;padding:6px 0;'
                f'border-bottom:1px solid #f1f5f9;font-size:13px;">'
                f'<span style="color:#64748b;">{label}</span>'
                f'<strong>{value}</strong></div>'
            )

        rows = []
        rows.append(_row(
            "草稿总处理",
            f"{report.get('total_processed', 0)} 条"
            f"（批准 {report.get('total_approved', 0)} / 拒绝 {report.get('total_rejected', 0)}）"
        ))
        rows.append(_row("自动发送", f"{report.get('total_autosend', 0)} 条 L2 草稿"))

        csat = report.get("csat") or {}
        if csat.get("avg") is not None:
            dist = csat.get("distribution") or {}
            rows.append(_row(
                "CSAT 均分",
                f"{csat['avg']:.1f} / 5.0 ⭐（{csat.get('count', 0)} 条会话）"
            ))
            rows.append(_row(
                "CSAT 分布",
                f"优{dist.get('excellent',0)} 良{dist.get('good',0)} "
                f"中{dist.get('fair',0)} 差{dist.get('poor',0)}"
            ))

        sla = report.get("sla_stats") or {}
        if sla:
            rows.append(_row("SLA 达标率", f"{sla.get('compliance_rate', 100)}%"))
            rows.append(_row("强制放行", f"{sla.get('force_overrides', 0)} 次"))

        ds = report.get("draft_stats") or {}
        if ds.get("by_level"):
            lvl_html = "".join(
                f'<span style="padding:2px 6px;border-radius:8px;background:#f1f5f9;'
                f'font-size:11px;margin-right:4px;">{k}: {v}</span>'
                for k, v in sorted(ds["by_level"].items())
            )
            rows.append(
                f'<div style="padding:6px 0;border-bottom:1px solid #f1f5f9;font-size:13px;">'
                f'<span style="color:#64748b;">草稿分布</span><br style="margin:2px 0;"/>'
                f'{lvl_html}</div>'
            )

        intents = report.get("top_intents") or []
        if intents:
            intent_html = "".join(
                f'<span style="padding:2px 6px;border-radius:8px;background:#ede9fe;'
                f'color:#5b21b6;font-size:11px;margin-right:4px;">'
                f'{i["intent"]}×{i["count"]}</span>'
                for i in intents[:5]
            )
            rows.append(
                f'<div style="padding:6px 0;border-bottom:1px solid #f1f5f9;font-size:13px;">'
                f'<span style="color:#64748b;">热门意图</span><br style="margin:2px 0;"/>'
                f'{intent_html}</div>'
            )

        perf = report.get("agent_perf") or []
        if perf:
            rows.append(
                f'<div style="padding:6px 0;font-size:13px;color:#64748b;">坐席绩效</div>'
            )
            for p in perf[:5]:
                csat_s = (
                    f' <span style="color:#f59e0b;">{p["avg_csat"]:.1f}⭐</span>'
                    if p.get("avg_csat") is not None else ""
                )
                rows.append(
                    f'<div style="padding:3px 0 3px 12px;font-size:12px;">'
                    f'👤 <strong>{p.get("agent_id", "?")}</strong> — '
                    f'处理 {p.get("total", 0)} 批准 {p.get("approved", 0)} '
                    f'拒绝 {p.get("rejected", 0)}{csat_s}</div>'
                )

        return "".join(rows)
