"""C0-1 离线授权（License）子系统。

私有化单实例商业化的地基：厂商用 Ed25519 私钥签发授权码，产品内置公钥离线验签。
本阶段（C0-1）**只读状态展示，不做功能强制**——gating 留给 C0-3，确保对既有部署零破坏。

对外入口：
- ``LicenseManager`` / ``get_license_manager`` —— 加载 + 验签 + 计算状态
- ``LicenseStatus`` —— 状态快照（state / plan / 到期 / 席位 / 渠道 / 功能位）
- ``issue_license`` / ``generate_keypair`` —— 厂商侧签发工具（scripts/license_tool.py 调用）
"""

from .gate import (
    READONLY_ALLOW_PREFIXES,
    WRITE_METHODS,
    channel_allowed,
    feature_allowed,
    is_write_blocked,
    seat_exceeded,
)
from .license_manager import (
    DEFAULT_VENDOR_PUBLIC_KEY_HEX,
    LicenseError,
    LicenseManager,
    LicenseStatus,
    configure_license_manager,
    generate_keypair,
    get_license_manager,
    issue_license,
    reset_license_manager,
)

__all__ = [
    "DEFAULT_VENDOR_PUBLIC_KEY_HEX",
    "LicenseError",
    "LicenseManager",
    "LicenseStatus",
    "configure_license_manager",
    "generate_keypair",
    "get_license_manager",
    "issue_license",
    "reset_license_manager",
    # gate
    "READONLY_ALLOW_PREFIXES",
    "WRITE_METHODS",
    "channel_allowed",
    "feature_allowed",
    "is_write_blocked",
    "seat_exceeded",
]
