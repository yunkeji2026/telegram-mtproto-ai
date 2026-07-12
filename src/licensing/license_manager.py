"""离线授权码：签发 / 验签 / 状态计算（Ed25519 非对称签名）。

设计要点
========
- **离线**：不依赖外网。厂商持私钥签发，产品内置公钥验签，适配私有化部署。
- **不可伪造（对仅持授权文件者）**：Ed25519 非对称。注意——本产品以源码交付，
  能改源码者总能绕过任何校验，这是分发模型决定的；授权的职责是「对诚实客户强制
  到期/额度 + 让休闲伪造不可行 + 支撑试用/分级运营」，而非对抗改源码的对手。
- **零破坏（C0-1）**：本阶段仅产出只读状态，``read_only`` 恒为 False；过期也不阻断、
  更不删数据。真正的功能 gating 在 C0-3 接入（用 ``feature_enabled`` / ``state``）。

授权码格式
==========
``<payload_b64url>.<signature_b64url>``（URL-safe base64 去 padding）。
payload 为 JSON，字段：

==============  ====================================================
sub             客户标识（公司名）
plan            community | basic | pro | flagship
iat / exp       签发 / 到期 unix 秒；exp 省略或 0 = 永久
seats           最大坐席席位（0 = 不限）
channels        允许渠道列表，如 ["telegram","line","web"]
features        功能位 dict，如 {"l4": true, "white_label": true}
grace_days      到期后宽限天数（默认 7）
lic_id          授权编号（便于吊销登记）
included_chars  含翻译/TTS 字符额度（0/省略 = 不限；P0-4 试用计量用）
trial           是否试用授权（bool，仅标记/展示用）
==============  ====================================================
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

try:  # 软依赖：缺失时降级为「验签不可用」而非崩溃
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
        PublicFormat,
    )

    _CRYPTO_OK = True
except Exception:  # pragma: no cover - 仅在未装 cryptography 时触发
    _CRYPTO_OK = False


# 厂商默认公钥（32 字节 Ed25519，hex）。真实厂商应用 scripts/license_tool.py
# 重新生成密钥对并替换此常量，私钥离线保管、切勿入库。
DEFAULT_VENDOR_PUBLIC_KEY_HEX = (
    "8ad5fff37020ac0bf95a1a9bb415bde58094c2981f0dadc1e9cead5dc7ce6dd0"
)

DEFAULT_GRACE_DAYS = 7

# 社区（未授权）默认值——C0-1 不强制，仅用于状态展示与 C0-3 gating 预留
_COMMUNITY = {
    "plan": "community",
    "seats": 2,
    "channels": ["web"],
    "features": {},
}


class LicenseError(Exception):
    """签发或验签过程中的可预期错误。"""


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


# ── 厂商侧：密钥生成 + 签发（供 scripts/license_tool.py 使用）─────────────────

def generate_keypair() -> Dict[str, str]:
    """生成 Ed25519 密钥对，返回 {"public_hex", "private_hex"}（各 32 字节 hex）。"""
    if not _CRYPTO_OK:
        raise LicenseError("cryptography 未安装，无法生成密钥对")
    priv = Ed25519PrivateKey.generate()
    pub_hex = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
    priv_hex = priv.private_bytes(
        Encoding.Raw, PrivateFormat.Raw, NoEncryption()
    ).hex()
    return {"public_hex": pub_hex, "private_hex": priv_hex}


def issue_license(payload: Dict[str, Any], private_hex: str) -> str:
    """用厂商私钥签发授权码。``payload`` 缺省补 iat / grace_days。"""
    if not _CRYPTO_OK:
        raise LicenseError("cryptography 未安装，无法签发授权")
    body = dict(payload)
    body.setdefault("iat", int(time.time()))
    body.setdefault("grace_days", DEFAULT_GRACE_DAYS)
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    priv = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private_hex))
    sig = priv.sign(raw)
    return f"{_b64url_encode(raw)}.{_b64url_encode(sig)}"


# ── 状态快照 ────────────────────────────────────────────────────────────────

@dataclass
class LicenseStatus:
    """授权状态快照（只读）。

    state 取值：
    - ``active``      验签通过且未过期
    - ``grace``       已过期但在宽限期内（仍可用，应提示续费）
    - ``expired``     过期超宽限（C0-3 起将降级只读；C0-1 仅提示）
    - ``unlicensed``  无授权文件 —— 社区模式
    - ``invalid``     格式错误 / 签名不匹配 / 被篡改
    - ``unavailable`` 运行环境缺 cryptography，无法验签
    """

    state: str = "unlicensed"
    plan: str = "community"
    customer: str = ""
    lic_id: str = ""
    issued_at: int = 0
    expires_at: int = 0  # 0 = 永久
    grace_days: int = DEFAULT_GRACE_DAYS
    seats: int = 0
    channels: List[str] = field(default_factory=list)
    features: Dict[str, Any] = field(default_factory=dict)
    messages: List[str] = field(default_factory=list)
    enforce: bool = False  # C0-3：是否开启强制（来自 licensing.enforce）
    # P0-4 免费试用（字符额度）：翻译/TTS 合计含量（0 = 不限）+ 试用标记
    included_chars: int = 0
    trial: bool = False

    @property
    def licensed(self) -> bool:
        """是否处于「有有效授权」状态（active 或 grace）。"""
        return self.state in ("active", "grace")

    @property
    def days_left(self) -> Optional[int]:
        """距到期天数（向下取整）；永久授权或无到期返回 None；已过期为负。"""
        if not self.expires_at:
            return None
        return int((self.expires_at - time.time()) // 86400)

    @property
    def read_only(self) -> bool:
        """是否应降级为只读。

        仅当 ``enforce=True`` 且授权 ``expired``（过期超宽限）或 ``invalid``（篡改）时为真。
        ``unlicensed``（社区模式）/ ``unavailable``（环境缺库）/ ``grace``（宽限期内）
        **永不** 因强制而锁死——避免误伤诚实客户与社区/开发部署。
        """
        return bool(self.enforce) and self.state in ("expired", "invalid")

    def feature_enabled(self, name: str) -> bool:
        """功能位查询（供 C0-3 gating 用）。社区/过期返回保守值。"""
        if self.state in ("expired", "invalid"):
            return False
        if self.licensed:
            return bool(self.features.get(name, False))
        return bool(_COMMUNITY["features"].get(name, False))

    def channel_allowed(self, channel: str) -> bool:
        chans = self.channels if self.licensed else list(_COMMUNITY["channels"])
        return (not chans) or (channel in chans)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "licensed": self.licensed,
            "plan": self.plan,
            "customer": self.customer,
            "lic_id": self.lic_id,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "days_left": self.days_left,
            "grace_days": self.grace_days,
            "seats": self.seats,
            "channels": list(self.channels),
            "features": dict(self.features),
            "included_chars": self.included_chars,
            "trial": self.trial,
            "enforce": self.enforce,
            "read_only": self.read_only,
            "messages": list(self.messages),
        }


# ── 产品侧：加载 + 验签 + 状态 ───────────────────────────────────────────────

class LicenseManager:
    """加载授权文件、验签、计算状态。线程安全、结果缓存、可 reload。"""

    def __init__(
        self,
        *,
        license_path: Optional[str] = None,
        public_key_hex: Optional[str] = None,
        license_token: Optional[str] = None,
        enforce: bool = False,
        now_fn=time.time,
    ):
        self._path = license_path
        self._public_key_hex = (public_key_hex or DEFAULT_VENDOR_PUBLIC_KEY_HEX).strip()
        self._inline_token = license_token
        self._enforce = bool(enforce)
        self._now = now_fn
        self._lock = threading.Lock()
        self._cached: Optional[LicenseStatus] = None

    def set_enforce(self, enforce: bool) -> None:
        """更新强制开关并使缓存失效（供启动时按 config 配置）。"""
        with self._lock:
            self._enforce = bool(enforce)
            self._cached = None

    @property
    def license_path(self) -> Optional[str]:
        """授权文件路径（C4 粘贴激活写入目标；可能为 None＝纯内联/env 模式）。"""
        return self._path

    def preview_token(self, token: str) -> LicenseStatus:
        """校验一段授权码并返回其状态快照——**不写盘、不动单例缓存**。

        C4 粘贴激活用：先 preview 确认 active/grace 再落盘，避免把无效/过期
        key 写进 ``config/license.key`` 后系统反而降级。
        """
        return LicenseManager(
            license_token=str(token or "").strip() or "-",
            public_key_hex=self._public_key_hex,
            enforce=self._enforce,
            now_fn=self._now,
        ).status()

    # -- 读取原始 token：优先内联 > 环境变量 > 文件 --
    def _read_token(self) -> Optional[str]:
        if self._inline_token:
            return self._inline_token.strip()
        env = os.environ.get("LICENSE_KEY")
        if env:
            return env.strip()
        path = self._path
        if path and os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    return fh.read().strip()
            except Exception:
                return None
        return None

    def _verify(self, token: str) -> Dict[str, Any]:
        """验签并返回 payload；失败抛 LicenseError。"""
        if not _CRYPTO_OK:
            raise LicenseError("unavailable")
        if "." not in token:
            raise LicenseError("格式错误")
        body_b64, sig_b64 = token.split(".", 1)
        try:
            raw = _b64url_decode(body_b64)
            sig = _b64url_decode(sig_b64)
        except Exception:
            raise LicenseError("编码错误")
        try:
            pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(self._public_key_hex))
            pub.verify(sig, raw)
        except InvalidSignature:
            raise LicenseError("签名不匹配")
        except Exception:
            raise LicenseError("验签失败")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            raise LicenseError("payload 解析失败")
        if not isinstance(payload, dict):
            raise LicenseError("payload 非对象")
        return payload

    def _compute(self) -> LicenseStatus:
        token = self._read_token()
        if not token:
            return LicenseStatus(
                state="unlicensed",
                plan=str(_COMMUNITY["plan"]),
                seats=int(_COMMUNITY["seats"]),
                channels=list(_COMMUNITY["channels"]),
                features=dict(_COMMUNITY["features"]),
                messages=["未检测到授权文件，当前为社区模式"],
            )
        if not _CRYPTO_OK:
            return LicenseStatus(
                state="unavailable",
                messages=["运行环境缺少 cryptography，无法校验授权"],
            )
        try:
            payload = self._verify(token)
        except LicenseError as e:
            if str(e) == "unavailable":
                return LicenseStatus(
                    state="unavailable",
                    messages=["运行环境缺少 cryptography，无法校验授权"],
                )
            return LicenseStatus(
                state="invalid",
                messages=[f"授权无效：{e}"],
            )

        exp = int(payload.get("exp") or 0)
        grace_days = int(payload.get("grace_days", DEFAULT_GRACE_DAYS))
        now = int(self._now())
        st = LicenseStatus(
            plan=str(payload.get("plan", "basic")),
            customer=str(payload.get("sub", "")),
            lic_id=str(payload.get("lic_id", "")),
            issued_at=int(payload.get("iat") or 0),
            expires_at=exp,
            grace_days=grace_days,
            seats=int(payload.get("seats") or 0),
            channels=list(payload.get("channels") or []),
            features=dict(payload.get("features") or {}),
            included_chars=max(0, int(payload.get("included_chars") or 0)),
            trial=bool(payload.get("trial", False)),
        )
        if not exp or now <= exp:
            st.state = "active"
            if exp and (exp - now) <= 7 * 86400:
                st.messages.append(f"授权将于 {st.days_left} 天后到期，请及时续费")
        elif now <= exp + grace_days * 86400:
            st.state = "grace"
            over = int((now - exp) // 86400)
            st.messages.append(
                f"授权已过期 {over} 天，处于 {grace_days} 天宽限期内，请尽快续费"
            )
        else:
            st.state = "expired"
            st.messages.append("授权已过期且超出宽限期，请联系厂商续费")
        return st

    def status(self, *, refresh: bool = False) -> LicenseStatus:
        with self._lock:
            if self._cached is None or refresh:
                st = self._compute()
                st.enforce = self._enforce
                if st.read_only:
                    st.messages.append("授权强制已开启且授权失效，系统进入只读模式")
                self._cached = st
            return self._cached

    def reload(self) -> LicenseStatus:
        return self.status(refresh=True)


# ── 进程级单例 ───────────────────────────────────────────────────────────────

_SINGLETON: Optional[LicenseManager] = None
_SINGLETON_LOCK = threading.Lock()


def _default_license_path() -> str:
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(here, "config", "license.key")


def get_license_manager(
    *, license_path: Optional[str] = None, public_key_hex: Optional[str] = None,
) -> LicenseManager:
    """获取进程级 LicenseManager 单例（首次调用确定路径/公钥）。"""
    global _SINGLETON
    with _SINGLETON_LOCK:
        if _SINGLETON is None:
            _SINGLETON = LicenseManager(
                license_path=license_path or _default_license_path(),
                public_key_hex=public_key_hex,
            )
        return _SINGLETON


def configure_license_manager(*, enforce: bool) -> LicenseStatus:
    """启动时按 config 配置强制开关并返回最新状态（单例已存在则就地更新）。"""
    mgr = get_license_manager()
    mgr.set_enforce(enforce)
    return mgr.status(refresh=True)


def reset_license_manager() -> None:
    """测试辅助：清空单例。"""
    global _SINGLETON
    with _SINGLETON_LOCK:
        _SINGLETON = None
