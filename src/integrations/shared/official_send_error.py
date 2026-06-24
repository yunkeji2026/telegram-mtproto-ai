"""官方通道发送错误统一分类（确定性纯函数）。

各官方 API（WhatsApp Cloud / Messenger / Instagram / LINE / Zalo）发送失败时返回的错误
**五花八门**（HTTP 状态码 + 各家私有 error.code），此前各 send 助手只把它打包成不透明的
``"HTTP 4xx: ..."`` 字符串就吞掉——结果**客服窗口过期、token 失效、限速**等都长一个样，
回复没送达却**无人知晓、无法分流**。本模块把它们归一成一张**跨平台错误类型表**，让上层
（可观测/转人工/窗口回退）能据 ``kind`` 决策，**一处分类、多处复用**。

设计同 ``src.ops.ban_signal.classify``（封号信号分类）：**纯函数、零网络、可注入假响应单测**。

kind 取值：
- ``window_expired``     ：超出平台「客服会话窗口」（WA 24h / IG·FB 24h / Zalo cs 7d）——
                           自由文本被拒，需模板/标签/人工跟进（**不是封号**）。
- ``invalid_token``      ：access token 失效/过期/权限不足——需重配凭证。
- ``rate_limited``       ：触发限速/配额——退避重试。
- ``recipient_unavailable``：收件人不可达（停用/拉黑/不在实验组）——放弃该条。
- ``unsupported``        ：消息类型/内容不被支持。
- ``transient``          ：5xx/网络抖动——可重试。
- ``ok``                 ：其实成功（status 200 且无 error）。
- ``unknown``            ：未归类（保留原始 reason 供排查）。
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

# ── 各平台 error.code → kind（以各家官方文档常见码为准；未列入的走 HTTP 状态兜底）──

# WhatsApp Cloud API（graph error.code）
_WA_CODES = {
    131047: "window_expired",       # Re-engagement message（距用户上次回复 >24h）
    131051: "unsupported",          # Unsupported message type
    131052: "unsupported",          # Media download error
    131026: "recipient_unavailable",  # Message undeliverable
    130472: "recipient_unavailable",  # User's number is part of an experiment
    131056: "rate_limited",         # (Pair rate limit) Too many messages to this number
    130429: "rate_limited",         # Rate limit hit
    80007:  "rate_limited",         # Rate limit issues
    131048: "rate_limited",         # Spam rate limit hit
    190:    "invalid_token",        # Access token expired/invalid
    0:      "invalid_token",        # AuthException
    10:     "invalid_token",        # Permission denied
    200:    "invalid_token",        # Permissions error
}

# Graph Messaging（Messenger / Instagram）：用 (code, subcode) 或单 code
_GRAPH_SUBCODES = {
    2534022: "window_expired",      # outside allowed window (24h)
    2018278: "window_expired",      # message sent outside of allowed window
    1545041: "recipient_unavailable",
    2018108: "recipient_unavailable",  # cannot message users who are not admins/testers
}
_GRAPH_CODES = {
    10:  "window_expired",          # 常与 subcode 2534022 同现（permission/window）
    190: "invalid_token",
    200: "invalid_token",           # 权限不足
    613: "rate_limited",
    4:   "rate_limited",            # application request limit
    80004: "rate_limited",
    551: "recipient_unavailable",   # user unavailable
    100: "unknown",
}

# Zalo OA OpenAPI v3（body.error；确切码以 Zalo 控制台为准，保守只映已知）
_ZALO_CODES = {
    -32:  "rate_limited",           # exceed message quota
    -213: "window_expired",         # 用户超出 cs 可发窗口/未在 7d 内互动
    -216: "window_expired",
    -230: "recipient_unavailable",
    -201: "invalid_token",
    -204: "invalid_token",
    -211: "invalid_token",
}


def _http_fallback(status: Optional[int]) -> str:
    """无法按平台码归类时，用 HTTP 状态兜底。"""
    if status is None:
        return "unknown"
    try:
        s = int(status)
    except (TypeError, ValueError):
        return "unknown"
    if s in (401, 403):
        return "invalid_token"
    if s == 429:
        return "rate_limited"
    if 500 <= s <= 599:
        return "transient"
    if s == 200:
        return "ok"
    return "unknown"


def _dig_error(body: Any) -> Dict[str, Any]:
    """从响应 body（dict）挖出 {code, subcode, message}；不同家结构不一，尽力而为。"""
    out: Dict[str, Any] = {"code": None, "subcode": None, "message": ""}
    if not isinstance(body, dict):
        return out
    err = body.get("error")
    if isinstance(err, dict):  # Graph / WA 风格：{"error": {"code":..., "error_subcode":..., "message":...}}
        out["code"] = err.get("code")
        out["subcode"] = err.get("error_subcode") or err.get("subcode")
        out["message"] = str(err.get("message") or "")
        return out
    if "error" in body and not isinstance(err, dict):  # Zalo 风格：{"error": -213, "message": "..."}
        out["code"] = err
        out["message"] = str(body.get("message") or "")
        return out
    return out


def _coerce_int(v: Any) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# 错误文本里的窗口关键词（body 拿不到 code 时的最后兜底）
_WINDOW_TEXT = re.compile(
    r"outside of allowed window|outside the allowed window|re-?engagement|"
    r"24 ?hours? have passed|customer care window|messaging window",
    re.IGNORECASE,
)
_TOKEN_TEXT = re.compile(
    r"access token|token (?:has )?expired|invalid token|oauth|unauthorized",
    re.IGNORECASE,
)


def classify_official_send_error(
    platform: str,
    *,
    status: Optional[int] = None,
    body: Any = None,
    error_text: str = "",
) -> Dict[str, Any]:
    """官方通道发送响应 → ``{kind, retriable, reason}``（纯函数）。

    入参任取其一即可：``body``（已解析 dict，最准）/ ``status``（HTTP 码）/ ``error_text``
    （原始错误串，含 code 时也能正则兜底）。``retriable`` 标记是否值得退避后重试。
    """
    plat = str(platform or "").strip().lower()
    err = _dig_error(body)
    code = _coerce_int(err.get("code"))
    subcode = _coerce_int(err.get("subcode"))
    msg = err.get("message") or ""
    text = f"{error_text} {msg}".strip()

    # 文本里夹带 "HTTP 4xx: {json}" 时，尝试从文本抠出第一个数字 code（body 没给时）
    if code is None and error_text:
        m = re.search(r'"code"\s*:\s*(-?\d+)', error_text)
        if m:
            code = _coerce_int(m.group(1))
        if subcode is None:
            ms = re.search(r'"error_subcode"\s*:\s*(\d+)', error_text)
            if ms:
                subcode = _coerce_int(ms.group(1))

    kind = "unknown"
    if plat == "whatsapp":
        if code in _WA_CODES:
            kind = _WA_CODES[code]
    elif plat in ("messenger", "instagram"):
        if subcode in _GRAPH_SUBCODES:
            kind = _GRAPH_SUBCODES[subcode]
        elif code in _GRAPH_CODES:
            kind = _GRAPH_CODES[code]
    elif plat == "zalo":
        if code in _ZALO_CODES:
            kind = _ZALO_CODES[code]

    # 平台码没命中 → 文本关键词兜底（窗口/token）
    if kind == "unknown" and text:
        if _WINDOW_TEXT.search(text):
            kind = "window_expired"
        elif _TOKEN_TEXT.search(text):
            kind = "invalid_token"

    # 仍未知 → HTTP 状态兜底
    if kind == "unknown":
        kind = _http_fallback(status)

    retriable = kind in ("rate_limited", "transient")
    reason = msg or error_text or (f"code={code}" if code is not None else f"http={status}")
    return {"kind": kind, "retriable": retriable, "reason": str(reason)[:200]}


# 终态（自由文本无论如何送不达，重试无用）——上层据此转人工/记一笔而非默默丢
TERMINAL_KINDS = frozenset({
    "window_expired", "invalid_token", "recipient_unavailable", "unsupported",
})


def is_terminal(kind: str) -> bool:
    return str(kind or "") in TERMINAL_KINDS


__all__ = [
    "classify_official_send_error", "is_terminal", "TERMINAL_KINDS",
]
