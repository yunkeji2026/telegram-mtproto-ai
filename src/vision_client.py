"""
图像理解客户端：智谱 GLM-4V，或 OpenAI 兼容多模态（Ollama / 本地 Gemma 等）。
将图片转为文字描述，供下游 AI 生成回复。
"""

import asyncio
import base64
import logging
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

try:
    from zhipuai import ZhipuAI
    ZHIPU_AVAILABLE = True
except ImportError:
    ZHIPU_AVAILABLE = False
    ZhipuAI = None

try:
    from openai import OpenAI
    OPENAI_SYNC_AVAILABLE = True
except ImportError:
    OPENAI_SYNC_AVAILABLE = False
    OpenAI = None  # type: ignore


def _zhipu_credentials(global_vision: dict, merged: dict) -> Optional[Dict[str, str]]:
    """从全局 vision 或合并配置中取智谱 key（排除占位符与 ollama）。支持 zhipu_api_key 专用于回退。"""
    gv = global_vision if isinstance(global_vision, dict) else {}
    m = merged if isinstance(merged, dict) else {}
    for d in (gv, m):
        zk = (d.get("zhipu_api_key") or "").strip()
        if zk and zk not in ("YOUR_ZHIPU_API_KEY",):
            model = (
                d.get("zhipu_model")
                or gv.get("model")
                or m.get("model")
                or "glm-4v-flash"
            )
            return {"api_key": zk, "model": str(model)}
    for d in (gv, m):
        k = (d.get("api_key") or "").strip()
        if k and k not in ("YOUR_ZHIPU_API_KEY", "ollama"):
            model = gv.get("model") or m.get("model") or "glm-4v-flash"
            return {"api_key": k, "model": str(model)}
    return None


def _wants_openai_primary(merged: dict) -> bool:
    prov = (merged.get("provider") or "zhipu").strip().lower()
    if prov not in ("openai_compatible", "ollama", "openai", "local"):
        return False
    return bool((merged.get("base_url") or "").strip())


def has_any_vision_backend(merged: dict, global_vision: dict) -> bool:
    """至少存在一种可用后端：配置了 Ollama base_url，或存在有效智谱 api_key。"""
    if _wants_openai_primary(merged):
        return True
    gv = global_vision if isinstance(global_vision, dict) else {}
    return _zhipu_credentials(gv, merged) is not None


def _image_to_data_url(image_path: str, max_dim: Optional[int] = None) -> Optional[str]:
    """将本地图片转为 data URL（base64），供多模态 API 使用。
    max_dim: 若非 None，将图片缩放使最长边 ≤ max_dim，并以 JPEG 输出（减少本地 VLM 内存压力）。
    """
    path = Path(image_path)
    if not path.exists() or not path.is_file():
        return None
    try:
        raw = path.read_bytes()
        if len(raw) > 10 * 1024 * 1024:  # 10MB hard limit
            return None
        if max_dim is not None:
            try:
                import io
                from PIL import Image as _PILImage
                img = _PILImage.open(io.BytesIO(raw)).convert("RGB")
                w, h = img.size
                if max(w, h) > max_dim:
                    scale = max_dim / max(w, h)
                    img = img.resize((int(w * scale), int(h * scale)), _PILImage.LANCZOS)
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=85)
                raw = buf.getvalue()
                b64 = base64.b64encode(raw).decode("ascii")
                return f"data:image/jpeg;base64,{b64}"
            except Exception:
                pass  # fall through to original encoding
        b64 = base64.b64encode(raw).decode("ascii")
        suffix = path.suffix.lower()
        mime = "image/jpeg"
        if suffix in (".png",):
            mime = "image/png"
        elif suffix in (".gif",):
            mime = "image/gif"
        return f"data:{mime};base64,{b64}"
    except Exception:
        return None


class VisionClient:
    """图像理解：provider=zhipu（智谱）或 openai_compatible（Ollama 等）。"""

    def __init__(self, config: dict):
        self.config = config
        self._client: Any = None  # ZhipuAI
        self._oa_sync: Any = None  # OpenAI sync client
        self._backend: str = "zhipu"
        self.logger = logging.getLogger(__name__)

    def _get_zhipu(self) -> Optional[Any]:
        if not ZHIPU_AVAILABLE or not self._client:
            return None
        return self._client

    def initialize(self) -> bool:
        provider = (self.config.get("provider") or "zhipu").strip().lower()
        if provider in ("openai_compatible", "ollama", "openai", "local"):
            return self._initialize_openai_vision()
        return self._initialize_zhipu()

    def _initialize_openai_vision(self) -> bool:
        if not OPENAI_SYNC_AVAILABLE:
            self.logger.warning("openai 库未安装，Vision(Ollama) 不可用: pip install openai")
            return False
        raw_base = (self.config.get("base_url") or "").strip().rstrip("/")
        if not raw_base:
            self.logger.warning(
                "vision provider=openai_compatible 需要 base_url，例如 http://127.0.0.1:11434/v1"
            )
            return False
        if not raw_base.endswith("/v1"):
            raw_base = raw_base + "/v1"
        key = (self.config.get("api_key") or "ollama").strip()
        if key in ("", "YOUR_ZHIPU_API_KEY"):
            key = "ollama"
        timeout = float(self.config.get("timeout", 120))
        try:
            self._oa_sync = OpenAI(api_key=key, base_url=raw_base, timeout=timeout)
            self._backend = "openai"
            self.logger.info(
                "Vision(OpenAI 兼容) 初始化成功 base=%s model=%s",
                raw_base,
                self.config.get("model", "?"),
            )
            return True
        except Exception as e:
            self.logger.warning("Vision OpenAI 兼容初始化失败: %s", e)
            return False

    def _initialize_zhipu(self) -> bool:
        if not ZHIPU_AVAILABLE:
            self.logger.warning("zhipuai 未安装，Vision 不可用。请执行: pip install zhipuai")
            return False
        api_key = (self.config.get("api_key") or "").strip()
        if not api_key or api_key == "YOUR_ZHIPU_API_KEY":
            self.logger.warning("Vision 未配置 api_key，图像理解已禁用")
            return False
        try:
            self._client = ZhipuAI(api_key=api_key)
            self._backend = "zhipu"
            self.logger.info("智谱 GLM-4V Vision 客户端初始化成功")
            return True
        except Exception as e:
            self.logger.warning("智谱 Vision 初始化失败: %s", e)
            return False

    def describe_image_sync(self, image_path: str, prompt: Optional[str] = None) -> Optional[str]:
        """同步：根据本地图片路径得到文字描述。"""
        if self._backend == "openai":
            return self._describe_openai_sync(image_path, prompt)
        return self._describe_zhipu_sync(image_path, prompt)

    def _describe_openai_sync(
        self, image_path: str, prompt: Optional[str] = None
    ) -> Optional[str]:
        if not self._oa_sync:
            return None
        max_dim = self.config.get("max_image_dim")
        if max_dim is None:
            max_dim = 800  # default: resize to 800px max for local VLMs
        data_url = _image_to_data_url(image_path, max_dim=int(max_dim))
        if not data_url:
            self.logger.warning("图片转 base64 失败或文件过大")
            return None
        model = self.config.get("model", "llava")
        default_prompt = (
            "请简要描述图中与聊天/文字相关的内容；若是聊天截图，说明最后一条对方消息大意。"
        )
        text_prompt = (prompt or self.config.get("prompt") or default_prompt).strip()
        try:
            resp = self._oa_sync.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "image_url", "image_url": {"url": data_url}},
                            {"type": "text", "text": text_prompt},
                        ],
                    }
                ],
                max_tokens=int(self.config.get("max_tokens") or 300),
            )
            if resp and getattr(resp, "choices", None) and len(resp.choices) > 0:
                content = getattr(resp.choices[0].message, "content", None)
                if content and isinstance(content, str) and content.strip():
                    return content.strip()[:2000]
            return None
        except Exception as e:
            self.logger.warning("Ollama/本地多模态 Vision 调用失败: %s", e)
            return None

    def _describe_zhipu_sync(
        self, image_path: str, prompt: Optional[str] = None
    ) -> Optional[str]:
        client = self._get_zhipu()
        if not client:
            return None
        data_url = _image_to_data_url(image_path)
        if not data_url:
            self.logger.warning("图片转 base64 失败或文件过大")
            return None
        model = self.config.get("model", "glm-4v-flash")
        timeout = int(self.config.get("timeout", 30))
        default_prompt = (
            "请按以下格式描述，便于作为查单依据使用。"
            "1) 银行/账单类型：哪个银行或支付渠道（如 EasyPaisa、银行转账、平台订单等）。"
            "2) 唯一识别依据：能唯一标识该笔交易/订单的字段与取值（如 Transaction ID、订单号、参考号）。"
            "3) 其他：金额、币种、时间、付款方/收款方等。只写图中出现的内容，不要编造。"
        )
        text_prompt = (prompt or self.config.get("prompt") or default_prompt).strip()
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "image_url", "image_url": {"url": data_url}},
                            {"type": "text", "text": text_prompt},
                        ],
                    }
                ],
                max_tokens=1024,
                timeout=timeout,
            )
            if resp and getattr(resp, "choices", None) and len(resp.choices) > 0:
                content = getattr(resp.choices[0].message, "content", None)
                if content and isinstance(content, str) and content.strip():
                    return content.strip()[:2000]
            return None
        except Exception as e:
            self.logger.warning(f"智谱 Vision 调用失败: {e}")
            return None

    async def describe_image(self, image_path: str, prompt: Optional[str] = None) -> Optional[str]:
        """异步封装：在线程池中执行同步调用，避免阻塞事件循环。"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, lambda: self.describe_image_sync(image_path, prompt)
        )

    @classmethod
    async def describe_image_with_ollama_zhipu_fallback(
        cls,
        merged_config: dict,
        global_vision: dict,
        image_path: str,
        prompt: Optional[str] = None,
    ) -> Tuple[Optional[str], str]:
        """
        优先 Ollama/OpenAI 兼容端；初始化失败、调用失败或空结果时，若配置了智谱 key 则回退智谱。
        global_vision 用于在 line_rpa 覆盖 provider 时仍能读到全局 vision.api_key。
        """
        merged = dict(merged_config) if merged_config else {}
        gv = global_vision if isinstance(global_vision, dict) else {}

        if not _wants_openai_primary(merged):
            vc = cls(merged)
            if not vc.initialize():
                return None, "vision_client_init_fail"
            txt = await vc.describe_image(image_path, prompt=prompt)
            return txt, "zhipu_only" if vc._backend == "zhipu" else "vision_ok"

        vc_o = cls(merged)
        ollama_ok = vc_o.initialize()
        txt: Optional[str] = None
        if ollama_ok:
            txt = await vc_o.describe_image(image_path, prompt=prompt)
        dbg = "ollama_unavailable" if not ollama_ok else ("ollama_empty" if not (txt or "").strip() else "ollama_ok")

        if (txt or "").strip():
            return txt.strip(), dbg

        creds = _zhipu_credentials(gv, merged)
        if not creds:
            return None, dbg if not ollama_ok else "ollama_empty_no_zhipu_key"

        zcfg = {
            **merged,
            "provider": "zhipu",
            "api_key": creds["api_key"],
            "model": creds["model"],
        }
        zcfg.pop("base_url", None)
        vc_z = cls(zcfg)
        if not vc_z.initialize():
            return None, f"{dbg}|zhipu_init_fail"
        ztxt = await vc_z.describe_image(image_path, prompt=prompt)
        if (ztxt or "").strip():
            return ztxt.strip(), f"{dbg}|zhipu_fallback"
        return None, f"{dbg}|zhipu_empty"
