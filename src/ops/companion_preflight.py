"""Phase N：真号扫码陪聊「上线前」就绪自检（纯函数）。

把 `docs/N_LINE_REAL_ACCOUNT_CHECKLIST.md` 的开关一致性（§1）+ 反封号护栏就绪（§4）
固化成一张红绿灯——operator **扫码前**就能看到「现在能不能拉起真号陪聊」，而非启动后翻
日志找 WARN。与 `golive.build_checklist` 同形（{ok,light,ready,checks,summary}），复用前端。

纯函数：入参是 config dict（路由层采集）。状态三态 ok/warn/fail；
总体灯：任一 fail→red；无 fail 有 warn→yellow；全 ok→green。

设计要点（以 N-Line checklist 为准）：
- companion_runtime=false → N 线扫码陪聊**未启用**：返回单条 info（ready=True，不拦上线）。
- companion_runtime=true 但 orchestrator/protocol 没开 → **fail**（协议号根本拉不起来）。
- 反封号闸门关 / 无代理 → **warn**（命门，建议跑通收发后开）。
"""

from __future__ import annotations

from typing import Any, Dict, List

_PLACEHOLDER = ("your_", "<", "changeme", "xxxx", "请填写", "填写")


def _is_placeholder(val: Any) -> bool:
    s = str(val if val is not None else "").strip()
    if not s:
        return True
    low = s.lower()
    return any(t in low for t in _PLACEHOLDER)


def _check(id_: str, name: str, status: str, detail: str,
           action_url: str = "", action_label: str = "") -> Dict[str, Any]:
    return {"id": id_, "name": name, "status": status, "detail": detail,
            "action_url": action_url, "action_label": action_label}


def _result(checks: List[Dict[str, Any]], *, applicable: bool) -> Dict[str, Any]:
    fails = sum(1 for c in checks if c["status"] == "fail")
    warns = sum(1 for c in checks if c["status"] == "warn")
    oks = sum(1 for c in checks if c["status"] == "ok")
    light = "red" if fails else ("yellow" if warns else "green")
    return {
        "ok": True,
        "applicable": applicable,
        "light": light,
        "ready": fails == 0,
        "checks": checks,
        "summary": {"ok": oks, "warn": warns, "fail": fails, "total": len(checks)},
    }


def build_companion_preflight(config: Dict[str, Any]) -> Dict[str, Any]:
    """真号扫码陪聊上线前自检（纯函数）。返回 {ok,applicable,light,ready,checks,summary}。"""
    config = config or {}
    tg = (config.get("telegram") or {})
    pl = (config.get("platform_login") or {})
    pl_tg = (pl.get("telegram") or {})

    companion_runtime = bool(pl_tg.get("companion_runtime", False))
    orchestrator_enabled = bool(pl.get("orchestrator_enabled", False))
    protocol_enabled = bool(pl_tg.get("protocol_enabled", False))

    # N 线未启用 → 不适用，单条 info，不拦上线
    if not companion_runtime:
        return _result([_check(
            "companion_runtime", "N 线扫码陪聊", "ok",
            "未启用（platform_login.telegram.companion_runtime=false）；"
            "协议号走 B 线薄连接 worker，无 A 线人设/记忆/情绪",
        )], applicable=False)

    checks: List[Dict[str, Any]] = []

    # 1) Telegram 应用凭证（扫码登录命门）——硬性
    if _is_placeholder(tg.get("api_id")) or _is_placeholder(tg.get("api_hash")):
        checks.append(_check(
            "tg_credentials", "Telegram 凭证（api_id/api_hash）", "fail",
            "api_id / api_hash 为空或占位——无法扫码登录",
            "/workspace/setup", "去配置"))
    else:
        checks.append(_check(
            "tg_credentials", "Telegram 凭证（api_id/api_hash）", "ok", "已配置"))

    # 2) 编排器开启（否则协议号不会被拉起）——硬性
    if orchestrator_enabled:
        checks.append(_check("orchestrator", "编排器已开启", "ok",
                             "platform_login.orchestrator_enabled=true"))
    else:
        checks.append(_check(
            "orchestrator", "编排器已开启", "fail",
            "companion_runtime 开了但 orchestrator_enabled 关——协议号不会被拉起",
            "/workspace/setup", "去开启"))

    # 3) 协议登录 provider 注册（扫码登录前提）——硬性
    if protocol_enabled:
        checks.append(_check("protocol", "扫码登录 provider 已注册", "ok",
                             "platform_login.telegram.protocol_enabled=true"))
    else:
        checks.append(_check(
            "protocol", "扫码登录 provider 已注册", "fail",
            "companion_runtime 开了但 protocol_enabled 关——扫码登录 provider 未注册",
            "/workspace/setup", "去开启"))

    # 4) 反封号闸门（命门，建议跑通收发后开）——软性
    gate = (config.get("companion_send_gate") or {})
    if gate.get("enabled", False):
        checks.append(_check("send_gate", "反封号闸门已开", "ok",
                             "companion_send_gate.enabled=true（预热爬坡 + 健康评分生效）"))
    else:
        checks.append(_check(
            "send_gate", "反封号闸门已开", "warn",
            "companion_send_gate.enabled=false——可先跑通收发再开；真号长跑务必开（反封号命门）"))

    # 5) 每号独立代理（反封号强烈建议）——软性
    proxy_pool = config.get("proxy_pool") or tg.get("proxy_pool") or []
    if proxy_pool:
        n = len(proxy_pool) if isinstance(proxy_pool, (list, tuple)) else 1
        checks.append(_check("proxy", "代理池", "ok", f"已配置 {n} 个代理"))
    else:
        checks.append(_check(
            "proxy", "代理池", "warn",
            "未配置代理——真号长跑强烈建议每号独立 socks5/http 代理（反封号命门）"))

    return _result(checks, applicable=True)


__all__ = ["build_companion_preflight"]
