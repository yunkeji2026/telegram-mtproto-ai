"""Bot 指令帮助页数据（stable section/cmd keys → web_i18n 翻译）。

分区 ``tier`` 驱动图标 CSS / simple 模式 ``pro-only`` 隐藏，不再依赖中文分区名做逻辑判断。
"""
from __future__ import annotations

from typing import Any

# tier: basic | channel | admin | schedule | ai | general
HELP_SECTIONS: list[dict[str, Any]] = [
    {
        "key": "scripts",
        "tier": "basic",
        "title_key": "hp_sec_scripts",
        "commands": [
            {
                "cmd": "/setgreeting",
                "desc_key": "hp_cmd_setgreeting",
                "example_key": "hp_ex_setgreeting",
                "role": "admin",
            },
            {
                "cmd": "/setrate",
                "desc_key": "hp_cmd_setrate",
                "example_key": "hp_ex_setrate",
                "role": "admin",
            },
            {
                "cmd": "/status",
                "desc_key": "hp_cmd_status",
                "example_key": "hp_ex_status",
                "role": None,
            },
            {
                "cmd": "/help",
                "desc_key": "hp_cmd_help",
                "example_key": "hp_ex_help",
                "role": None,
            },
        ],
    },
]

_PRO_TIERS = frozenset({"admin", "schedule", "ai"})


def get_help_sections() -> list[dict[str, Any]]:
    """Return help accordion sections (mutable copy safe for templates)."""
    return [dict(s, commands=[dict(c) for c in s["commands"]]) for s in HELP_SECTIONS]


def section_is_pro_only(tier: str) -> bool:
    return tier in _PRO_TIERS
