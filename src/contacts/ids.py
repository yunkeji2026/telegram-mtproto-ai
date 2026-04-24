"""ID 与 Token 生成工具。

- 实体 ID：uuid4.hex（32 字符），时间排序靠表的 ts 列
- HandoffToken：6 字符 a-z0-9，去掉易混淆字符 (o/0/i/1/l)
"""

from __future__ import annotations

import secrets
import uuid

# 去掉视觉易混淆的字符，方便用户口述/手输
_TOKEN_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # 31 个
TOKEN_LENGTH = 6  # 31^6 ≈ 8.87 亿，碰撞可忽略


def new_id() -> str:
    """生成实体 ID（uuid4 hex 去横线）。"""
    return uuid.uuid4().hex


def new_token(length: int = TOKEN_LENGTH) -> str:
    """生成一个候选 HandoffToken。

    注意：这只是候选值，实际写入前必须在同一事务里检查唯一性，
    重试由 HandoffTokenService 负责。
    """
    return "".join(secrets.choice(_TOKEN_ALPHABET) for _ in range(length))


def is_valid_token_shape(s: str) -> bool:
    """快速检查一个字符串是否符合 token 字面格式（6 位 + 字符集内）。"""
    if not s or len(s) != TOKEN_LENGTH:
        return False
    return all(ch in _TOKEN_ALPHABET for ch in s)
