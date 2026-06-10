"""阶段 C-2：受控远程媒体下载（SSRF 安全）。

定位：``media_resolver`` 保持纯函数、只解析本地路径；本模块负责把**远程 http(s)
媒体 URL** 安全地下载到本地临时文件，供 OCR/ASR 复用。默认关闭（安全敏感）。

安全设计（多层）：
1. 仅允许 http/https。
2. 可选域名白名单（配置后只放行这些域名——生产推荐）。
3. DNS 解析后封锁私网/环回/链路本地/保留/多播 IP（SSRF 防护，含云元数据
   169.254.169.254）。
4. 禁止自动重定向（重定向可绕过上述校验 → 直接拒绝，回落上传）。
5. 大小上限（先看 Content-Length，再流式累计，超限即中止）。
6. content-type / 扩展名与媒体类型匹配校验。

残留风险：DNS rebinding（解析与连接之间重绑定）未完全闭合——media_ref 来源为
平台 runner 落库（半受信），威胁模型下可接受；如需彻底闭合需把连接 pin 到已校验
IP（破坏 https SNI/证书校验），留待后续。
"""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
import tempfile
from typing import List, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
_AUDIO_EXT = {".ogg", ".opus", ".mp3", ".m4a", ".wav", ".webm", ".amr", ".aac", ".mp4"}
_CHUNK = 64 * 1024


def _is_blocked_ip(ip_str: str) -> bool:
    """纯函数：判断 IP 是否属于禁止访问的内网/保留段（SSRF 防护）。解析失败视为不安全。"""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _host_in_allowlist(host: str, allow_domains: List[str]) -> bool:
    """host 命中白名单（精确或子域）。allow_domains 为空表示不启用白名单。"""
    host = (host or "").strip().lower().rstrip(".")
    for d in allow_domains or []:
        d = str(d or "").strip().lower().lstrip(".").rstrip(".")
        if not d:
            continue
        if host == d or host.endswith("." + d):
            return True
    return False


def _validate_url(url: str, allow_domains: List[str]) -> Tuple[bool, str, str]:
    """纯函数：scheme + 白名单校验（不做 DNS）。返回 (ok, host, reason)。"""
    try:
        p = urlparse(url)
    except Exception:
        return False, "", "bad_url"
    if p.scheme not in ("http", "https"):
        return False, "", "bad_scheme"
    host = p.hostname or ""
    if not host:
        return False, "", "bad_url"
    if allow_domains and not _host_in_allowlist(host, allow_domains):
        return False, host, "domain_not_allowed"
    return True, host, "ok"


def _resolve_host_safe(host: str) -> Tuple[bool, str]:
    """解析 host 的所有 IP；任一落在禁止段则判定不安全。返回 (safe, reason)。"""
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return False, "dns_failed"
    addrs = {str(info[4][0]) for info in infos if info and info[4]}
    if not addrs:
        return False, "dns_failed"
    for ip in addrs:
        if _is_blocked_ip(ip):
            return False, "blocked_host"
    return True, "ok"


def _ext_for(url: str, content_type: str, kind: str) -> str:
    ext = os.path.splitext(urlparse(url).path or "")[1].lower()
    valid = _IMAGE_EXT if kind == "image" else _AUDIO_EXT
    if ext in valid:
        return ext
    ct = (content_type or "").lower()
    if kind == "image":
        if "png" in ct:
            return ".png"
        if "webp" in ct:
            return ".webp"
        if "gif" in ct:
            return ".gif"
        return ".jpg"
    if "ogg" in ct or "opus" in ct:
        return ".ogg"
    if "mp4" in ct or "m4a" in ct:
        return ".m4a"
    if "wav" in ct:
        return ".wav"
    return ".mp3"


def _content_type_matches(content_type: str, kind: str) -> bool:
    ct = (content_type or "").lower().split(";")[0].strip()
    if not ct:
        return True  # 缺失则不据此拒绝（仍有扩展名/大小兜底）
    if kind == "image":
        return ct.startswith("image/")
    # 语音：常见为 audio/*，部分平台用 video/*（如 mp4/ogg 容器）
    return ct.startswith("audio/") or ct.startswith("video/") or ct == "application/octet-stream"


async def fetch_remote_media(
    url: str,
    *,
    kind: str,
    max_bytes: int = 10 * 1024 * 1024,
    timeout_sec: float = 8.0,
    allow_domains: Optional[List[str]] = None,
) -> Tuple[Optional[str], str]:
    """安全下载远程媒体到本地临时文件。返回 (local_path|None, reason)。

    reason ∈ ok | no_aiohttp | bad_url | bad_scheme | domain_not_allowed | dns_failed |
            blocked_host | redirect_blocked | http_error | too_large | bad_content_type |
            empty | fetch_failed
    调用方在失败时应回落「上传」路径；成功时负责用完删除临时文件。
    """
    # 先做廉价校验（无需可选依赖即可 fail-fast 拒绝坏 scheme / 内网主机）。
    allow_domains = allow_domains or []
    ok, host, reason = _validate_url(url, allow_domains)
    if not ok:
        return None, reason
    safe, reason = _resolve_host_safe(host)
    if not safe:
        return None, reason

    try:
        import aiohttp
    except Exception:
        return None, "no_aiohttp"

    tmp_path: Optional[str] = None
    try:
        timeout = aiohttp.ClientTimeout(total=float(timeout_sec or 8.0))
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            # allow_redirects=False：重定向可绕过 SSRF 校验，直接拒绝。
            async with sess.get(url, allow_redirects=False) as resp:
                if 300 <= resp.status < 400:
                    return None, "redirect_blocked"
                if resp.status != 200:
                    return None, "http_error"
                ctype = resp.headers.get("Content-Type", "")
                if not _content_type_matches(ctype, kind):
                    return None, "bad_content_type"
                clen = resp.headers.get("Content-Length")
                if clen and clen.isdigit() and int(clen) > max_bytes:
                    return None, "too_large"
                ext = _ext_for(url, ctype, kind)
                fd, tmp_path = tempfile.mkstemp(prefix="media_dl_", suffix=ext)
                total = 0
                with os.fdopen(fd, "wb") as fh:
                    async for chunk in resp.content.iter_chunked(_CHUNK):
                        total += len(chunk)
                        if total > max_bytes:
                            raise _TooLarge()
                        fh.write(chunk)
        if not total:
            _safe_unlink(tmp_path)
            return None, "empty"
        return tmp_path, "ok"
    except _TooLarge:
        _safe_unlink(tmp_path)
        return None, "too_large"
    except Exception:
        logger.debug("远程媒体下载失败 url=%s", url, exc_info=True)
        _safe_unlink(tmp_path)
        return None, "fetch_failed"


class _TooLarge(Exception):
    pass


def _safe_unlink(path: Optional[str]) -> None:
    if not path:
        return
    try:
        os.unlink(path)
    except Exception:
        pass


__all__ = ["fetch_remote_media", "_is_blocked_ip", "_validate_url", "_host_in_allowlist"]
