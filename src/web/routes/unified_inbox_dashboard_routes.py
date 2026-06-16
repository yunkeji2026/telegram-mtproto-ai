"""统一收件箱——经营日报 CSV / 经理仪表盘路由域（巨石拆分 slice 16b）。

把"逐日经营日报 CSV 导出 + 工作台经理仪表盘（今日/趋势/SLA/首响/解决/翻译漏斗/自动派单）"
这一子域，从 ``register_unified_inbox_routes`` 巨型闭包中外移为
``register_workspace_dashboard_routes(app, *, api_auth, config_manager)``，由主 register 在
**原位置**顺序调用。端点路径/方法/响应零变化（admin_route_inventory URL 契约守卫）。

依赖全部朝下：逐日聚合计算层（slice 16a 已下沉的 unified_inbox_reports）、sla 配置、auth
身份/主管权限、services 存储；web_funnel_snapshot / 翻译·派单统计为 handler 内局部 import。
config_manager 仅供 web_funnel_snapshot 取经营漏斗配置。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict

from fastapi import Request
from fastapi.responses import Response

from src.web.routes.unified_inbox_auth import _require_supervisor, _session_agent
from src.web.routes.unified_inbox_reports import (
    _agent_daily_report_rows,
    _daily_report_rows,
)
from src.web.routes.unified_inbox_services import _contacts_store, _inbox_store
from src.web.routes.unified_inbox_sla import _sla_cfg

logger = logging.getLogger(__name__)


def register_workspace_dashboard_routes(app, *, api_auth, config_manager=None) -> None:
    """挂载经营日报 CSV / 经理仪表盘端点（/api/workspace/daily-report.csv、/api/workspace/dashboard）。"""

    @app.get("/api/workspace/daily-report.csv")
    async def api_workspace_daily_report(
        request: Request, days: int = 7, agent: str = "",
    ):
        """逐日经营日报 CSV（历史回看）：days=7/30，每行一天，含汇总行。

        传 agent → 该坐席个人绩效日报（首响/发送量/完成任务）。
        """
        api_auth(request)
        span = 30 if int(days or 7) >= 30 else 7
        agent = str(agent or "").strip()
        # 团队日报(无 agent)或他人个人日报 → 主管专属；本人个人日报放行
        if not agent or agent != _session_agent(request)["agent_id"]:
            _require_supervisor(request)
        if agent:
            import csv
            import io
            data = _agent_daily_report_rows(request, span, agent)
            buf = io.StringIO()
            buf.write("\ufeff")
            w = csv.writer(buf)
            w.writerow(["date", "first_responded", "frt_avg_sec",
                        "frt_attain_rate_pct", "sends", "tasks_done"])
            tot = {"fr": 0, "sends": 0, "tasks": 0, "frt_sum": 0, "attain": 0}
            for r in data:
                w.writerow([r["date"], r["first_responded"], r["frt_avg_sec"],
                            r["frt_attain_rate"], r["sends"], r["tasks_done"]])
                tot["fr"] += r["first_responded"]
                tot["sends"] += r["sends"]
                tot["tasks"] += r["tasks_done"]
                tot["frt_sum"] += r["frt_avg_sec"] * r["first_responded"]
                tot["attain"] += round(r["frt_attain_rate"] / 100 * r["first_responded"])
            frt_avg = int(tot["frt_sum"] / tot["fr"]) if tot["fr"] else 0
            attain = round(tot["attain"] / tot["fr"] * 100, 1) if tot["fr"] else 0.0
            w.writerow(["合计", tot["fr"], frt_avg, attain, tot["sends"], tot["tasks"]])
            fname = "agent-report-%s-%dd-%s.csv" % (
                agent, span, time.strftime("%Y%m%d", time.localtime()))
            return Response(
                content=buf.getvalue(),
                media_type="text/csv; charset=utf-8",
                headers={"Content-Disposition": "attachment; filename=" + fname},
            )
        data = _daily_report_rows(request, span)
        import csv
        import io
        buf = io.StringIO()
        buf.write("\ufeff")  # Excel UTF-8 BOM
        w = csv.writer(buf)
        w.writerow(["date", "new_contacts", "leads", "conversions",
                    "frt_count", "frt_responded", "frt_avg_sec",
                    "frt_attain_rate_pct", "resolved", "resolution_avg_sec"])
        tot = {"new_contacts": 0, "leads": 0, "conversions": 0, "frt_count": 0,
               "frt_responded": 0, "resolved": 0, "frt_sum": 0, "res_sum": 0,
               "attain": 0}
        for r in data:
            w.writerow([r["date"], r["new_contacts"], r["leads"], r["conversions"],
                        r["frt_count"], r["frt_responded"], r["frt_avg_sec"],
                        r["frt_attain_rate"], r["resolved"], r["resolution_avg_sec"]])
            tot["new_contacts"] += r["new_contacts"]
            tot["leads"] += r["leads"]
            tot["conversions"] += r["conversions"]
            tot["frt_count"] += r["frt_count"]
            tot["frt_responded"] += r["frt_responded"]
            tot["resolved"] += r["resolved"]
            tot["frt_sum"] += r["frt_avg_sec"] * r["frt_responded"]
            tot["res_sum"] += r["resolution_avg_sec"] * r["resolved"]
            tot["attain"] += round(r["frt_attain_rate"] / 100 * r["frt_responded"])
        frt_avg = int(tot["frt_sum"] / tot["frt_responded"]) if tot["frt_responded"] else 0
        res_avg = int(tot["res_sum"] / tot["resolved"]) if tot["resolved"] else 0
        attain = round(tot["attain"] / tot["frt_responded"] * 100, 1) if tot["frt_responded"] else 0.0
        w.writerow(["合计", tot["new_contacts"], tot["leads"], tot["conversions"],
                    tot["frt_count"], tot["frt_responded"], frt_avg, attain,
                    tot["resolved"], res_avg])
        fname = "daily-report-%dd-%s.csv" % (
            span, time.strftime("%Y%m%d", time.localtime()))
        return Response(
            content=buf.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=" + fname},
        )

    @app.get("/api/workspace/dashboard")
    async def api_workspace_dashboard(request: Request, days: int = 7):
        """工作台仪表盘：今日会话/留资/引流 + 到期跟进 + 坐席负载 + 趋势 + SLA + 首响。"""
        api_auth(request)
        store = _contacts_store(request)
        agent = _session_agent(request)
        sla = _sla_cfg(request)
        span = 30 if int(days or 7) >= 30 else 7
        now = int(time.time())
        lt = time.localtime(now)
        midnight = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))
        since = midnight - (span - 1) * 86400
        out: Dict[str, Any] = {"ok": True, "today": {}, "agent_load": [],
                               "funnel": {}, "trend": [], "sla": {}, "days": span,
                               "first_response": {}, "sla_by_agent": [],
                               "agent_frt": [], "resolution": {}, "res_trend": []}
        if store is not None:
            try:
                ev = store.count_events_since_multi(
                    ["lead_captured", "handoff_sent"], midnight)
                out["today"] = {
                    "new_contacts": store.count_contacts_created_since(midnight),
                    "leads": ev.get("lead_captured", 0),
                    "handoffs": ev.get("handoff_sent", 0),
                }
                out["due_tasks"] = store.count_due_tasks()
                out["due_tasks_mine"] = store.count_due_tasks(assignee=agent["agent_id"])
                out["agent_load"] = store.agent_task_load()
                out["stage_counts"] = store.count_journeys_by_stage()
                # N 日趋势（按本地日期）：新客户 / 留资 / 引流(转化)
                by_new = store.count_contacts_by_day(since)
                by_lead = store.count_events_by_day("lead_captured", since)
                by_conv = store.count_events_by_day("handoff_sent", since)
                trend = []
                for i in range(span):
                    day_ts = since + i * 86400
                    key = time.strftime("%Y-%m-%d", time.localtime(day_ts))
                    trend.append({"day": key[5:], "new_contacts": by_new.get(key, 0),
                                  "leads": by_lead.get(key, 0),
                                  "conversions": by_conv.get(key, 0)})
                out["trend"] = trend
                # 解决(引流)时长：首条 msg_in → handoff_sent（按解决日聚合）
                res_per_day: Dict[str, Dict[str, float]] = {}
                r_sum = r_cnt = 0
                for rr in store.resolution_stats(since):
                    if rr["resolved_ts"] is None:
                        continue
                    dur = max(0, rr["resolved_ts"] - rr["t_in"])
                    rday = time.strftime("%Y-%m-%d", time.localtime(rr["resolved_ts"]))
                    pd = res_per_day.setdefault(rday, {"sum": 0.0, "n": 0})
                    pd["sum"] += dur
                    pd["n"] += 1
                    if rr["resolved_ts"] >= midnight:
                        r_sum += dur
                        r_cnt += 1
                out["resolution"] = {
                    "today_resolved": r_cnt,
                    "today_avg_sec": int(r_sum / r_cnt) if r_cnt else 0}
                res_trend = []
                for i in range(span):
                    day_ts = since + i * 86400
                    key = time.strftime("%Y-%m-%d", time.localtime(day_ts))
                    pd = res_per_day.get(key)
                    res_trend.append({
                        "day": key[5:],
                        "avg_min": round(pd["sum"] / pd["n"] / 60, 1) if pd and pd["n"] else 0,
                        "count": pd["n"] if pd else 0})
                out["res_trend"] = res_trend
            except Exception:
                logger.debug("dashboard 统计失败（已忽略）", exc_info=True)
        # SLA + 首响：均基于 inbox 消息
        try:
            inbox = _inbox_store(request)
            if inbox is not None:
                # 当前等待回复（末条入站）+ 分级
                cids = [c["conversation_id"] for c in inbox.list_conversations(limit=500)]
                dirs = inbox.last_message_dirs(cids)
                # 活跃 claim → 会话归属坐席（lease 有效，可靠；过期已 purge）
                claim_map: Dict[str, Dict[str, str]] = {}
                try:
                    for cl in inbox.list_conversation_claims():
                        claim_map[str(cl.get("conversation_id") or "")] = {
                            "agent_id": str(cl.get("agent_id") or ""),
                            "agent_name": str(cl.get("agent_name") or ""),
                        }
                except Exception:
                    logger.debug("dashboard claim 读取失败（已忽略）", exc_info=True)
                waiting = breaching = critical = 0
                by_agent: Dict[str, Dict[str, Any]] = {}
                for cid, v in dirs.items():
                    if v.get("direction") != "in":
                        continue
                    waiting += 1
                    wait = now - (v.get("ts") or now)
                    is_warn = wait >= sla["warn"]
                    is_crit = wait >= sla["crit"]
                    if is_crit:
                        critical += 1
                    if is_warn:
                        breaching += 1
                    cl = claim_map.get(cid)
                    akey = cl["agent_id"] if cl and cl["agent_id"] else ""
                    bucket = by_agent.get(akey)
                    if bucket is None:
                        bucket = {"agent_id": akey,
                                  "agent_name": (cl["agent_name"] if cl else "")
                                  or akey or "(未认领)",
                                  "waiting": 0, "breaching": 0, "critical": 0}
                        by_agent[akey] = bucket
                    bucket["waiting"] += 1
                    if is_warn:
                        bucket["breaching"] += 1
                    if is_crit:
                        bucket["critical"] += 1
                out["sla"] = {"waiting": waiting, "breaching": breaching,
                              "critical": critical, "warn_sec": sla["warn"],
                              "crit_sec": sla["crit"]}
                out["sla_by_agent"] = sorted(
                    by_agent.values(),
                    key=lambda x: (-x["critical"], -x["breaching"], -x["waiting"]))
                # 首响：窗口内首次进线的会话，首条入站→首条其后出站
                rows = inbox.first_response_rows(since)
                per_day: Dict[str, Dict[str, float]] = {}
                t_sum = t_cnt = t_attain = t_resp = 0
                for r in rows:
                    day = time.strftime("%Y-%m-%d", time.localtime(r["t_in"]))
                    d = per_day.setdefault(day, {"n": 0, "resp": 0, "sum": 0.0,
                                                 "attain": 0})
                    d["n"] += 1
                    if r["t_out"] is not None:
                        frt = max(0.0, r["t_out"] - r["t_in"])
                        d["resp"] += 1
                        d["sum"] += frt
                        if frt <= sla["warn"]:
                            d["attain"] += 1
                    if r["t_in"] >= midnight:
                        t_cnt += 1
                        if r["t_out"] is not None:
                            frt = max(0.0, r["t_out"] - r["t_in"])
                            t_resp += 1
                            t_sum += frt
                            if frt <= sla["warn"]:
                                t_attain += 1
                out["first_response"] = {
                    "today_count": t_cnt,
                    "today_responded": t_resp,
                    "today_avg_sec": int(t_sum / t_resp) if t_resp else 0,
                    "today_attain_rate": round(t_attain / t_resp * 100, 1) if t_resp else 0.0,
                }
                # 首响达标率趋势（与 trend 对齐 day 维度）
                frt_trend = []
                for i in range(span):
                    day_ts = since + i * 86400
                    key = time.strftime("%Y-%m-%d", time.localtime(day_ts))
                    d = per_day.get(key)
                    rate = round(d["attain"] / d["resp"] * 100, 1) if d and d["resp"] else 0.0
                    frt_trend.append({"day": key[5:], "rate": rate,
                                      "count": d["n"] if d else 0})
                out["frt_trend"] = frt_trend
                # 坐席首响绩效（基于 agent_sends 归属，窗口内）
                ag: Dict[str, Dict[str, Any]] = {}
                for r in inbox.agent_first_responses(since):
                    if r["resp_ts"] is None or not r["agent_id"]:
                        continue
                    frt = max(0.0, r["resp_ts"] - r["t_in"])
                    a = ag.get(r["agent_id"])
                    if a is None:
                        a = {"agent_id": r["agent_id"],
                             "agent_name": r["agent_name"] or r["agent_id"],
                             "responded": 0, "_sum": 0.0, "attain": 0}
                        ag[r["agent_id"]] = a
                    a["responded"] += 1
                    a["_sum"] += frt
                    if frt <= sla["warn"]:
                        a["attain"] += 1
                agent_frt = []
                for a in ag.values():
                    n = a["responded"]
                    agent_frt.append({
                        "agent_id": a["agent_id"], "agent_name": a["agent_name"],
                        "responded": n,
                        "avg_sec": int(a["_sum"] / n) if n else 0,
                        "attain_rate": round(a["attain"] / n * 100, 1) if n else 0.0})
                agent_frt.sort(key=lambda x: -x["responded"])
                out["agent_frt"] = agent_frt
        except Exception:
            logger.debug("dashboard SLA/首响 统计失败（已忽略）", exc_info=True)
        try:
            from src.workspace.agent_coordinator import web_funnel_snapshot
            out["funnel"] = web_funnel_snapshot(request, config_manager)
        except Exception:
            out["funnel"] = {}
        # P3：跨语言翻译漏斗（覆盖率/auto 失败率/按客户语言分布 + 趋势；供经理看板）。
        # 优先读「按日」持久化聚合（跨重启、响应 7/30 日选择器、含趋势线），与其它面板同源；
        # inbox store 不可用时回退进程内累计快照（不含趋势）。
        try:
            inbox_x = _inbox_store(request)
            if inbox_x is not None and hasattr(inbox_x, "get_outbound_xlate_stats"):
                out["translation"] = inbox_x.get_outbound_xlate_stats(since)
            else:
                from src.ai.outbound_translation_stats import get_outbound_translation_stats
                out["translation"] = get_outbound_translation_stats().dump()
        except Exception:
            out["translation"] = {}
        # P3：入站翻译漏斗（客户→坐席自动翻译；客户来源语言分布），与出向合成跨语言总览
        try:
            inbox_in = _inbox_store(request)
            if inbox_in is not None and hasattr(inbox_in, "get_inbound_xlate_stats"):
                out["translation_inbound"] = inbox_in.get_inbound_xlate_stats(since)
            else:
                out["translation_inbound"] = {}
        except Exception:
            out["translation_inbound"] = {}
        # P3：自动派单（AutoClaimWorker）按日聚合（派单量/语言命中/命中语言分布 + 趋势）。
        # 与翻译漏斗同源「按日」表，响应日窗选择器、跨重启可回溯（status_snapshot 仅进程累计）。
        try:
            inbox_ac = _inbox_store(request)
            if inbox_ac is not None and hasattr(inbox_ac, "get_auto_claim_stats"):
                out["auto_claim"] = inbox_ac.get_auto_claim_stats(since)
            else:
                out["auto_claim"] = {}
        except Exception:
            out["auto_claim"] = {}
        return out
