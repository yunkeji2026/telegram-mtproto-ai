"""桌面壳选择器覆写层（D1 热更新地基）——后端权威的「选择器修正」下发源。

桌面注入脚本（``desktop/inject/profiles.js`` 内置定制档 + 通用工厂档）启动时拉取本端点；
官方网页改版导致选择器失配时，运营**只改一个 JSON 文件**即可热修，无需重新打包/分发桌面端。

设计取舍：
- **后端只下发「覆写补丁」**（patch），不复制全套选择器 → 内置档（profiles.js）仍是唯一权威，
  避免两处选择器漂移。补丁为空时（常态）注入直接用内置档。
- 仅白名单字段可被覆写（与 profiles.js::OVERLAYABLE_KEYS 对齐）：选择器字符串 + 少量布尔开关；
  自定义解析函数永不可被远程替换（安全边界）。
- ``version`` 为内容散列，供桌面端将来做条件拉取/缓存（本期注入仍每次拉，量极小）。

纯函数 + 文件读取，无 FastAPI 依赖，便于单测。
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)

# 与 desktop/inject/profiles.js::OVERLAYABLE_KEYS 保持一致（任何一端新增字段需两处同步）。
OVERLAYABLE_KEYS = (
    "bubble", "bubbleText", "composer", "sendBtn", "peerTitle",
    "outFlag", "outSelector", "mediaImg", "mediaAudio",
    "supported", "canIngest", "richInput",
)

_BOOL_KEYS = {"supported", "canIngest", "richInput"}

# 覆写文件默认路径：<repo>/config/desktop_selector_profiles.json（不存在=无覆写，常态）。
_DEFAULT_PATH = Path(__file__).resolve().parents[2] / "config" / "desktop_selector_profiles.json"


def _sanitize(raw: Any) -> Dict[str, Dict[str, Any]]:
    """把任意 JSON 收敛成 ``{platform: {overlayable_key: value}}``，丢弃非法字段。

    类型守卫：布尔字段只收 bool，字符串字段只收非空 str。防止运营误填把注入打挂。
    """
    out: Dict[str, Dict[str, Any]] = {}
    if not isinstance(raw, dict):
        return out
    # 兼容两种顶层形态：{profiles:{...}} 或直接 {platform:{...}}
    profiles = raw.get("profiles") if isinstance(raw.get("profiles"), dict) else raw
    if not isinstance(profiles, dict):
        return out
    for platform, patch in profiles.items():
        if not isinstance(platform, str) or not isinstance(patch, dict):
            continue
        clean: Dict[str, Any] = {}
        for key in OVERLAYABLE_KEYS:
            if key not in patch:
                continue
            val = patch[key]
            if key in _BOOL_KEYS:
                if isinstance(val, bool):
                    clean[key] = val
            elif isinstance(val, str) and val.strip():
                clean[key] = val
        if clean:
            out[platform] = clean
    return out


def load_selector_overlay(path: Path | str | None = None) -> Dict[str, Dict[str, Any]]:
    """读取并清洗覆写文件；不存在/损坏 → 返回空 dict（注入用内置档）。"""
    p = Path(path) if path is not None else _DEFAULT_PATH
    try:
        if not p.exists():
            return {}
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("[desktop] 选择器覆写文件解析失败，降级为空覆写：%s", p, exc_info=True)
        return {}
    return _sanitize(raw)


def overlay_version(profiles: Dict[str, Dict[str, Any]]) -> str:
    """内容散列（稳定、与 key 顺序无关）→ 供条件拉取/缓存。空覆写返回固定串。"""
    if not profiles:
        return "empty"
    blob = json.dumps(profiles, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def selector_profiles_payload(path: Path | str | None = None) -> Dict[str, Any]:
    """端点响应体：``{ok, version, profiles}``。"""
    profiles = load_selector_overlay(path)
    return {"ok": True, "version": overlay_version(profiles), "profiles": profiles}
