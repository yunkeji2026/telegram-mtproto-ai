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

def _getf(obj: Any, *keys: str) -> Any:
    """从对象（getattr）或 dict（get）取首个非空字段——兼容各平台不同数据形态。"""
    for k in keys:
        v = obj.get(k) if isinstance(obj, dict) else getattr(obj, k, None)
        if v:
            return v
    return None


def extract_self_profile(user: Any) -> Dict[str, str]:
    """从鸭子类型 user（对象或 dict）抽取自身资料，兼容各平台字段命名。

    返回仅含**非空**键的 dict：``self_name`` / ``self_username``。
    - name = "first last"（Telegram）；无则回落 ``name``/``pushname``/``display_name``
      （WhatsApp/LINE/Messenger）；再无则回落 @username。
    - username 去掉可能的前导 @。
    """
    if user is None:
        return {}
    first = str(_getf(user, "first_name") or "").strip()
    last = str(_getf(user, "last_name") or "").strip()
    username = str(_getf(user, "username") or "").strip().lstrip("@")
    name = (first + " " + last).strip()
    if not name:
        # 只给单一显示名的平台（WA pushname / LINE displayName / Messenger name）
        name = str(_getf(user, "name", "pushname", "display_name") or "").strip()
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
        existing_meta = (reg.get(platform, account_id) or {}).get("meta") or {}
        extra: Dict[str, Any] = {}
        if self_avatar_enabled(config) and client is not None:
            fid = photo_file_ref(user)
            if fid and not avatar_needs_refresh(existing_meta, fid) \
                    and existing_meta.get("self_avatar"):
                # 头像未变 → 复用既有 URL，不重下（省带宽 + 保幂等）
                profile["self_avatar"] = str(existing_meta["self_avatar"])
                extra["self_avatar_fid"] = fid
                _bump("avatar_reused")
            elif fid and await _download_avatar(
                    client, fid, platform, account_id, avatar_dir):
                profile["self_avatar"] = build_avatar_url(platform, account_id, fid)
                extra["self_avatar_fid"] = fid
                _bump("avatar_downloaded")
        return _write_profile(platform, account_id, profile, existing_meta, extra)
    except Exception:  # noqa: BLE001
        _bump("errors")
        logger.debug("[self_profile] 富集失败（忽略）", exc_info=True)
        return {}


async def enrich_from_fields(
    platform: str,
    account_id: str,
    *,
    name: str = "",
    username: str = "",
    avatar_url: str = "",
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, str]:
    """通用富集入口（供 WhatsApp/LINE/Messenger 等已知自身昵称的适配器直接喂字段）。

    与 ``enrich_from_user`` 共用同一 read-merge-write + 计数管道。``avatar_url`` 为远端
    头像直链（这些平台无需本地下载，直接存 URL）。flag 关/无有效字段 → 空 dict，绝不抛。
    """
    if not self_profile_enabled(config):
        return {}
    _bump("calls")
    try:
        profile = extract_self_profile(
            {"name": name, "username": username})
        if avatar_url:
            profile["self_avatar"] = str(avatar_url)
        if not profile:
            return {}
        from src.integrations.account_registry import get_account_registry
        reg = get_account_registry()
        existing_meta = (reg.get(platform, account_id) or {}).get("meta") or {}
        return _write_profile(platform, account_id, profile, existing_meta)
    except Exception:  # noqa: BLE001
        _bump("errors")
        logger.debug("[self_profile] enrich_from_fields 失败（忽略）", exc_info=True)
        return {}


def _write_profile(
    platform: str, account_id: str, profile: Dict[str, str],
    existing_meta: Dict[str, Any], extra_meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, str]:
    """read-merge-write 收尾：合并 self_* + 内部字段，幂等跳过，写库并计数。"""
    if not profile:
        return {}
    merged = merge_self_profile_meta(existing_meta, profile)
    for k, v in (extra_meta or {}).items():
        if v:
            merged[k] = v
    # 幂等优化：与既有一致则跳过写库（避免每启动白写 + bump updated_at）
    if merged == existing_meta:
        _bump("skipped")
        return profile
    from src.integrations.account_registry import get_account_registry
    get_account_registry().upsert(platform, account_id, meta=merged)
    _bump("written")
    return profile


def cleanup_avatar(
    platform: str, account_id: str, avatar_dir: str = _DEFAULT_AVATAR_DIR
) -> bool:
    """账号移除时回收其自身头像文件（best-effort）。删除成功/无文件返回 True，异常 False。"""
    try:
        f = Path(avatar_dir) / _safe_avatar_filename(platform, account_id)
        if f.exists():
            f.unlink()
        return True
    except Exception:  # noqa: BLE001
        logger.debug("[self_profile] 头像回收失败（忽略）", exc_info=True)
        return False


def sweep_orphan_avatars(
    known_keys: Any, avatar_dir: str = _DEFAULT_AVATAR_DIR
) -> Dict[str, int]:
    """清扫孤儿头像：删除 self_*.jpg 中「注册表已无对应活跃账号」的残留文件。

    ``known_keys``：当前活跃账号的 ``self_<platform>_<account_id>`` 文件名基（不含 .jpg）
    集合，或形如 ``{(platform, account_id), ...}`` 的元组集合（自动换算文件名）。
    补 ``remove()`` 之外的漏网（如直接删库行、迁移遗留）。best-effort，绝不抛。

    返回 ``{"scanned": n, "removed": m}``。
    """
    result = {"scanned": 0, "removed": 0}
    try:
        keep: set = set()
        for k in (known_keys or set()):
            if isinstance(k, (tuple, list)) and len(k) == 2:
                keep.add(_safe_avatar_filename(str(k[0]), str(k[1])))
            else:
                s = str(k)
                keep.add(s if s.endswith(".jpg") else f"{s}.jpg")
        d = Path(avatar_dir)
        if not d.is_dir():
            return result
        for f in d.glob("self_*.jpg"):
            result["scanned"] += 1
            if f.name not in keep:
                try:
                    f.unlink()
                    result["removed"] += 1
                except Exception:  # noqa: BLE001
                    logger.debug("[self_profile] 孤儿头像删除失败 %s", f.name, exc_info=True)
    except Exception:  # noqa: BLE001
        logger.debug("[self_profile] 孤儿头像清扫失败（忽略）", exc_info=True)
    return result


def dump_self_profile_prom() -> str:
    """Prometheus exposition：account_self_profile_<key>_total（供外部监控采集健康）。"""
    st = get_self_profile_stats()
    lines = [
        "# HELP account_self_profile_total 账号自身资料采集计数（进程内，自启动累计）",
        "# TYPE account_self_profile_total counter",
    ]
    for k, v in st.items():
        lines.append(f'account_self_profile_total{{op="{k}"}} {int(v)}')
    return "\n".join(lines) + "\n"
