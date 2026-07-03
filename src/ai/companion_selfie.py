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
import random
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
    # 繁体 / 港台常见写法（对方多为繁体输入，简体 marker 匹配不到 → 补齐同义）
    "發張照片", "發個照片", "發張自拍", "發個自拍", "發張圖", "傳張照片", "傳個照片",
    "拍個照片", "來張照片", "給我看看你", "給我看看妳", "看看妳", "想看妳",
    "你的樣子", "妳的照片", "妳的相片", "妳的樣子", "你長什麼樣", "你長啥樣",
    "妳長什麼樣", "你的寫真", "你的近照",
    "selfie", "photo of you", "pic of you", "picture of you", "send a pic",
    "send me a pic", "send a photo", "show me you", "show me your face",
    "what do you look like", "your photo", "your picture", "see your face",
)

# 反向护栏：明确指向"对方做/煮/买的东西"的照片（"你煮的…拍张照给我看"）属**对话临时要图**
# （上下文要图 = 后续 Stage B），不是"人设本人自拍" → 命中则不当作 selfie，避免误发人设照片。
_OBJECT_PHOTO_MARKERS = (
    "你煮的", "妳煮的", "你做的", "妳做的", "你买的", "你買的",
    "你拍的", "你點的", "你点的", "你種的", "你种的", "你養的", "你养的",
    "你寫的", "你写的", "你画的", "你畫的",
)


def detect_selfie_request(text: str) -> bool:
    """是否在向 AI 索要形象照/自拍（多语、保守，含繁体）。

    反向护栏：请求明确指向"对方做/煮/买的东西"的照片（``你煮的…拍张照``）→ 返回 False，
    交由上下文要图路径处理，避免把"拍下你煮的面"误当成"发一张你的自拍"。
    """
    t = str(text or "").strip().lower()
    if not t or len(t) > 200:  # 超长多半是叙述而非索图
        return False
    if any(m in t for m in _OBJECT_PHOTO_MARKERS):
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


# album 后端可挑选的图片扩展名。
_ALBUM_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


class SelfieProvider:
    """形象照生成 provider（软失败骨架，镜像 TTSPipeline）。

    Config（``companion.selfie.provider``）：
        enabled: false
        backend: disabled | openai | command
        model/size/api_key/base_url：openai images 用（model=gpt-image-1 默认；亦支持 dall-e-3）
        quality: 可选（gpt-image-1：low|medium|high|auto）；request_timeout_sec: 单请求超时(默认 60)
        command_args / command_template：本地推理（ComfyUI/SD 脚本），占位 {prompt}/{out}
        out_dir: tmp_selfies
    """

    def __init__(self, cfg: Optional[Dict[str, Any]] = None):
        cfg = cfg or {}
        self.enabled = bool(cfg.get("enabled", False))
        self.backend = str(cfg.get("backend", "disabled")).strip().lower()
        self.model = str(cfg.get("model") or "gpt-image-1").strip()
        self.size = str(cfg.get("size") or "1024x1024").strip()
        self.quality = str(cfg.get("quality") or "").strip()
        self.api_key = str(cfg.get("api_key") or "").strip()
        self.base_url = str(cfg.get("base_url") or "").strip().rstrip("/")
        self.request_timeout_sec = float(cfg.get("request_timeout_sec", 60) or 60)
        self.out_dir = Path(str(cfg.get("out_dir") or "tmp_selfies"))
        # backend=album：从预制相册随机挑图（不出图、零 API 费、同一张脸最一致）。
        self.album_dir = Path(str(cfg.get("album_dir") or "config/persona_albums"))
        self.command_args = cfg.get("command_args")
        self.command_template = str(cfg.get("command_template") or "").strip()
        self.command_timeout_sec = float(cfg.get("command_timeout_sec", 180) or 180)

    def stats(self) -> Dict[str, Any]:
        return {"enabled": self.enabled, "backend": self.backend,
                "model": self.model, "out_dir": str(self.out_dir),
                "album_dir": str(self.album_dir)}

    async def generate(
        self, prompt: str, *, timeout_sec: Optional[float] = None,
        album_key: str = "", avoid_path: str = "", base_image: str = "",
    ) -> SelfieResult:
        """出图。``base_image`` 非空且存在 → img2img（openai images.edit / command ``{base}``），
        用于锁住人设一致性；album 后端忽略 prompt/base（只挑现成图）。"""
        rv = SelfieResult(prompt=str(prompt or ""), provider=self.backend)
        if not self.enabled or self.backend in ("", "disabled"):
            rv.error = "provider_disabled"
            return rv
        # album 后端：不出图，从预制相册挑一张已有照片（无需 prompt）。
        if self.backend == "album":
            return self._pick_from_album(album_key=album_key, avoid_path=avoid_path)
        if not rv.prompt.strip():
            rv.error = "empty_prompt"
            return rv
        # 外层 wait_for 是兜底：须严格大于 client/命令各自的请求超时，否则会在请求合法运行中途
        # 误砍，掩盖掉底层（client.timeout / command_timeout）的精确错误。取请求超时 + 15s 余量。
        inner = (self.command_timeout_sec if self.backend == "command"
                 else self.request_timeout_sec)
        eff_timeout = float(timeout_sec) if timeout_sec else float(inner) + 15.0
        self.out_dir.mkdir(parents=True, exist_ok=True)
        out = self.out_dir / f"selfie-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}.png"
        t0 = time.monotonic()
        try:
            await asyncio.wait_for(
                asyncio.to_thread(self._generate_sync, rv.prompt, out, base_image),
                timeout=eff_timeout,
            )
            if out.exists() and out.stat().st_size > 0:
                rv.ok = True
                rv.image_path = str(out)
                rv.extra["bytes"] = out.stat().st_size
                rv.extra["img2img"] = bool(base_image)
            else:
                rv.error = "empty_image"
        except asyncio.TimeoutError:
            rv.error = f"selfie_timeout({eff_timeout:.0f}s)"
        except Exception as ex:  # noqa: BLE001
            rv.error = f"{type(ex).__name__}: {ex}"
        rv.latency_ms = int((time.monotonic() - t0) * 1000)
        return rv

    def _album_dirs(self, album_key: str = "") -> list:
        """候选相册目录：优先 ``album_dir/<persona_key>``（多人设分册），回落 ``album_dir`` 根目录。

        ``album_key`` 只保留字母/数字（含 CJK）/``-``/``_``——挡掉路径分隔符与 ``..``，防目录穿越。
        """
        dirs: list = []
        key = "".join(c for c in str(album_key or "") if c.isalnum() or c in ("-", "_"))
        if key:
            dirs.append(self.album_dir / key)
        dirs.append(self.album_dir)
        return dirs

    def _list_album(self, album_key: str = "") -> list:
        """列出候选目录里第一个含图片的目录的所有图片路径（排序稳定）；无则空列表。"""
        for d in self._album_dirs(album_key):
            try:
                if d.is_dir():
                    files = sorted(
                        str(p) for p in d.iterdir()
                        if p.is_file() and p.suffix.lower() in _ALBUM_IMAGE_EXT
                    )
                    if files:
                        return files
            except Exception:  # noqa: BLE001
                continue
        return []

    def _pick_from_album(
        self, *, album_key: str = "", avoid_path: str = ""
    ) -> SelfieResult:
        """从预制相册随机挑一张（尽量避开上一张 ``avoid_path``，避免连发同图）。"""
        files = self._list_album(album_key)
        if not files:
            return SelfieResult(provider="album", error="album_empty")
        pool = [f for f in files if f != str(avoid_path or "")] or files
        pick = random.choice(pool)
        return SelfieResult(ok=True, image_path=pick, provider="album",
                            extra={"album_size": len(files)})

    def reference_image(self, album_key: str = "") -> str:
        """挑一张相册图当"基础图/锁脸参考"（openai/command 后端 img2img 用）；无相册回空串。

        让 album_dir 一物两用：album 后端直接发它，openai/command 后端拿它当 img2img 基础图，
        使生成的人设照片保持同一张脸。
        """
        files = self._list_album(album_key)
        return files[0] if files else ""

    def _generate_sync(self, prompt: str, out: Path, base_image: str = "") -> None:
        if self.backend == "openai":
            self._generate_openai(prompt, out, base_image)
            return
        if self.backend == "command":
            self._generate_command(prompt, out, base_image)
            return
        raise RuntimeError(f"unknown backend {self.backend}")

    def _generate_openai(self, prompt: str, out: Path, base_image: str = "") -> None:
        client = self._make_openai_client()
        if base_image and Path(base_image).is_file():
            out.write_bytes(self._openai_edit_bytes(client, prompt, base_image))
        else:
            out.write_bytes(self._openai_generate_bytes(client, prompt))

    def _make_openai_client(self) -> Any:
        """构造 OpenAI 客户端（独立测试缝：测试可 monkeypatch 本方法注入假 client）。"""
        from openai import OpenAI  # type: ignore
        if not self.api_key:
            raise RuntimeError("missing api_key for openai images")
        kwargs: Dict[str, Any] = {"api_key": self.api_key,
                                  "timeout": self.request_timeout_sec}
        if self.base_url:
            kwargs["base_url"] = self.base_url
        return OpenAI(**kwargs)

    def _openai_generate_bytes(self, client: Any, prompt: str) -> bytes:
        """调 images.generate 并取回 PNG 字节。model 感知 + b64/url 双回退。

        - ``gpt-image-1``：恒返回 b64（且**不接受** response_format 参数，传了会报错）。
        - ``dall-e-2/3``：默认返回 url；显式要 ``response_format=b64_json`` 才回 b64。
        - 兜底：拿不到 b64 但有 url → 下载 url（兼容自建/代理 images 网关行为差异）。
        """
        req: Dict[str, Any] = {"model": self.model, "prompt": prompt,
                               "size": self.size, "n": 1}
        if self.quality:
            req["quality"] = self.quality
        if self.model.startswith("dall-e"):
            req["response_format"] = "b64_json"
        return self._resp_to_bytes(client.images.generate(**req))

    def _openai_edit_bytes(self, client: Any, prompt: str, base_image: str) -> bytes:
        """基础图 img2img：调 images.edit（gpt-image-1 / dall-e-2 编辑接口），锁住人设一致性。

        传入基础图 + prompt，返回改写后的图；b64/url 双回退同 generate（dall-e-2 需 b64_json）。
        """
        req: Dict[str, Any] = {"model": self.model, "prompt": prompt,
                               "size": self.size, "n": 1}
        if self.model.startswith("dall-e"):
            req["response_format"] = "b64_json"
        with open(base_image, "rb") as fh:
            req["image"] = fh
            resp = client.images.edit(**req)
        return self._resp_to_bytes(resp)

    def _resp_to_bytes(self, resp: Any) -> bytes:
        """从 images 响应（generate/edit 通用）取 PNG 字节：优先 b64_json，回退下载 url。"""
        import base64 as _b64

        data = getattr(resp, "data", None) or []
        if not data:
            raise RuntimeError("openai images: empty response data")
        item = data[0]
        b64 = getattr(item, "b64_json", None) if not isinstance(item, dict) \
            else item.get("b64_json")
        if b64:
            return _b64.b64decode(b64)
        url = getattr(item, "url", None) if not isinstance(item, dict) \
            else item.get("url")
        if url:
            return self._download_image(str(url))
        raise RuntimeError("openai images: no b64_json/url in response")

    def _download_image(self, url: str) -> bytes:
        """下载远端图片（stdlib，不引依赖）；受 request_timeout_sec 约束。"""
        import urllib.request

        with urllib.request.urlopen(url, timeout=self.request_timeout_sec) as r:
            data = r.read()
        if not data:
            raise RuntimeError("openai images: empty download")
        return data

    def _generate_command(self, prompt: str, out: Path, base_image: str = "") -> None:
        raw_args = self.command_args
        tpl = self.command_template
        if not tpl and not isinstance(raw_args, list):
            raise RuntimeError("selfie command not configured")
        # {base}=基础图路径（img2img，空则为空串——脚本可据此决定 text2img/img2img）。
        values = {"prompt": prompt, "out": str(out), "base": str(base_image or "")}
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
