"""全自动发送「安全视图」聚合器（纯函数 core + 薄 gather 适配，只读零副作用）。

回答运营在真号自动发时最该盯的四问，每号一行：
1. **发了多少 / 占 cap 多少**：sends_today（SendCountStore 权威计数，跨重启存活）÷ 预热建议上限。
2. **发得成不成**：delivered / failed（draft_audit_log 的 autosend / autosend_failed），失败率。
3. **为什么失败**：把 autosend_failed 的 reason 归因为 闸门(kill_switch/canary/send_gate) /
   永久性(无发言权/被拉黑/会话失效) / 平台错误 / 其它——闸门拦截 ≠ 平台报错，处置完全不同。
4. **客户回不回**：可选 reply（对今日被自动发过的会话，之后是否有客户入站）——AI 是否真在work。

设计：core ``compute_send_health`` 吃**已取好的原语**（账号信号 + 24h 审计行 + 可选回复数），
零 IO、可单测；``gather_send_health`` 从真实 store/registry/limiter 取数喂 core。CLI
``scripts/send_health_report.py`` 与路由 ``/api/accounts/send-health`` 共用同一 core。
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.skills.account_health import account_health

_DAY = 86400.0

# autosend_failed reason → 失败类别（处置口径不同：闸门=预期节流，平台=真故障）
FAIL_GATE = "gate"            # 被反封号闸门/急停/金丝雀拦（预期内的安全节流，非故障）
FAIL_PERMANENT = "permanent"  # 无发言权/被拉黑/会话失效（该会话短期发不出）
FAIL_PLATFORM = "platform"    # 平台投递报错（网络/服务 500/协议异常等真故障）
FAIL_OTHER = "other"


def classify_fail_reason(reason: str) -> str:
    """把 autosend_failed 的 reason 文本归因到失败类别（纯函数）。"""
    r = str(reason or "").lower()
    if any(k in r for k in ("kill_switch", "canary", "send_gate", "circuit_open",
                            "quota_hour", "quota_day", "warmup_cap", "health_red", "banned")):
        return FAIL_GATE
    if any(k in r for k in ("无发言权", "被拉黑", "会话失效", "peer", "blocked by",
                            "userisblocked", "chat not found", "forbidden")):
        return FAIL_PERMANENT
    if any(k in r for k in ("投递失败", "发送失败", "500", "502", "timeout",
                            "connection", "network", "unavailable")):
        return FAIL_PLATFORM
    return FAIL_OTHER


def account_from_conv(conversation_id: str) -> Tuple[str, str]:
    """从 ``platform:account:chat`` 解析 (platform, account_id)；异常 → ("","")。"""
    parts = str(conversation_id or "").split(":")
    if len(parts) >= 2:
        return parts[0].strip().lower(), parts[1].strip()
    return "", ""


def is_sender_account(status: str) -> bool:
    """该账号是否属于「live 发送面」（值得进安全视图）。

    排除 ``removed``（已从系统移除/封禁下线——不再发送，列出来只会用假红灯污染队列级别，
    让真正在发的号被淹没）。其余（online/active/pending/warming/offline）都保留：pending/
    offline 的号虽当前不发但仍是 live 账号，0 活动行低噪声、便于一眼看全机群。
    """
    return str(status or "").strip().lower() != "removed"


def _verdict(*, light: str, sends: int, cap: int, fail_rate: float,
             platform_fails: int) -> Tuple[str, str]:
    """单号综合判词 (level, reason)。level ∈ ok|watch|risk（供 UI 颜色/排序）。"""
    if light == "red":
        return "risk", "健康红灯：账号受限/封禁或风控信号严重，应停发核查"
    if platform_fails > 0 and fail_rate >= 0.3:
        return "risk", f"投递失败率 {fail_rate:.0%}（含 {platform_fails} 次平台报错），排查通道"
    cap_pct = (sends / cap) if cap > 0 else 0.0
    if cap_pct >= 1.0:
        return "risk", f"今日已发 {sends} 达/超上限 {cap}，闸门将开始拦截后续"
    if cap_pct >= 0.8:
        return "watch", f"今日已发 {sends}，接近上限 {cap}（{cap_pct:.0%}）"
    if light == "amber":
        return "watch", "健康黄灯：有风控信号（预热期/失败/无独立代理），留意"
    if fail_rate >= 0.3 and (sends > 0):
        return "watch", f"投递失败率 {fail_rate:.0%}，留意"
    return "ok", "安全区内"


def compute_send_health(
    *,
    accounts: List[Dict[str, Any]],
    audit_24h: List[Dict[str, Any]],
    config: Optional[Dict[str, Any]] = None,
    reply_by_account: Optional[Dict[str, Dict[str, int]]] = None,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """聚合每号自动发送安全视图（纯函数）。

    accounts: 每号信号 dict（build_account_signals 输出 + platform/status），需含
        ``platform, account_id`` + 可选 ``age_days/proxy_bound/banned/sends_today``。
    audit_24h: 近 24h draft_audit_log 行 [{action, conversation_id, reason}]。
    reply_by_account: 可选 {account_key: {autosent_convs, replied_convs}}（回复率，best-effort）。
    """
    now = float(now if now is not None else time.time())
    gc = (config or {}).get("companion_send_gate") or {}
    caps = dict(
        target_cap=int(gc.get("target_cap", 15) or 15),
        warmup_start_cap=int(gc.get("warmup_start_cap", 2) or 2),
        warmup_ramp_days=int(gc.get("warmup_ramp_days", 14) or 14),
    )

    # 按账号预聚合审计（delivered / failed / 失败类别）
    agg: Dict[str, Dict[str, Any]] = {}

    def _slot(key: str) -> Dict[str, Any]:
        return agg.setdefault(key, {
            "delivered": 0, "failed": 0,
            "fail_by_cat": {FAIL_GATE: 0, FAIL_PERMANENT: 0, FAIL_PLATFORM: 0, FAIL_OTHER: 0},
        })

    for row in audit_24h or []:
        action = str(row.get("action") or "")
        plat, acc = account_from_conv(row.get("conversation_id"))
        if not acc:
            continue
        key = f"{plat}:{acc}"
        if action == "autosend":
            _slot(key)["delivered"] += 1
        elif action == "autosend_failed":
            s = _slot(key)
            s["failed"] += 1
            s["fail_by_cat"][classify_fail_reason(row.get("reason"))] += 1

    out_accounts: List[Dict[str, Any]] = []
    summary = {"ok": 0, "watch": 0, "risk": 0,
               "sends_today": 0, "delivered_24h": 0, "failed_24h": 0}

    for sig in accounts or []:
        plat = str(sig.get("platform") or "").lower()
        acc = str(sig.get("account_id") or "")
        if not acc:
            continue
        key = f"{plat}:{acc}"
        health = account_health(sig, **caps)
        a = agg.get(key, {"delivered": 0, "failed": 0,
                          "fail_by_cat": {FAIL_GATE: 0, FAIL_PERMANENT: 0,
                                          FAIL_PLATFORM: 0, FAIL_OTHER: 0}})
        sends = int(sig.get("sends_today") or 0)
        cap = int(health["recommended_cap"])
        delivered, failed = a["delivered"], a["failed"]
        # 失败率只算「真故障」（平台/永久/其它），**闸门拦截不计**——它是预期节流不是投递失败，
        # 也不进分母（不是真发尝试）。否则养号期大量 warmup_cap hold 会把失败率虚高成 risk。
        gate_held = a["fail_by_cat"][FAIL_GATE]
        real_failed = failed - gate_held
        real_attempts = delivered + real_failed
        fail_rate = (real_failed / real_attempts) if real_attempts else 0.0
        platform_fails = a["fail_by_cat"][FAIL_PLATFORM]
        level, why = _verdict(light=health["light"], sends=sends, cap=cap,
                              fail_rate=fail_rate, platform_fails=platform_fails)

        rec = reply_by_account.get(key) if reply_by_account else None
        reply_view = None
        if rec and int(rec.get("autosent_convs") or 0) > 0:
            ac, rc = int(rec["autosent_convs"]), int(rec.get("replied_convs") or 0)
            reply_view = {"autosent_convs": ac, "replied_convs": rc,
                          "reply_rate": round(rc / ac, 3)}

        out_accounts.append({
            "platform": plat, "account_id": acc,
            "level": level, "reason": why,
            "light": health["light"], "score": health["score"],
            "sends_today": sends, "recommended_cap": cap,
            "cap_pct": round(sends / cap, 3) if cap > 0 else None,
            "delivered_24h": delivered, "failed_24h": failed,
            "gate_held_24h": gate_held,          # 闸门拦截次数（预期节流，非故障）
            "fail_rate": round(fail_rate, 3),    # 真故障率（已剔除闸门拦截）
            "fail_by_cat": a["fail_by_cat"],
            "reply": reply_view,
            "health_reasons": health["reasons"],
        })
        summary[level] += 1
        summary["sends_today"] += sends
        summary["delivered_24h"] += delivered
        summary["failed_24h"] += failed

    # risk 最前、watch 次之，同级按今日发量降序（最该看的在上）
    _order = {"risk": 0, "watch": 1, "ok": 2}
    out_accounts.sort(key=lambda x: (_order.get(x["level"], 3), -x["sends_today"]))
    fleet_level = ("risk" if summary["risk"] else
                   "watch" if summary["watch"] else
                   "ok" if out_accounts else "unknown")
    return {
        "generated_at": round(now, 3),
        "fleet_level": fleet_level,
        "summary": summary,
        "accounts": out_accounts,
    }


def gather_send_health(
    *, inbox_store: Any, registry: Any = None, limiter: Any = None,
    config: Optional[Dict[str, Any]] = None, now: Optional[float] = None,
    window_hours: float = 24.0, with_reply: bool = True,
) -> Dict[str, Any]:
    """从真实 store/registry/limiter 取数喂 ``compute_send_health``（薄适配，best-effort）。"""
    now = float(now if now is not None else time.time())
    since = now - max(1.0, float(window_hours)) * 3600.0
    from src.skills.account_signals import build_account_signals

    # 1) 账号清单 + 信号（注册表 + 持久化 limiter 的今日计数）
    accounts: List[Dict[str, Any]] = []
    reg_rows = []
    try:
        reg_rows = registry.list() if registry is not None else []
    except Exception:
        reg_rows = []
    for r in reg_rows:
        plat, acc = r.get("platform"), r.get("account_id")
        if not acc or not is_sender_account(r.get("status", "")):
            continue   # 跳过 removed（已下线/封禁的历史号，列出只会用假红灯污染队列级别）
        sig = build_account_signals(plat, acc, registry=registry, limiter=limiter, now=now)
        sig["platform"] = str(plat or "").lower()
        sig["status"] = r.get("status", "")
        accounts.append(sig)

    # 2) 近 24h 自动发审计行
    audit_24h: List[Dict[str, Any]] = []
    try:
        rows = inbox_store.list_draft_audit(since_ts=since, limit=5000)
        for row in rows or []:
            act = str(row.get("action") or "")
            if act in ("autosend", "autosend_failed"):
                audit_24h.append({"action": act,
                                  "conversation_id": row.get("conversation_id", ""),
                                  "reason": row.get("reason", "")})
    except Exception:
        audit_24h = []

    # 3) 可选回复率（对今日被自动发过的会话，之后是否有客户入站）。
    #    经注入 inbound_exists 回调解耦 store 具体方法：store 有 has_inbound_since 就用，
    #    没有则跳过（best-effort），CLI 侧改注入直连 sqlite 的实现。
    reply_by_account: Optional[Dict[str, Dict[str, int]]] = None
    if with_reply:
        fn = getattr(inbox_store, "has_inbound_since", None)
        if callable(fn):
            try:
                reply_by_account = reply_stats(audit_24h, since, fn)
            except Exception:
                reply_by_account = None

    return compute_send_health(
        accounts=accounts, audit_24h=audit_24h, config=config or {},
        reply_by_account=reply_by_account, now=now,
    )


def reply_stats(
    audit_24h: List[Dict[str, Any]], since: float,
    inbound_exists: Callable[[str, float], bool],
) -> Dict[str, Dict[str, int]]:
    """对今日被 autosend 过的会话，统计「之后是否有客户入站」→ 每号 {autosent_convs, replied_convs}。

    ``inbound_exists(conversation_id, since_ts) -> bool`` 由调用方注入（store 方法或直连
    sqlite 的闭包），本函数纯编排、可单测。
    """
    convs_by_acct: Dict[str, set] = {}
    for row in audit_24h:
        if row.get("action") != "autosend":
            continue
        plat, acc = account_from_conv(row.get("conversation_id"))
        if not acc:
            continue
        convs_by_acct.setdefault(f"{plat}:{acc}", set()).add(row.get("conversation_id"))

    out: Dict[str, Dict[str, int]] = {}
    for key, convs in convs_by_acct.items():
        replied = sum(1 for cid in convs if inbound_exists(cid, since))
        out[key] = {"autosent_convs": len(convs), "replied_convs": replied}
    return out


__all__ = [
    "FAIL_GATE", "FAIL_PERMANENT", "FAIL_PLATFORM", "FAIL_OTHER",
    "classify_fail_reason", "account_from_conv", "is_sender_account",
    "compute_send_health", "gather_send_health", "reply_stats",
]
