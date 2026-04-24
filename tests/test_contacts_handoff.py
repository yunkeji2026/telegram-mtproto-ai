"""HandoffTokenService 单元测试。"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.contacts.store import ContactStore
from src.contacts.handoff import (
    HandoffTokenService,
    TokenNotFound,
    TokenExpired,
    TokenAlreadyConsumed,
    TokenRevoked,
)
from src.contacts.ids import TOKEN_LENGTH, is_valid_token_shape, new_token
from src.contacts.models import CHANNEL_MESSENGER, CHANNEL_LINE


@pytest.fixture
def store(tmp_path):
    s = ContactStore(db_path=tmp_path / "contacts.db")
    yield s
    s.close()


@pytest.fixture
def svc(store):
    return HandoffTokenService(store, ttl_seconds=3600)


@pytest.fixture
def messenger_ci(store):
    _, ci, _ = store.ensure_channel_identity(
        channel=CHANNEL_MESSENGER, account_id="acc-A", external_id="fb_100",
        display_name="Alice")
    return ci


@pytest.fixture
def line_ci(store):
    _, ci, _ = store.ensure_channel_identity(
        channel=CHANNEL_LINE, account_id="acc-A", external_id="line_xx",
        display_name="Alice")
    return ci


class TestIdsHelpers:
    def test_token_shape(self):
        t = new_token()
        assert len(t) == TOKEN_LENGTH
        assert is_valid_token_shape(t)
        # 去混淆字符不应出现
        for ch in "oil01":
            assert ch not in t

    def test_invalid_shapes(self):
        assert not is_valid_token_shape("")
        assert not is_valid_token_shape("abc")
        assert not is_valid_token_shape("o" * TOKEN_LENGTH)  # 'o' 不在字符集
        assert not is_valid_token_shape("A" * TOKEN_LENGTH)  # 大写不在字符集


class TestIssue:
    def test_issue_basic(self, svc, messenger_ci):
        t = svc.issue(messenger_ci.channel_identity_id)
        assert is_valid_token_shape(t.token)
        assert t.issued_from_ci_id == messenger_ci.channel_identity_id
        assert t.expires_at > t.issued_at

    def test_multiple_issues_unique(self, svc, messenger_ci):
        tokens = {svc.issue(messenger_ci.channel_identity_id).token for _ in range(50)}
        assert len(tokens) == 50

    def test_ttl_applied(self, store, messenger_ci):
        svc = HandoffTokenService(store, ttl_seconds=12345)
        t = svc.issue(messenger_ci.channel_identity_id)
        assert t.expires_at - t.issued_at == 12345


class TestConsume:
    def test_consume_happy_path(self, svc, messenger_ci, line_ci):
        tok = svc.issue(messenger_ci.channel_identity_id)
        out = svc.consume(tok.token, consumed_by_ci_id=line_ci.channel_identity_id)
        assert out.is_consumed
        assert out.consumed_by_ci_id == line_ci.channel_identity_id

    def test_consume_not_found(self, svc, line_ci):
        with pytest.raises(TokenNotFound):
            svc.consume("zzzzzz", consumed_by_ci_id=line_ci.channel_identity_id)

    def test_consume_bad_shape(self, svc, line_ci):
        with pytest.raises(TokenNotFound):
            svc.consume("bad", consumed_by_ci_id=line_ci.channel_identity_id)

    def test_consume_twice_raises(self, svc, messenger_ci, line_ci):
        tok = svc.issue(messenger_ci.channel_identity_id)
        svc.consume(tok.token, consumed_by_ci_id=line_ci.channel_identity_id)
        with pytest.raises(TokenAlreadyConsumed):
            svc.consume(tok.token, consumed_by_ci_id=line_ci.channel_identity_id)

    def test_consume_revoked(self, svc, messenger_ci, line_ci):
        tok = svc.issue(messenger_ci.channel_identity_id)
        svc.revoke(tok.token, reason="safety")
        with pytest.raises(TokenRevoked):
            svc.consume(tok.token, consumed_by_ci_id=line_ci.channel_identity_id)

    def test_consume_expired(self, store, messenger_ci, line_ci):
        svc = HandoffTokenService(store, ttl_seconds=1)
        tok = svc.issue(messenger_ci.channel_identity_id)
        # 直接把数据库里 expires_at 改到过去
        with store._lock:
            store._conn.execute(
                "UPDATE handoff_tokens SET expires_at=? WHERE token=?",
                (tok.issued_at - 10, tok.token),
            )
            store._conn.commit()
        with pytest.raises(TokenExpired):
            svc.consume(tok.token, consumed_by_ci_id=line_ci.channel_identity_id)


class TestExtractFromText:
    def test_extract_in_chinese_sentence(self, svc, messenger_ci):
        tok = svc.issue(messenger_ci.channel_identity_id)
        text = f"我加你啦～暗号是 {tok.token}，你认一下"
        cands = svc.extract_candidates(text)
        assert tok.token in cands

    def test_extract_multiple(self, svc):
        text = "abcdef ghjkmn 23456789"
        cands = svc.extract_candidates(text)
        assert "abcdef" in cands
        assert "ghjkmn" in cands
        # 8 位纯数字不匹配（长度不同）
        assert "23456789" not in cands

    def test_try_consume_from_text_success(self, svc, messenger_ci, line_ci):
        tok = svc.issue(messenger_ci.channel_identity_id)
        out = svc.try_consume_from_text(
            f"hi 我是小A 暗号 {tok.token}",
            consumed_by_ci_id=line_ci.channel_identity_id,
        )
        assert out is not None and out.token == tok.token

    def test_try_consume_from_text_none_when_no_match(self, svc, line_ci):
        assert svc.try_consume_from_text(
            "这段话没有暗号",
            consumed_by_ci_id=line_ci.channel_identity_id,
        ) is None

    def test_try_consume_skips_expired_and_returns_none(self, store, messenger_ci, line_ci):
        svc = HandoffTokenService(store, ttl_seconds=60)
        tok = svc.issue(messenger_ci.channel_identity_id)
        with store._lock:
            store._conn.execute(
                "UPDATE handoff_tokens SET expires_at=? WHERE token=?",
                (tok.issued_at - 10, tok.token),
            )
            store._conn.commit()
        # 过期的 token 出现在文本里，try_consume 应返回 None 不抛
        assert svc.try_consume_from_text(
            f"我加你了 {tok.token}",
            consumed_by_ci_id=line_ci.channel_identity_id,
        ) is None


class TestActiveTokens:
    def test_list_active(self, svc, store, messenger_ci):
        t1 = svc.issue(messenger_ci.channel_identity_id)
        t2 = svc.issue(messenger_ci.channel_identity_id)
        svc.revoke(t1.token, reason="test")
        actives = store.list_active_tokens_issued_from(messenger_ci.channel_identity_id)
        active_tokens = {t.token for t in actives}
        assert t2.token in active_tokens
        assert t1.token not in active_tokens
