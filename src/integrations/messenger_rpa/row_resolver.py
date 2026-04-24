"""给定 inbox 截图 + 目标联系人名，定位它在屏上的真实 row_index。

为什么需要：
  combined_vision / unread_fallback 输出的 row_index 其实是 LLM 推测，可能错位。
  本模块让 vision 只回答"X 在第几行 / 具体 Y"，任务单一、不易错。

用法：
  idx = await resolve_row_by_name(png_path, target_name, vision_cfg, global_vision)
  if idx is not None and 0 <= idx <= 5:
      use it
  else:
      fallback to原 row_index
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


ROW_RESOLVE_PROMPT = (
    "这是 Facebook Messenger Android Inbox 截图。\n"
    "目标联系人：**<NAME>**\n"
    "\n"
    "请**仅回答一个 JSON**（无 markdown，无多余文字）：\n"
    "  {\"row_index\": N, \"found\": true/false}\n"
    "\n"
    "其中 row_index 是从屏上**第一个会话行**（Stories 行之下、Inbox/Marketplace "
    "tab 之上）开始，**从 0 起**数到目标联系人所在行。\n"
    "\n"
    "规则：\n"
    "  - 严格按屏上从上到下排序数\n"
    "  - 如果同屏可见多个同名，取**最上面那个**\n"
    "  - 如果滚屏之外，found=false row_index=-1\n"
    "  - 如果看不清名字，found=false row_index=-1\n"
    "  - 屏上最多 6-7 行，row_index 应在 0..5"
)


async def resolve_row_by_name(
    image_path: str,
    target_name: str,
    *,
    vision_cfg: Dict[str, Any],
    global_vision: Dict[str, Any],
) -> Optional[int]:
    if not target_name.strip():
        return None
    try:
        from src.vision_client import VisionClient
    except Exception:
        return None
    prompt = ROW_RESOLVE_PROMPT.replace("<NAME>", target_name.strip())
    try:
        text, _tag = await VisionClient.describe_image_with_ollama_zhipu_fallback(
            merged_config=vision_cfg,
            global_vision=global_vision,
            image_path=image_path,
            prompt=prompt,
        )
    except Exception as ex:
        logger.debug("[row_resolver] vision 调用失败 err=%s", ex)
        return None

    if not text:
        return None
    text = text.strip()
    m = re.search(r"\{.*?\}", text, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except Exception:
        return None
    if not bool(obj.get("found")):
        return None
    try:
        idx = int(obj.get("row_index"))
    except Exception:
        return None
    if not (0 <= idx <= 5):
        return None
    return idx
