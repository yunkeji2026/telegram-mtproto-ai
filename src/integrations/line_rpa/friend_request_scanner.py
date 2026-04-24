"""LINE 好友申请扫描器。

职责范围（MVP 半自动）：
  1. 接收一张"添加好友"页面截图路径 + vision 调用器
  2. 用严格 prompt 引导多模态 LLM 返回 JSON 数组
  3. 解析 + 清洗，返回结构化 List[FriendRequest]

不做：
  - 不触发 adb tap（runner 在运营批准后决定什么时候点）
  - 不自动合并 Contact（等对方发首条消息后再由 ContactGateway.on_line_first_text 处理）
  - 不入库（W3 再做"申请审核队列"表 + Web UI）

这样 scanner 是纯函数行为，好测，真机集成不打坏现有 runner。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, List, Optional

logger = logging.getLogger(__name__)


# LLM 可能用 ``` 包 / 加前后缀话 / 中英混排，宽松点容错
_JSON_ARRAY_RE = re.compile(r"\[\s*(?:\{.*?\})(?:\s*,\s*\{.*?\})*\s*\]", re.DOTALL)

# 兜底：截屏里文字少，LLM 可能直接返 "[]" 或说明性中文。只认明确 JSON。


@dataclass
class FriendRequest:
    display_name: str
    hint_text: str = ""                  # 对方附言/签名
    raw: str = ""                        # 原始 JSON 片段（调试用）

    def to_dict(self) -> dict:
        return {
            "display_name": self.display_name,
            "hint_text": self.hint_text,
        }


# 严格 prompt：中文描述 + 规则 + 示例，降低 LLM 自由发挥带来的误识
_DEFAULT_PROMPT = """\
你看到的是一张 LINE 手机 App 的"添加好友"页面截图。请**只识别页面上"待处理的好友申请"（Received / 收到的申请 / 邀请）**，忽略以下内容：
- 已加的好友、你主动发出的申请
- "可能认识的人/推荐好友/通讯录好友"等推荐列表
- 群组邀请、公式账号（企业号）、官方账号

严格按下面的 JSON 数组格式返回，不要任何解释或 Markdown 代码块标记：

[{"display_name": "对方显示名", "hint_text": "对方的附言或个性签名，若无留空字符串"}]

规则：
1. display_name 必须是清晰可见的字符串，不要用"未知用户"或占位符代替
2. hint_text 只在明确有附言时填，推荐理由/共同好友提示不算附言
3. 如果页面上看不到"好友申请"条目（比如在其他页面或空列表），直接返回 []
4. 不要返回任何非 JSON 内容——不要前缀"以下是..."、不要 ```json``` 标记
"""


VisionCall = Callable[..., Awaitable[Optional[str]]]
"""vision 调用器：签名 (image_path, prompt=str) -> Awaitable[str|None]。

调用方传入 VisionClient.describe_image 的 bound method 即可。
"""


async def scan_friend_requests(
    image_path: str,
    vision_call: VisionCall,
    *,
    prompt_override: str = "",
) -> List[FriendRequest]:
    """扫一张截图，返回识别到的好友申请列表。

    失败/空结果返回 []，不抛异常——让调用方的 runner 继续走下一轮。
    """
    prompt = prompt_override.strip() or _DEFAULT_PROMPT
    try:
        text = await vision_call(image_path, prompt=prompt)
    except Exception as e:
        logger.warning("vision call failed in friend_request_scanner: %s", e)
        return []
    if not text:
        return []
    return parse_friend_requests(text)


def parse_friend_requests(vision_text: str) -> List[FriendRequest]:
    """从 vision 的纯文本输出里抽出 FriendRequest 列表。

    容错：
      - LLM 偶尔会带 Markdown 标记或前后缀话——regex 先抓第一个 JSON 数组
      - 每条记录必须有非空 display_name 才接受
      - hint_text 非字符串会被强制转字符串或丢弃
    """
    if not vision_text:
        return []
    raw = vision_text.strip()
    # 去掉 Markdown 代码块
    if raw.startswith("```"):
        lines = [ln for ln in raw.splitlines() if not ln.strip().startswith("```")]
        raw = "\n".join(lines).strip()

    # 优先直接解析
    payload: Any = None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        m = _JSON_ARRAY_RE.search(raw)
        if m:
            try:
                payload = json.loads(m.group(0))
            except json.JSONDecodeError:
                payload = None

    if not isinstance(payload, list):
        logger.debug("parse_friend_requests: not a list: %r", raw[:200])
        return []

    out: List[FriendRequest] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        name = str(item.get("display_name") or "").strip()
        if not name:
            continue
        # 规避占位符误识别
        if name.lower() in {"未知用户", "unknown", "n/a", "anonymous", "用户", "user"}:
            continue
        hint_raw = item.get("hint_text")
        hint = str(hint_raw).strip() if hint_raw not in (None, "") else ""
        out.append(FriendRequest(
            display_name=name,
            hint_text=hint,
            raw=json.dumps(item, ensure_ascii=False),
        ))
    return out
