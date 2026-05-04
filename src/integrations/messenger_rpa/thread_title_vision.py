"""``verify_thread_title`` 的 Vision 兜底。

某些设备（MIUI 一类把 ``uiautomator`` 进程 OOM/lowmemkill 的 ROM、或 USB
调试-安全设置受限的机型）上，``uiautomator dump`` 静默返回空，导致整条
RPA 链路在第一道安全网（U1 顶栏校验）就被卡死。

本模块用 ``screencap`` + 裁顶栏 + GLM-4V-Flash 读出 peer 名，作为 dump
失败时的最后一道防线。**只在 dump 失败时被调用**——健康设备零额外开销。

成本：一次发送 1 次 vision 调用（≈2-4s，token 极小），仅在故障路径产生。
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, Optional

from src.integrations.line_rpa import adb_helpers as adb

logger = logging.getLogger(__name__)

# Messenger 顶栏（含状态栏）在 720x1600 上约 200px = 12.5%。
# 留点 buffer 取 13%；设备纵横比变化也兜得住。
_TITLE_BAR_RATIO = 0.13
_MIN_CROP_HEIGHT = 60

_PROMPT = (
    "This is a Facebook Messenger app top navigation bar (cropped from a "
    "phone screenshot). Read the contact or group name displayed next to "
    "the avatar (under the back arrow). IGNORE status text such as "
    "'Active 5m ago' / '在线' / '上次活跃...'. "
    "Reply with STRICT JSON only, no markdown, no commentary:\n"
    "{\"title\":\"<exact name as shown, or empty string if no chat header visible>\"}"
)


@dataclass(frozen=True)
class VisionTitleResult:
    title: Optional[str]
    debug: str = ""


def screencap_top_strip(
    serial: str,
    *,
    top_ratio: float = _TITLE_BAR_RATIO,
    timeout: float = 22.0,
) -> Optional[Path]:
    """``adb exec-out screencap`` → PIL 裁顶栏 → 写到临时 PNG。

    成功返回临时文件路径（调用方负责删）；失败返 None。
    裁剪在本地做，传给 Vision 的图像更小，省 token + 减少误读底栏。
    """
    png_bytes, _err, code = adb.run_adb_binary(
        ["exec-out", "screencap", "-p"],
        serial=serial, timeout=timeout,
    )
    if code != 0 or not png_bytes or not png_bytes.startswith(b"\x89PNG"):
        logger.debug(
            "[thread_title_vision] screencap rc=%s len=%s",
            code, len(png_bytes) if png_bytes else 0,
        )
        return None

    def _mktmp() -> Path:
        fd, name = tempfile.mkstemp(prefix="mrpa_title_", suffix=".png")
        try:
            os.close(fd)
        except OSError:
            pass
        return Path(name)

    try:
        from PIL import Image
    except ImportError:
        logger.debug("[thread_title_vision] PIL 不可用，发整张截图")
        try:
            tmp = _mktmp()
            tmp.write_bytes(png_bytes)
            return tmp
        except OSError:
            return None

    try:
        img = Image.open(BytesIO(png_bytes))
        w, h = img.size
        crop_h = max(_MIN_CROP_HEIGHT, int(h * top_ratio))
        cropped = img.crop((0, 0, w, crop_h))
        tmp = _mktmp()
        cropped.save(tmp, format="PNG")
        return tmp
    except Exception as e:
        logger.debug("[thread_title_vision] crop 失败 %s；写整张原 PNG", e)
        try:
            tmp = _mktmp()
            tmp.write_bytes(png_bytes)
            return tmp
        except OSError:
            return None


_EXPLANATORY_PREFIX_RE = re.compile(
    # 关键约束：prefix 后必须跟标点（,/:/;）或继续解释词，避免把"Sure Tanaka"
    # 这种合法人名误杀。
    r"^("
    r"sure\s*[,:;]"                                    # "Sure," "Sure:"
    r"|sure\s+(here|the|i|based|looking|it|this|of\s+course)\b"
    r"|here(\s+(is|are)\b|'s\b|\s*[,:])"               # "Here is" "Here's" "Here,"
    r"|the\s+(title|chat|peer|name|chat\s+header)\s+(is|are|shows?)\b"
    r"|i\s+(see|think|believe|notice|can\s+see)\b"
    r"|based\s+on\b"
    r"|looking\s+at\b"
    r"|it\s+(is|appears|looks)\s+(like|to|that|a)\b"
    r"|this\s+(is|appears|chat\s+is|looks)\b"
    r")",
    re.IGNORECASE,
)
# LLM 把自己回答的元信息塞进文本（"as an AI"、"the chat title is"、"shown in the
# header"），这些含 self-reference 的字符串不可能是合法 peer 名。
_LLM_SELF_REF_RE = re.compile(
    r"\b(chat\s+header|the\s+title\s+is|peer\s+name\s+is|"
    r"shown\s+in\s+the|displayed\s+in\s+the|as\s+an\s+ai)\b",
    re.IGNORECASE,
)


def parse_title_response(text: str) -> Optional[str]:
    """把 LLM 返回解析成 peer 名。容错：JSON / 裸 JSON 字段 / 单行纯文本。"""
    if not text:
        return None
    s = text.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.MULTILINE)
    s = re.sub(r"\s*```\s*$", "", s, flags=re.MULTILINE)
    s = s.strip()

    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            t = obj.get("title")
            if isinstance(t, str):
                t = t.strip()
                return t or None
            # 合法 JSON dict 但 title 缺失/非字符串：直接拒绝，不落末尾"裸首行"
            # 兜底——否则 LLM 偶发回 "{}" 时会把字面 "{}" 当成 title。
            return None
    except json.JSONDecodeError:
        pass

    m = re.search(r'"title"\s*:\s*"([^"]*)"', s)
    if m:
        v = m.group(1).strip()
        return v or None

    lines = [ln.strip() for ln in s.splitlines() if ln.strip()]
    if lines and 0 < len(lines[0]) <= 80:
        candidate = lines[0]
        # 防御：JSON 字面 token / 解析残渣不是合法 title
        if candidate in {"{}", "[]", "null", "None", "undefined", "{ }", "[ ]"}:
            return None
        # 防御：LLM 解释性前缀（"Sure, the title is..." / "Here's: ..."），
        # 这些字符串包了真名也已经不是 peer 名本身——拒绝总比 wrong_chat 好。
        if _EXPLANATORY_PREFIX_RE.search(candidate):
            return None
        # 防御：LLM 自我描述／元信息（"the chat header"），罕见但出现过。
        if _LLM_SELF_REF_RE.search(candidate):
            return None
        return candidate
    return None


# 任务名——与 vision_task_models.VISION_TASKS 的 key 对齐
_TASK_NAME = "title_verify"


def read_thread_title_via_vision(
    serial: str,
    vision_cfg: Dict[str, Any],
    global_vision: Optional[Dict[str, Any]] = None,
    *,
    top_ratio: float = _TITLE_BAR_RATIO,
    cleanup: bool = True,
    task_name: str = _TASK_NAME,
) -> VisionTitleResult:
    """主入口：截图顶栏 → Vision 读 peer 名。

    同步实现，可在异步 runner 内安全调用（vision_client 的 sync 路径不依赖
    事件循环）。失败时 ``title=None`` + ``debug`` 标因。

    ``task_name`` 默认 ``"title_verify"`` → 通过 ``vision_task_models``
    解析对应推荐模型/超时（实测：flash 4-7s 准确，无需 plus）。
    """
    img_path = screencap_top_strip(serial, top_ratio=top_ratio)
    if img_path is None:
        return VisionTitleResult(title=None, debug="screencap_failed")

    from src.integrations.messenger_rpa.vision_task_models import cfg_for_task
    from src.integrations.messenger_rpa import vision_metrics as _vm
    import time as _t
    title_cfg = cfg_for_task(task_name, base_cfg=vision_cfg)

    # P6: 度量起点。所有 return 路径必须 record（含失败 & 异常）。
    _t0 = _t.time()
    _used_model: Optional[str] = title_cfg.get("model")
    _used_provider: Optional[str] = title_cfg.get("provider")

    def _emit(ok_flag: bool, error_class: Optional[str]) -> None:
        try:
            _vm.record(
                task_name=task_name,
                model=_used_model,
                api_provider=_used_provider,
                duration_ms=int((_t.time() - _t0) * 1000),
                ok=ok_flag,
                error_class=error_class,
            )
        except Exception:
            pass

    try:
        from src.vision_client import VisionClient

        vc = VisionClient(title_cfg)
        ok = vc.initialize()
        if not ok:
            gv = global_vision or {}
            zk = (gv.get("zhipu_api_key") or "").strip()
            if zk and zk != "YOUR_ZHIPU_API_KEY":
                # 主端起不来 → 切 zhipu，沿用任务表的模型选择
                z_cfg = cfg_for_task(
                    task_name,
                    base_cfg={
                        **title_cfg,
                        "provider": "zhipu",
                        "api_key": zk,
                    },
                )
                z_cfg.pop("base_url", None)
                vc = VisionClient(z_cfg)
                _used_model = z_cfg.get("model")
                _used_provider = z_cfg.get("provider")
                if not vc.initialize():
                    _emit(False, "vision_init_fail")
                    return VisionTitleResult(
                        title=None, debug="vision_init_fail",
                    )
            else:
                _emit(False, "vision_init_fail_no_zhipu")
                return VisionTitleResult(
                    title=None, debug="vision_init_fail_no_zhipu",
                )

        text = vc.describe_image_sync(str(img_path), prompt=_PROMPT)
        if not text:
            _emit(False, "vision_empty")
            return VisionTitleResult(title=None, debug="vision_empty")
        title = parse_title_response(text)
        if title is None:
            _emit(False, "parse_fail")
            return VisionTitleResult(
                title=None, debug=f"parse_fail:{text[:60]!r}",
            )
        _emit(True, None)
        return VisionTitleResult(title=title, debug="ok")
    except Exception as e:
        logger.debug("[thread_title_vision] vision call exc", exc_info=True)
        _emit(False, f"exc:{type(e).__name__}")
        return VisionTitleResult(title=None, debug=f"exc:{type(e).__name__}")
    finally:
        if cleanup and img_path is not None:
            try:
                img_path.unlink(missing_ok=True)
            except OSError:
                pass


__all__ = [
    "VisionTitleResult",
    "screencap_top_strip",
    "parse_title_response",
    "read_thread_title_via_vision",
]
