"""统一收件箱——经营报表/坐席绩效逐日计算层（巨石拆分 slice 16a 基座）。

把被 ``daily-report.csv`` 与经理 ``dashboard`` **共用**的两个逐日聚合闭包 helper
（``_daily_report_rows`` / ``_agent_daily_report_rows``）从 ``register_unified_inbox_routes``
巨型闭包中**下沉为模块级纯函数**，为 slice 16b 路由外移做前置准备：路由域搬家时其依赖的
计算层已是模块级，零闭包牵连。

依赖全部朝下：sla 配置（unified_inbox_sla._sla_cfg）、services 存储；纯计算、入参 request，
无副作用、无回 routes 依赖。函数签名/返回结构与原闭包逐字节一致。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List

from fastapi import Request

from src.web.routes.unified_inbox_services import _contacts_store, _inbox_store
from src.web.routes.unified_inbox_sla import _sla_cfg

logger = logging.getLogger(__name__)


def _daily_report_rows(request: Request, span: int) -> List[Dict[str, Any]]:
    """逐日经营指标表（坐席日报/导出共用）。

    每日一行：新客/留资/引流(转化) + 首响(条数/已响应/均值/达标率) +
    解决(引流)时长(解决数/均值)。窗口 = 今天回溯 span 天，按本地日期。
    """
    sla = _sla_cfg(request)
    now = int(time.time())
    lt = time.localtime(now)
    midnight = int(time.mktime(
        (lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))
    since = midnight - (span - 1) * 86400
    days_keys = [time.strftime("%Y-%m-%d", time.localtime(since + i * 86400))
                 for i in range(span)]
    rows: Dict[str, Dict[str, Any]] = {
        k: {"date": k, "new_contacts": 0, "leads": 0, "conversions": 0,
            "frt_count": 0, "frt_responded": 0, "frt_avg_sec": 0,
            "frt_attain_rate": 0.0, "resolved": 0, "resolution_avg_sec": 0}
        for k in days_keys}
    store = _contacts_store(request)
    if store is not None:
        try:
            by_new = store.count_contacts_by_day(since)
            by_lead = store.count_events_by_day("lead_captured", since)
            by_conv = store.count_events_by_day("handoff_sent", since)
            for k in days_keys:
                rows[k]["new_contacts"] = by_new.get(k, 0)
                rows[k]["leads"] = by_lead.get(k, 0)
                rows[k]["conversions"] = by_conv.get(k, 0)
            res_acc: Dict[str, List[float]] = {}
            for rr in store.resolution_stats(since):
                if rr["resolved_ts"] is None:
                    continue
                rday = time.strftime("%Y-%m-%d",
                                     time.localtime(rr["resolved_ts"]))
                acc = res_acc.setdefault(rday, [0.0, 0.0])
                acc[0] += max(0, rr["resolved_ts"] - rr["t_in"])
                acc[1] += 1
            for k, (s, n) in res_acc.items():
                if k in rows and n:
                    rows[k]["resolved"] = int(n)
                    rows[k]["resolution_avg_sec"] = int(s / n)
        except Exception:
            logger.debug("daily-report contacts 统计失败（已忽略）", exc_info=True)
    inbox = _inbox_store(request)
    if inbox is not None:
        try:
            fr_acc: Dict[str, List[float]] = {}
            for r in inbox.first_response_rows(since):
                day = time.strftime("%Y-%m-%d", time.localtime(r["t_in"]))
                acc = fr_acc.setdefault(day, [0.0, 0.0, 0.0, 0.0])  # n,resp,sum,attain
                acc[0] += 1
                if r["t_out"] is not None:
                    frt = max(0.0, r["t_out"] - r["t_in"])
                    acc[1] += 1
                    acc[2] += frt
                    if frt <= sla["warn"]:
                        acc[3] += 1
            for k, (n, resp, s, att) in fr_acc.items():
                if k not in rows:
                    continue
                rows[k]["frt_count"] = int(n)
                rows[k]["frt_responded"] = int(resp)
                rows[k]["frt_avg_sec"] = int(s / resp) if resp else 0
                rows[k]["frt_attain_rate"] = round(att / resp * 100, 1) if resp else 0.0
        except Exception:
            logger.debug("daily-report inbox 统计失败（已忽略）", exc_info=True)
    return [rows[k] for k in days_keys]


def _agent_daily_report_rows(
    request: Request, span: int, agent: str,
) -> List[Dict[str, Any]]:
    """某坐席逐日个人绩效：首响数/均值/达标率 + 发送量 + 完成任务数。

    首响按"响应日(resp_ts)"归属（即坐席当日实际动作）；frt=resp_ts-t_in。
    """
    sla = _sla_cfg(request)
    now = int(time.time())
    lt = time.localtime(now)
    midnight = int(time.mktime(
        (lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))
    since = midnight - (span - 1) * 86400
    days_keys = [time.strftime("%Y-%m-%d", time.localtime(since + i * 86400))
                 for i in range(span)]
    rows: Dict[str, Dict[str, Any]] = {
        k: {"date": k, "first_responded": 0, "frt_avg_sec": 0,
            "frt_attain_rate": 0.0, "sends": 0, "tasks_done": 0}
        for k in days_keys}
    inbox = _inbox_store(request)
    if inbox is not None:
        try:
            fr_acc: Dict[str, List[float]] = {}
            for r in inbox.agent_first_responses(since):
                if r["resp_ts"] is None or r["agent_id"] != agent:
                    continue
                day = time.strftime("%Y-%m-%d", time.localtime(r["resp_ts"]))
                acc = fr_acc.setdefault(day, [0.0, 0.0, 0.0])  # n, sum, attain
                frt = max(0.0, r["resp_ts"] - r["t_in"])
                acc[0] += 1
                acc[1] += frt
                if frt <= sla["warn"]:
                    acc[2] += 1
            for k, (n, s, att) in fr_acc.items():
                if k not in rows:
                    continue
                rows[k]["first_responded"] = int(n)
                rows[k]["frt_avg_sec"] = int(s / n) if n else 0
                rows[k]["frt_attain_rate"] = round(att / n * 100, 1) if n else 0.0
            for k, n in inbox.count_agent_sends_by_day(agent, since).items():
                if k in rows:
                    rows[k]["sends"] = int(n)
        except Exception:
            logger.debug("agent daily-report inbox 统计失败（已忽略）", exc_info=True)
    store = _contacts_store(request)
    if store is not None:
        try:
            for k, n in store.count_tasks_done_by_day(agent, since).items():
                if k in rows:
                    rows[k]["tasks_done"] = int(n)
        except Exception:
            logger.debug("agent daily-report tasks 统计失败（已忽略）", exc_info=True)
    return [rows[k] for k in days_keys]
