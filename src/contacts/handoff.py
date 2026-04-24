"""HandoffToken 签发/消费服务。

责任：
- 生成唯一 token，存数据库（碰撞自动重试）
- 默认 TTL 72h（由你指定）
- 消费必须原子，不能重复消费
- 过期 / 撤销 / 已消费都返回明确异常，方便上层区分失败原因

典型用法：
    svc = HandoffTokenService(store)
    tok = svc.issue(messenger_ci_id)             # Messenger 引流话术里嵌入 tok.token
    tok2 = svc.consume(raw_text, line_ci_id)     # LINE 收到的用户首条消息，里面有 token
    if tok2:
        merge_service.apply_token_merge(tok2, line_ci_id)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Iterable, List, Optional

from .ids import TOKEN_LENGTH, is_valid_token_shape, new_token
from .models import HandoffToken
from .store import ContactStore

logger = logging.getLogger(__name__)


# 默认 72 小时（按 v5 决策值）
DEFAULT_TTL_SECONDS = 72 * 3600

# token 碰撞时的重试次数（8.87 亿空间内碰撞几率极低，3 次已极保守）
MAX_INSERT_RETRY = 5


class TokenError(Exception):
    """Handoff token 相关错误基类。"""


class TokenNotFound(TokenError):
    pass


class TokenExpired(TokenError):
    pass


class TokenAlreadyConsumed(TokenError):
    pass


class TokenRevoked(TokenError):
    pass


# 在用户自由文本里抓 token：匹配字符集，允许前后是非字母数字边界
# 注意：目前只识别纯小写字符（token 就是纯小写），避免误把大写 ID 吞掉
_TOKEN_RE = re.compile(r"(?<![a-z0-9])([abcdefghjkmnpqrstuvwxyz23456789]{%d})(?![a-z0-9])" % TOKEN_LENGTH)


class HandoffTokenService:
    def __init__(self, store: ContactStore, *, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        self._store = store
        self._ttl = int(ttl_seconds)

    # ── 签发 ───────────────────────────────────────────────
    def issue(self, issued_from_ci_id: str) -> HandoffToken:
        """为某 Messenger ChannelIdentity 签发一个 token。"""
        now = self._store._now()  # noqa: SLF001 — 同包内读 now 是合理的
        expires_at = now + self._ttl
        last_exc: Optional[Exception] = None
        for _ in range(MAX_INSERT_RETRY):
            candidate = new_token()
            tok = HandoffToken(
                token=candidate,
                issued_from_ci_id=issued_from_ci_id,
                issued_at=now,
                expires_at=expires_at,
            )
            try:
                ok = self._store.insert_token(tok)
            except Exception as e:  # IntegrityError 在 store 层已吞掉返回 False
                last_exc = e
                continue
            if ok:
                logger.info("handoff_token issued: %s from_ci=%s ttl=%ds",
                            candidate, issued_from_ci_id, self._ttl)
                return tok
        raise TokenError(f"could not insert unique token after {MAX_INSERT_RETRY} retries: {last_exc}")

    # ── 消费 ───────────────────────────────────────────────
    def consume(self, token: str, *, consumed_by_ci_id: str) -> HandoffToken:
        """原子消费；成功返回已更新的 token，失败抛对应异常。"""
        if not is_valid_token_shape(token):
            raise TokenNotFound(token)
        existing = self._store.get_token(token)
        if not existing:
            raise TokenNotFound(token)
        if existing.is_revoked:
            raise TokenRevoked(token)
        if existing.is_consumed:
            raise TokenAlreadyConsumed(token)
        now = self._store._now()  # noqa: SLF001
        if existing.is_expired(now):
            raise TokenExpired(token)
        consumed = self._store.consume_token(token, consumed_by_ci_id=consumed_by_ci_id)
        if not consumed:
            # 竞态：在我们检查后被别的线程抢先消费/撤销/过期
            fresh = self._store.get_token(token)
            if fresh and fresh.is_consumed and fresh.consumed_by_ci_id != consumed_by_ci_id:
                raise TokenAlreadyConsumed(token)
            if fresh and fresh.is_revoked:
                raise TokenRevoked(token)
            if fresh and fresh.is_expired(now):
                raise TokenExpired(token)
            raise TokenError(f"consume failed unexpectedly: {token}")
        return consumed

    # ── 撤销 ───────────────────────────────────────────────
    def revoke(self, token: str, reason: str = "manual") -> bool:
        return self._store.revoke_token(token, reason=reason)

    # ── 辅助：从用户自由文本里抽取可能的 token 候选 ────────
    @staticmethod
    def extract_candidates(text: str) -> List[str]:
        """从一段文本里抓 token 样子的片段。可能返回空或多个。

        注意：这一步只判字面格式，不查数据库；真正有效性由 consume() 判定。
        """
        if not text:
            return []
        return _TOKEN_RE.findall(text.lower())

    def try_consume_from_text(self, text: str, *, consumed_by_ci_id: str) -> Optional[HandoffToken]:
        """从文本里找出第一个能成功消费的 token。

        约定：多个候选里只有一个能成功（其他的会抛 NotFound），
        静默吞掉 NotFound；其他异常（Expired/Consumed/Revoked）
        只记日志但不抛，方便调用方只关心"有没有真的合并成功"。
        """
        for candidate in self.extract_candidates(text):
            try:
                return self.consume(candidate, consumed_by_ci_id=consumed_by_ci_id)
            except TokenNotFound:
                continue
            except (TokenExpired, TokenAlreadyConsumed, TokenRevoked) as e:
                logger.info("token candidate not usable: %s (%s)", candidate, e.__class__.__name__)
                continue
        return None
