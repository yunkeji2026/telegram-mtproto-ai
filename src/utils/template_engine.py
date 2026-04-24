"""话术模板变量引擎 — 安全的 {variable} 插值"""

import re
import time
from typing import Any, Dict, Optional

_VAR_PATTERN = re.compile(r"\{(\w+)\}")

_BUILTIN_VARS = {
    "timestamp": lambda _: time.strftime("%Y-%m-%d %H:%M:%S"),
    "date": lambda _: time.strftime("%Y-%m-%d"),
    "time": lambda _: time.strftime("%H:%M"),
}


class SafeFormatDict(dict):
    """format_map 缺失 key 时返回原占位符而非报错"""
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def render_template(template: str, context: Optional[Dict[str, Any]] = None) -> str:
    if not template or "{" not in template:
        return template
    ctx = context or {}
    merged = SafeFormatDict()
    for k, fn in _BUILTIN_VARS.items():
        merged[k] = fn(ctx)
    for k, v in ctx.items():
        if isinstance(v, str):
            merged[k] = v
        elif v is not None:
            merged[k] = str(v)
    try:
        return template.format_map(merged)
    except (KeyError, ValueError, IndexError):
        return template


def extract_variables(template: str):
    return _VAR_PATTERN.findall(template)


def preview_template(template: str) -> str:
    """用示例值填充模板变量，便于预览"""
    examples = SafeFormatDict({
        "channel_name": "EasyPaisa",
        "fee_rate": "3.5%",
        "order_no": "123456789",
        "user_name": "张三",
        "amount": "10,000",
        "status": "active",
    })
    for k, fn in _BUILTIN_VARS.items():
        examples[k] = fn({})
    try:
        return template.format_map(examples)
    except Exception:
        return template
