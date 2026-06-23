"""Stage A：陪伴「形象照/自拍」引擎（对标星野/Talkie/Replika 招牌能力）。

把变现目录里早已定义、却**零交付代码**的付费项 `exclusive_album`（专属相册）真正"通电"：
用户在对话里要照片 → 按关系等级 + 付费权益判准入 → 生成在 persona 一致的形象照 → 发出；
够不着的（关系浅）温柔搪塞，未解锁的给软付费引导（驱动 exclusive_album 转化）。

本模块是**纯逻辑 + 软失败 provider 骨架**（镜像 `tts_pipeline` 范式）：意图识别 / 提示词构造 /
准入决策都是可单测纯函数；图像 provider 默认 `disabled`（不接真模型零行为），接 openai images /
本地命令模板（如 ComfyUI/SD 推理脚本）后才真正出图。绝不抛——任何失败退回文字陪伴。

安全：提示词强制 SFW 约束（成人/暴露内容硬约束在 prompt 层，配合 persona_guard/wellbeing）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from src.utils.monetization import feature_allowed

logger = logging.getLogger(__name__)

# 付费项 id（变现目录 items.exclusive_album）；自拍超出免费额度后据此判拥有/引导解锁。
SELFIE_FEATURE = "exclusive_album"

# 意图关键词（多语，刻意保守——须明确指向"对方/AI 的样子/照片"，避免误命中用户自述照片）。
_REQUEST_MARKERS = (
    "自拍", "你的照片", "你的相片", "你的样子", "你长什么样", "你长啥样",
    "拍张照", "拍张自拍", "拍一张", "发张照片", "发张自拍", "发张图", "发个自拍",
    "看看你", "想看你", "你的照", "来张照片", "给我看看你", "你的写真", "你的近照",
    "selfie", "photo of you", "pic of you", "picture of you", "send a pic",
    "send me a pic", "send a photo", "show me you", "show me your face",
    "what do you look like", "your photo", "your picture", "see your face",
)


def detect_selfie_request(text: str) -> bool:
    """是否在向 AI 索要形象照/自拍（多语、保守）。"""
    t = str(text or "").strip().lower()
    if not t or len(t) > 200:  # 超长多半是叙述而非索图
        return False
    return any(m in t for m in _REQUEST_MARKERS)


def _persona_visual(persona: Any) -> str:
    """从 persona（dict/str）抽取**真实外貌描述**（不含 name 兜底）；缺则空串。"""
    if isinstance(persona, str):
        return persona.strip()
    if not isinstance(persona, dict):
        return ""
    for k in ("appearance", "visual", "look", "self_image_desc", "description"):
        v = str(persona.get(k) or "").strip()
        if v:
            return v
    return ""


def _persona_name(persona: Any) -> str:
    return str(persona.get("name") or "").strip() if isinstance(persona, dict) else ""


def build_selfie_prompt(
    persona: Any,
    *,
    scene_hint: str = "",
    style: str = "",
    default_appearance: str = "",
    sfw: bool = True,
) -> str:
    """构造形象照生成提示词（纯函数）。强制 SFW 安全约束。

    优先级：persona 真实外貌 → ``default_appearance``（config 可配）→ 按 name 的通用描述 → 中性兜底。
    """
    name = _persona_name(persona)
    base = (
        _persona_visual(persona)
        or str(default_appearance or "").strip()
        or (f"a warm, friendly companion named {name}" if name else "")
        or "a warm, friendly young woman, gentle expression"
    )
    parts = [f"Portrait selfie photo of {base}"]
    sc = str(scene_hint or "").strip()
    if sc:
        parts.append(sc)
    st = str(style or "").strip() or "natural lighting, candid, photorealistic, high quality"
    parts.append(st)
    if sfw:
        parts.append("fully clothed, tasteful, safe-for-work, no nudity")
    return ", ".join(p for p in parts if p)


def decide_selfie(
    *,
    entitlement: Optional[Dict[str, Any]],
    gate_enabled: bool,
    free_used: int,
    free_daily: int,
    bond_level: int = 0,
    min_bond_level: int = 0,
) -> Dict[str, Any]:
    """形象照准入决策（纯函数）。返回 ``{action, feature, used_free}``。

    - ``too_soon``：关系等级不足（避免一上来就要照片的轻浮感）。
    - ``allow``：已拥有相册 / gate 关（不计费）→ 不限；否则免费额度内 → ``used_free=True``。
    - ``locked``：gate 开 + 未拥有 + 免费额度用尽 → 走 exclusive_album 付费引导。
    """
    if int(bond_level) < int(min_bond_level):
        return {"action": "too_soon", "feature": SELFIE_FEATURE, "used_free": False}
    if feature_allowed(entitlement, SELFIE_FEATURE, gate_enabled=bool(gate_enabled)):
        return {"action": "allow", "feature": SELFIE_FEATURE, "used_free": False}
    if int(free_used) < max(0, int(free_daily)):
        return {"action": "allow", "feature": SELFIE_FEATURE, "used_free": True}
    return {"action": "locked", "feature": SELFIE_FEATURE, "used_free": False}


@dataclass
class SelfieResult:
    ok: bool = False
    image_path: str = ""
    prompt: str = ""
    provider: str = ""
    latency_ms: int = 0
    error: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)


class SelfieProvider:
    """形象照生成 provider（软失败骨架，镜像 TTSPipeline）。

    Config（``companion.selfie.provider``）：
        enabled: false
        backend: disabled | openai | command
        model/size/api_key/base_url：openai images 用
        command_args / command_template：本地推理（ComfyUI/SD 脚本），占位 {prompt}/{out}
        out_dir: tmp_selfies
    """

    def __init__(self, cfg: Optional[Dict[str, Any]] = None):
        cfg = cfg or {}
        self.enabled = bool(cfg.get("enabled", False))
        self.backend = str(cfg.get("backend", "disabled")).strip().lower()
        self.model = str(cfg.get("model") or "gpt-image-1").strip()
        self.size = str(cfg.get("size") or "1024x1024").strip()
        self.api_key = str(cfg.get("api_key") or "").strip()
        self.base_url = str(cfg.get("base_url") or "").strip().rstrip("/")
        self.out_dir = Path(str(cfg.get("out_dir") or "tmp_selfies"))
        self.command_args = cfg.get("command_args")
        self.command_template = str(cfg.get("command_template") or "").strip()
        self.command_timeout_sec = float(cfg.get("command_timeout_sec", 180) or 180)

    def stats(self) -> Dict[str, Any]:
        return {"enabled": self.enabled, "backend": self.backend,
                "model": self.model, "out_dir": str(self.out_dir)}

    async def generate(self, prompt: str, *, timeout_sec: float = 60.0) -> SelfieResult:
        rv = SelfieResult(prompt=str(prompt or ""), provider=self.backend)
        if not self.enabled or self.backend in ("", "disabled"):
            rv.error = "provider_disabled"
            return rv
        if not rv.prompt.strip():
            rv.error = "empty_prompt"
            return rv
        self.out_dir.mkdir(parents=True, exist_ok=True)
        out = self.out_dir / f"selfie-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}.png"
        t0 = time.monotonic()
        try:
            await asyncio.wait_for(
                asyncio.to_thread(self._generate_sync, rv.prompt, out),
                timeout=timeout_sec,
            )
            if out.exists() and out.stat().st_size > 0:
                rv.ok = True
                rv.image_path = str(out)
                rv.extra["bytes"] = out.stat().st_size
            else:
                rv.error = "empty_image"
        except asyncio.TimeoutError:
            rv.error = f"selfie_timeout({timeout_sec:.0f}s)"
        except Exception as ex:  # noqa: BLE001
            rv.error = f"{type(ex).__name__}: {ex}"
        rv.latency_ms = int((time.monotonic() - t0) * 1000)
        return rv

    def _generate_sync(self, prompt: str, out: Path) -> None:
        if self.backend == "openai":
            self._generate_openai(prompt, out)
            return
        if self.backend == "command":
            self._generate_command(prompt, out)
            return
        raise RuntimeError(f"unknown backend {self.backend}")

    def _generate_openai(self, prompt: str, out: Path) -> None:
        import base64 as _b64

        from openai import OpenAI  # type: ignore
        if not self.api_key:
            raise RuntimeError("missing api_key for openai images")
        kwargs: Dict[str, Any] = {"api_key": self.api_key}
        if self.base_url:
            kwargs["base_url"] = self.base_url
        client = OpenAI(**kwargs)
        resp = client.images.generate(
            model=self.model, prompt=prompt, size=self.size, n=1)
        b64 = resp.data[0].b64_json  # type: ignore[attr-defined]
        if not b64:
            raise RuntimeError("openai images: empty b64")
        out.write_bytes(_b64.b64decode(b64))

    def _generate_command(self, prompt: str, out: Path) -> None:
        raw_args = self.command_args
        tpl = self.command_template
        if not tpl and not isinstance(raw_args, list):
            raise RuntimeError("selfie command not configured")
        values = {"prompt": prompt, "out": str(out)}
        if isinstance(raw_args, list):
            cmd = [str(x).format(**values) for x in raw_args]
            r = subprocess.run(cmd, shell=False, capture_output=True, text=True,
                               timeout=self.command_timeout_sec, env=os.environ.copy())
        else:
            quoted = {k: shlex.quote(v) for k, v in values.items()}
            r = subprocess.run(tpl.format(**quoted), shell=True, capture_output=True,
                               text=True, timeout=self.command_timeout_sec,
                               env=os.environ.copy())
        if r.returncode != 0:
            raise RuntimeError(f"selfie_command_failed:{(r.stderr or r.stdout or '')[:300]}")


_selfie_singleton: Optional[SelfieProvider] = None


def get_selfie_provider(cfg: Optional[Dict[str, Any]] = None) -> SelfieProvider:
    global _selfie_singleton
    if _selfie_singleton is None:
        _selfie_singleton = SelfieProvider(cfg or {})
    return _selfie_singleton


def reset_selfie_provider() -> None:
    global _selfie_singleton
    _selfie_singleton = None


__all__ = [
    "SELFIE_FEATURE",
    "detect_selfie_request",
    "build_selfie_prompt",
    "decide_selfie",
    "SelfieResult",
    "SelfieProvider",
    "get_selfie_provider",
    "reset_selfie_provider",
]
