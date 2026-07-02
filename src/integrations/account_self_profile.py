"""账号「自身资料」采集与注册表富集（P1 身份化）。

把已登录账号**自己**的昵称/用户名（可选头像）写进 ``account_registry`` 的
``meta.self_*`` 字段，供「连接中心 / 账号切换条」显示真实身份（而非裸手机号或占位头像）。

设计约束（重要）：
- **纯函数** ``extract_self_profile`` / ``merge_self_profile_meta`` 零副作用、可单测。
- **feature flag**：``accounts.self_profile.enabled``（默认关，遵仓库「新子系统默认 false」约定）；
  头像下载另设子开关 ``accounts.self_profile.avatar``（更重、需真账号，默认关）。
- **全程 best-effort**：任何异常都吞掉，**绝不影响登录/收消息主链路**（登录成功才有资格富集）。
- **read-merge-write**：``registry.upsert(meta=)`` 是整块覆盖语义，直接传会清掉既有
  ``session_string`` 等敏感字段——故先读既有 meta，仅叠加 self_* 再写回。
- **手机号不上线**：Telegram 的 ``account_id`` 本身即手机号，前端已脱敏；这里**不再**额外
  往 meta 存 self_phone，减少 PII 面。
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_DEFAULT_AVATAR_DIR = "src/web/static/persona_avatars"
_AVATAR_URL_PREFIX = "/static/persona_avatars"
# 对外透出键（self_avatar_fid 是内部指纹，用于头像变更检测，不外泄）
_SELF_KEYS = ("self_name", "self_username", "self_avatar")

# ── 采集可观测计数（进程内，best-effort，供 fleet-health 展示"采集是否生效"） ──
_STATS_LOCK = threading.Lock()
_STATS: Dict[str, int] = {
    "calls": 0, "written": 0, "skipped": 0,
    "avatar_downloaded": 0, "avatar_reused": 0, "errors": 0,
}


def _bump(key: str, n: int = 1) -> None:
    with _STATS_LOCK:
        _STATS[key] = _STATS.get(key, 0) + n


def get_self_profile_stats() -> Dict[str, int]:
    with _STATS_LOCK:
        return dict(_STATS)


def reset_self_profile_stats() -> None:
    with _STATS_LOCK:
        for k in list(_STATS):
            _STATS[k] = 0


# ── feature flags ────────────────────────────────────────────────────────────

def self_profile_enabled(config: Optional[Dict[str, Any]]) -> bool:
    acc = ((config or {}).get("accounts", {}) or {}).get("self_profile", {}) or {}
    return bool(acc.get("enabled", False))


def self_avatar_enabled(config: Optional[Dict[str, Any]]) -> bool:
    acc = ((config or {}).get("accounts", {}) or {}).get("self_profile", {}) or {}
    return bool(acc.get("avatar", False))


# ── 纯函数（可单测） ─────────────────────────────────────────────────────────

def extract_self_profile(user: Any) -> Dict[str, str]:
    """从鸭子类型 user 对象（pyrogram/telethon User 或任意有同名属性者）抽取自身资料。

    返回仅含**非空**键的 dict：``self_name`` / ``self_username``。
    - name = "first last"（去空白）；都空则回落 @username；再空则空串（键不返回）。
    - username 去掉可能的前导 @。
    """
    if user is None:
        return {}
    first = str(getattr(user, "first_name", "") or "").strip()
    last = str(getattr(user, "last_name", "") or "").strip()
    username = str(getattr(user, "username", "") or "").strip().lstrip("@")
    name = (first + " " + last).strip()
    if not name:
        name = username  # 无姓名的号（bot/隐私）回落用户名当显示名
    out: Dict[str, str] = {}
    if name:
        out["self_name"] = name[:60]
    if username:
        out["self_username"] = username[:60]
    return out


def merge_self_profile_meta(
    existing_meta: Optional[Dict[str, Any]], profile: Dict[str, str]
) -> Dict[str, Any]:
    """把 profile 的 self_* 叠加进既有 meta（不动其它键）。空值不覆盖既有非空值。"""
    merged: Dict[str, Any] = dict(existing_meta or {})
    for k in _SELF_KEYS:
        v = profile.get(k)
        if v:
            merged[k] = v
    return merged


def read_self_profile_from_meta(meta: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """从注册表 meta 读回 self_*（供接口透出层复用，统一口径）。"""
    m = meta or {}
    out: Dict[str, str] = {}
    for k in _SELF_KEYS:
        v = str(m.get(k) or "").strip()
        if v:
            out[k] = v
    return out


def _safe_avatar_filename(platform: str, account_id: str) -> str:
    key = f"{platform}_{account_id}"
    key = re.sub(r"[^A-Za-z0-9_.-]", "_", key)
    return f"self_{key}.jpg"


# ── 头像缓存击穿 + 变更检测（纯函数，可单测） ────────────────────────────────

def photo_file_ref(user: Any) -> str:
    """取账号自身头像的稳定 file_id（照片变更时该 id 会变，用于判定是否需重下）。"""
    photo = getattr(user, "photo", None)
    return str(
        getattr(photo, "big_file_id", None)
        or getattr(photo, "small_file_id", None)
        or ""
    )


def avatar_cache_key(fid: str) -> str:
    """由 file_id 派生短缓存键：同图恒定（幂等不重下），换图即变（浏览器缓存击穿）。"""
    s = str(fid or "")
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:8] if s else ""


def build_avatar_url(platform: str, account_id: str, fid: str) -> str:
    """本地静态头像 URL + ?v=<fid 派生键>（缓存击穿）。"""
    fn = _safe_avatar_filename(platform, account_id)
    key = avatar_cache_key(fid)
    return f"{_AVATAR_URL_PREFIX}/{fn}" + (f"?v={key}" if key else "")


def avatar_needs_refresh(existing_meta: Optional[Dict[str, Any]], fid: str) -> bool:
    """头像是否需要重下：有新 fid 且（与既有指纹不同 或 之前根本没存过头像）。"""
    if not fid:
        return False
    m = existing_meta or {}
    return str(m.get("self_avatar_fid") or "") != str(fid) or not m.get("self_avatar")


# ── 副作用封装（best-effort，全程降级） ──────────────────────────────────────

async def _download_avatar(
    client: Any, file_ref: str, platform: str, account_id: str, avatar_dir: str
) -> bool:
    """best-effort 下载头像到本地静态目录，成功 True。任何异常吞掉。"""
    try:
        if not (file_ref and hasattr(client, "download_media")):
            return False
        Path(avatar_dir).mkdir(parents=True, exist_ok=True)
        dest = str(Path(avatar_dir) / _safe_avatar_filename(platform, account_id))
        saved = await client.download_media(file_ref, file_name=dest)
        return bool(saved)
    except Exception:  # noqa: BLE001
        logger.debug("[self_profile] 头像下载失败（忽略）", exc_info=True)
        return False


async def enrich_from_user(
    platform: str,
    account_id: str,
    user: Any,
    *,
    config: Optional[Dict[str, Any]] = None,
    client: Any = None,
    avatar_dir: str = _DEFAULT_AVATAR_DIR,
) -> Dict[str, str]:
    """登录成功后调用：把 user 的自身资料富集进注册表 meta.self_*。

    返回实际写入的 self_* dict（未启用/失败返回空 dict）。**绝不抛异常**。
    """
    if not self_profile_enabled(config):
        return {}
    _bump("calls")
    try:
        profile = extract_self_profile(user)
        from src.integrations.account_registry import get_account_registry
        reg = get_account_registry()
        existing = reg.get(platform, account_id) or {}
        existing_meta = existing.get("meta") or {}
        new_fid = ""  # 本次确认的头像指纹（写库时持久化，供下次变更检测）
        if self_avatar_enabled(config) and client is not None:
            fid = photo_file_ref(user)
            if fid and not avatar_needs_refresh(existing_meta, fid) \
                    and existing_meta.get("self_avatar"):
                # 头像未变 → 复用既有 URL，不重下（省带宽 + 保幂等）
                profile["self_avatar"] = str(existing_meta["self_avatar"])
                new_fid = fid
                _bump("avatar_reused")
            elif fid and await _download_avatar(
                    client, fid, platform, account_id, avatar_dir):
                profile["self_avatar"] = build_avatar_url(platform, account_id, fid)
                new_fid = fid
                _bump("avatar_downloaded")
        if not profile:
            return {}
        merged = merge_self_profile_meta(existing_meta, profile)
        if new_fid:
            merged["self_avatar_fid"] = new_fid  # 内部指纹，不外泄
        # 幂等优化：self_*（含头像指纹）与既有一致则跳过写库（避免每启动白写 + bump updated_at）
        if merged == existing_meta:
            _bump("skipped")
            return profile
        reg.upsert(platform, account_id, meta=merged)
        _bump("written")
        return profile
    except Exception:  # noqa: BLE001
        _bump("errors")
        logger.debug("[self_profile] 富集失败（忽略）", exc_info=True)
        return {}
