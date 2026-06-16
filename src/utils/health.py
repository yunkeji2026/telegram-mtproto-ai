"""D1 运行时健康聚合（区别于 P2-1 上线就绪清单）。

上线清单（golive）回答「能不能开张」（一次性配置就绪）；本模块回答「现在跑得健不健康」
（运行时存活/积压/熔断）。把 DB 连通、AI 配置、授权状态、渠道在线、后台 worker 存活、
草稿队列积压聚合成一张运行时红绿灯。

:func:`build_health` 为**纯函数**（入参已是采集好的轻量信号），便于单测；I/O 采集留给路由层。
状态三态：``ok`` / ``warn`` / ``fail``；总体灯：任一 fail→red，无 fail 有 warn→yellow，全 ok→green。
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

_PLACEHOLDER = ("your_", "<", "changeme", "xxxx", "请填写", "填写")


def is_placeholder(val: Any) -> bool:
    s = str(val if val is not None else "").strip()
    if not s:
        return True
    low = s.lower()
    return any(t in low for t in _PLACEHOLDER)


def _comp(id_: str, name: str, status: str, detail: str) -> Dict[str, Any]:
    return {"id": id_, "name": name, "status": status, "detail": detail}


def build_health(
    *,
    db_ok: bool,
    ai_provider: str = "",
    ai_key_ok: bool = False,
    license_state: str = "",
    license_read_only: bool = False,
    license_plan: str = "",
    channels_ready: int = 0,
    channels_configured: int = 0,
    channels_total: int = 0,
    workers: Optional[List[Dict[str, Any]]] = None,
    pending_drafts: Optional[int] = None,
    pending_threshold: int = 200,
) -> Dict[str, Any]:
    """聚合运行时健康（纯函数）。返回 {ok, light, components, summary, ts}。"""
    comps: List[Dict[str, Any]] = []

    # 1) 数据库连通——硬性（挂了整站不可用）
    if db_ok:
        comps.append(_comp("db", "数据库连通", "ok", "SELECT 1 正常"))
    else:
        comps.append(_comp("db", "数据库连通", "fail", "无法访问持久层"))

    # 2) AI 大模型可用——硬性（核心能力）
    if ai_provider and ai_key_ok:
        comps.append(_comp("ai", "AI 大模型", "ok", f"provider={ai_provider}"))
    elif ai_provider:
        comps.append(_comp("ai", "AI 大模型", "fail", "api_key 为空/占位"))
    else:
        comps.append(_comp("ai", "AI 大模型", "fail", "未配置 provider"))

    # 3) 授权状态——expired/invalid 且只读→fail；过期未强制→warn；其余 ok
    st = str(license_state or "")
    if st in ("active", "grace", "unlicensed", "unavailable", "community", ""):
        detail = f"plan={license_plan or 'community'}（{st or 'community'}）"
        comps.append(_comp("license", "授权状态", "ok", detail))
    elif license_read_only:
        comps.append(_comp("license", "授权状态", "fail",
                           f"授权 {st}，系统已降级只读"))
    else:
        comps.append(_comp("license", "授权状态", "warn", f"授权 {st}，请尽快续期"))

    # 4) 渠道在线——软性（纯 web chat / AI 也能跑）
    if channels_ready > 0:
        comps.append(_comp("channels", "消息渠道", "ok",
                           f"{channels_ready}/{channels_total} 渠道就绪"))
    elif channels_configured > 0:
        comps.append(_comp("channels", "消息渠道", "warn", "已配置但无渠道就绪/登录"))
    else:
        comps.append(_comp("channels", "消息渠道", "warn", "尚无渠道接入"))

    # 5) 后台 worker 存活——已挂载但未运行→fail；熔断→warn；运行→ok
    for w in (workers or []):
        name = str(w.get("name") or w.get("id") or "worker")
        wid = str(w.get("id") or name)
        if not w.get("present"):
            continue  # 未启用的 worker 不计入健康
        if not w.get("running"):
            comps.append(_comp(f"worker_{wid}", name, "fail", "已挂载但未运行"))
        elif w.get("circuit_open"):
            le = str(w.get("last_error") or "")
            comps.append(_comp(f"worker_{wid}", name, "warn",
                               f"熔断中{('：' + le) if le else ''}"))
        else:
            comps.append(_comp(f"worker_{wid}", name, "ok", "运行中"))

    # 6) 草稿队列积压——超阈值→warn（提示处理不过来/worker 卡顿）
    if pending_drafts is not None:
        if pending_drafts > pending_threshold:
            comps.append(_comp("queue", "草稿队列", "warn",
                               f"待处理 {pending_drafts} 条（> {pending_threshold}）"))
        else:
            comps.append(_comp("queue", "草稿队列", "ok",
                               f"待处理 {pending_drafts} 条"))

    fails = sum(1 for c in comps if c["status"] == "fail")
    warns = sum(1 for c in comps if c["status"] == "warn")
    oks = sum(1 for c in comps if c["status"] == "ok")
    light = "red" if fails else ("yellow" if warns else "green")
    return {
        "ok": True,
        "light": light,
        "healthy": fails == 0,
        "components": comps,
        "summary": {"ok": oks, "warn": warns, "fail": fails, "total": len(comps)},
        "ts": time.time(),
    }
