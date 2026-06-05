"""访客身份 token（无账号）：HMAC 签名，防伪造、可选有效期。

token 形如 ``<b64url(payload)>.<hexsig>``，payload={"vid","iat"}。
secret 取 web_chat.token_secret，留空则回落 web_admin.secret_key。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from typing import Optional


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def new_visitor_id() -> str:
    return "wv_" + uuid.uuid4().hex[:20]


def issue_visitor_token(secret: str, visitor_id: str, *, issued_at: Optional[float] = None) -> str:
    iat = int(issued_at if issued_at is not None else time.time())
    raw = json.dumps({"vid": visitor_id, "iat": iat},
                     separators=(",", ":"), sort_keys=True).encode()
    body = _b64e(raw)
    sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def verify_visitor_token(secret: str, token: str, *, max_age_sec: float = 0) -> Optional[str]:
    """校验 token；通过返回 visitor_id，否则 None。"""
    if not token or "." not in token:
        return None
    body, _, sig = token.partition(".")
    expected = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return None
    try:
        payload = json.loads(_b64d(body))
    except Exception:
        return None
    vid = str(payload.get("vid") or "")
    if not vid:
        return None
    if max_age_sec and (time.time() - float(payload.get("iat") or 0)) > float(max_age_sec):
        return None
    return vid
