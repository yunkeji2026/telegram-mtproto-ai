"""Phase N3：account_registry meta 敏感字段静态加密（Fernet 对称）。

N2 把 ``session_string``（≈ 完整登录凭证）明文写进 ``account_registry.meta``。N3 给这些
敏感字段做**落盘加密**：写入前加密、读取时解密，且：

- **向后兼容**：加密值带 ``enc:v1:`` 前缀；无前缀的旧明文照常读出（N2 已写的不破）。
- **best-effort**：无 ``cryptography`` / 取不到密钥 → 原样明文存（只 warn 一次，绝不阻断登录）。
- **解密失败容错**：密钥丢失/换钥导致解不开 → 该字段置空（回落文件 session / 重新扫码），不喂garbage。

密钥来源（优先级）：
1. 环境变量 ``ACCOUNT_REGISTRY_KEY``（标准 Fernet key，urlsafe-base64 32B）；
2. 密钥文件 ``config/registry.key``（首次自动生成，权限收紧到 0600）。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_PREFIX = "enc:v1:"
# 视为敏感、需加密落盘的 meta 字段名
_SENSITIVE_KEYS = ("session_string", "two_fa_password", "session_secret")
_KEY_ENV = "ACCOUNT_REGISTRY_KEY"
_DEFAULT_KEY_FILE = Path("config/registry.key")

_fernet: Any = None
_fernet_resolved = False
_warned = False


def _resolve_fernet() -> Any:
    """取（并缓存）Fernet 实例；不可用返回 None（→ 明文回落）。"""
    global _fernet, _fernet_resolved, _warned
    if _fernet_resolved:
        return _fernet
    _fernet_resolved = True
    try:
        from cryptography.fernet import Fernet
    except Exception:
        if not _warned:
            logger.warning("[registry_crypto] cryptography 不可用 → meta 敏感字段明文存储")
            _warned = True
        _fernet = None
        return None

    key: Optional[bytes] = None
    env_key = os.environ.get(_KEY_ENV)
    if env_key:
        key = env_key.encode("utf-8")
    else:
        key = _load_or_create_key_file(Fernet)

    if not key:
        _fernet = None
        return None
    try:
        _fernet = Fernet(key)
    except Exception:
        if not _warned:
            logger.warning("[registry_crypto] 密钥无效（非合法 Fernet key）→ 明文存储")
            _warned = True
        _fernet = None
    return _fernet


def _load_or_create_key_file(Fernet: Any) -> Optional[bytes]:
    path = Path(os.environ.get("ACCOUNT_REGISTRY_KEY_FILE") or _DEFAULT_KEY_FILE)
    try:
        if path.exists():
            return path.read_bytes().strip() or None
        path.parent.mkdir(parents=True, exist_ok=True)
        key = Fernet.generate_key()
        path.write_bytes(key)
        try:
            os.chmod(path, 0o600)  # POSIX 收紧；Windows 上 best-effort（无效不报错）
        except Exception:
            pass
        logger.info("[registry_crypto] 已生成 meta 加密密钥：%s（请纳入备份/密管）", path)
        return key
    except Exception:
        logger.debug("[registry_crypto] 密钥文件读写失败", exc_info=True)
        return None


def reset_cache() -> None:
    """重置缓存（单测换钥/换 env 用）。"""
    global _fernet, _fernet_resolved, _warned
    _fernet = None
    _fernet_resolved = False
    _warned = False


def encrypt_meta(meta: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """返回副本：敏感字段值加密为 ``enc:v1:<token>``。无密钥 → 原样返回（明文）。"""
    if not meta:
        return dict(meta or {})
    out = dict(meta)
    f = _resolve_fernet()
    if f is None:
        return out
    for k in _SENSITIVE_KEYS:
        v = out.get(k)
        if isinstance(v, str) and v and not v.startswith(_PREFIX):
            try:
                out[k] = _PREFIX + f.encrypt(v.encode("utf-8")).decode("ascii")
            except Exception:
                logger.debug("[registry_crypto] 加密字段 %s 失败（保持明文）", k, exc_info=True)
    return out


def decrypt_meta(meta: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """返回副本：``enc:v1:`` 前缀的敏感字段解密还原。

    无前缀（旧明文）→ 原样；解密失败（密钥丢失/换钥）→ 该字段置空（回落文件 session）。
    """
    if not meta:
        return dict(meta or {})
    out = dict(meta)
    for k in _SENSITIVE_KEYS:
        v = out.get(k)
        if isinstance(v, str) and v.startswith(_PREFIX):
            f = _resolve_fernet()
            token = v[len(_PREFIX):]
            if f is None:
                out[k] = ""  # 有密文却无密钥 → 置空，喂 garbage 反而更糟
                continue
            try:
                out[k] = f.decrypt(token.encode("ascii")).decode("utf-8")
            except Exception:
                logger.debug("[registry_crypto] 解密字段 %s 失败（置空回落）", k, exc_info=True)
                out[k] = ""
    return out


__all__ = ["encrypt_meta", "decrypt_meta", "reset_cache", "_SENSITIVE_KEYS"]
