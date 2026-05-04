"""B2 + B5: per-chat persona binding e2e 测试。

测试目标：
  - state_store CRUD 接口正确
  - runner._pick_reply_profile 优先读 manual override
  - 多账号下 binding 隔离正确
  - 批量绑定接口
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.integrations.messenger_rpa.runner import MessengerRpaRunner
from src.integrations.messenger_rpa.state_store import MessengerRpaStateStore


@pytest.fixture
def store(tmp_path):
    """临时 sqlite state_store。"""
    db_path = tmp_path / "test_state.db"
    return MessengerRpaStateStore(str(db_path))


def _runner_with_real_store(store, cfg: Dict[str, Any]):
    """构造真 state_store 的 runner。"""
    r = object.__new__(MessengerRpaRunner)
    r._cfg = cfg
    r._self_skip_until = {}
    r._chat_key_prefix = "test"
    r._state = store
    return r


# ════════════════════════════════════════════════════════════════════
#  state_store CRUD
# ════════════════════════════════════════════════════════════════════

class TestChatPersonaOverrideStore:
    def test_upsert_and_get(self, store):
        ok = store.upsert_chat_persona_override(
            chat_name="Alice", reply_profile_id="warm_companion",
            account_id="acc1", bound_by="web_admin",
        )
        assert ok is True
        b = store.get_chat_persona_override("Alice", account_id="acc1")
        assert b is not None
        assert b["reply_profile_id"] == "warm_companion"
        assert b["bound_by"] == "web_admin"

    def test_account_id_scoped(self, store):
        """同 chat_name 在不同 account_id 下独立绑定。"""
        store.upsert_chat_persona_override(
            chat_name="Alice", reply_profile_id="profile_a", account_id="acc1",
        )
        store.upsert_chat_persona_override(
            chat_name="Alice", reply_profile_id="profile_b", account_id="acc2",
        )
        b1 = store.get_chat_persona_override("Alice", account_id="acc1")
        b2 = store.get_chat_persona_override("Alice", account_id="acc2")
        assert b1["reply_profile_id"] == "profile_a"
        assert b2["reply_profile_id"] == "profile_b"

    def test_get_fallback_to_global_account_empty(self, store):
        """当 account_id='X' 没绑定，但 account_id='' 全局有 → 返回全局。"""
        store.upsert_chat_persona_override(
            chat_name="Alice", reply_profile_id="global_default", account_id="",
        )
        b = store.get_chat_persona_override("Alice", account_id="any_account")
        assert b is not None
        assert b["reply_profile_id"] == "global_default"

    def test_remove(self, store):
        store.upsert_chat_persona_override(
            chat_name="Alice", reply_profile_id="foo", account_id="acc1",
        )
        assert store.get_chat_persona_override("Alice", "acc1") is not None
        removed = store.remove_chat_persona_override("Alice", "acc1")
        assert removed is True
        assert store.get_chat_persona_override("Alice", "acc1") is None

    def test_list(self, store):
        store.upsert_chat_persona_override(
            chat_name="A", reply_profile_id="p1", account_id="acc1",
        )
        store.upsert_chat_persona_override(
            chat_name="B", reply_profile_id="p2", account_id="acc1",
        )
        store.upsert_chat_persona_override(
            chat_name="C", reply_profile_id="p3", account_id="acc2",
        )
        all_b = store.list_chat_persona_overrides()
        assert len(all_b) == 3
        acc1_b = store.list_chat_persona_overrides(account_id="acc1")
        # acc1 应该只看到 acc1 的 + 全局空 account_id
        chat_names_acc1 = {b["chat_name"] for b in acc1_b}
        assert "A" in chat_names_acc1 and "B" in chat_names_acc1

    def test_batch_upsert(self, store):
        bindings = [
            {"chat_name": "A", "reply_profile_id": "p1", "account_id": "acc1"},
            {"chat_name": "B", "reply_profile_id": "p1", "account_id": "acc1"},
            {"chat_name": "C", "reply_profile_id": "p1", "account_id": "acc1"},
        ]
        n = store.batch_upsert_chat_persona_overrides(bindings)
        assert n == 3
        # 验证 3 个都绑定到同一 profile
        for name in ["A", "B", "C"]:
            b = store.get_chat_persona_override(name, "acc1")
            assert b["reply_profile_id"] == "p1"


# ════════════════════════════════════════════════════════════════════
#  runner._pick_reply_profile 优先级
# ════════════════════════════════════════════════════════════════════

class TestPickReplyProfileWithOverride:
    @pytest.fixture
    def cfg(self):
        return {
            "reply_profiles": {
                "default": "warm_companion",
                "profiles": [
                    {"id": "warm_companion", "persona": {"name": "Camille"}},
                    {
                        "id": "sato_takumi_test",
                        "match_names": ["Victor Zan"],
                        "persona": {"name": "佐藤拓海"},
                    },
                    {"id": "professional_support", "persona": {"name": "Pro"}},
                ],
            },
        }

    def test_no_override_falls_back_to_default(self, store, cfg):
        r = _runner_with_real_store(store, cfg)
        picked = r._pick_reply_profile("test:Random User", "Random User")
        assert picked["id"] == "warm_companion"  # default

    def test_override_takes_precedence(self, store, cfg):
        """运营手动绑定优先于 default + match_names。"""
        store.upsert_chat_persona_override(
            chat_name="Random User",
            reply_profile_id="professional_support",
            account_id="",
        )
        r = _runner_with_real_store(store, cfg)
        picked = r._pick_reply_profile("test:Random User", "Random User")
        assert picked["id"] == "professional_support", (
            "运营手动 binding 必须优先于 default"
        )

    def test_override_takes_precedence_over_match_names(self, store, cfg):
        """运营手动绑定优先于 match_names 自动匹配。"""
        store.upsert_chat_persona_override(
            chat_name="Victor Zan",
            reply_profile_id="warm_companion",  # 运营要 warm 而不是 sato
            account_id="",
        )
        r = _runner_with_real_store(store, cfg)
        picked = r._pick_reply_profile("test:Victor Zan", "Victor Zan")
        # 即使 match_names 命中 sato_takumi_test，运营 override 也优先
        assert picked["id"] == "warm_companion"

    def test_missing_profile_id_falls_back_to_match(self, store, cfg):
        """如果 override 的 profile_id 不存在，fallback 到自动匹配。"""
        store.upsert_chat_persona_override(
            chat_name="Victor Zan",
            reply_profile_id="non_existent_profile",
            account_id="",
        )
        r = _runner_with_real_store(store, cfg)
        picked = r._pick_reply_profile("test:Victor Zan", "Victor Zan")
        # match_names 命中 sato
        assert picked["id"] == "sato_takumi_test"

    def test_account_scoped_override(self, store, cfg):
        """不同账号对同名 chat 可绑定不同 profile。"""
        store.upsert_chat_persona_override(
            chat_name="Common User", reply_profile_id="warm_companion",
            account_id="acc_a",
        )
        store.upsert_chat_persona_override(
            chat_name="Common User", reply_profile_id="professional_support",
            account_id="acc_b",
        )
        # runner 模拟 acc_a 跑
        r_a = _runner_with_real_store(store, cfg)
        r_a._account_id = "acc_a"
        picked_a = r_a._pick_reply_profile("test:Common User", "Common User")
        assert picked_a["id"] == "warm_companion"
        # runner 模拟 acc_b 跑
        r_b = _runner_with_real_store(store, cfg)
        r_b._account_id = "acc_b"
        picked_b = r_b._pick_reply_profile("test:Common User", "Common User")
        assert picked_b["id"] == "professional_support"


# ════════════════════════════════════════════════════════════════════
#  多用户隔离防回归
# ════════════════════════════════════════════════════════════════════

class TestMultiUserIsolation:
    def test_default_warm_companion_for_new_users(self, store):
        """B1 修复防回归：default = warm_companion，新用户不再被当 victor。"""
        cfg = {
            "reply_profiles": {
                "default": "warm_companion",  # B1 修复
                "profiles": [
                    {"id": "warm_companion", "persona": {"name": "Camille"}},
                    {
                        "id": "sato_takumi_test",
                        "match_names": ["Victor Zan"],
                        "persona": {"name": "佐藤拓海"},
                    },
                ],
            },
        }
        r = _runner_with_real_store(store, cfg)
        # 新用户：random Chinese name
        picked = r._pick_reply_profile("test:张三", "张三")
        assert picked["id"] == "warm_companion", (
            "新加的中文用户必须用 warm_companion 而不是 sato_takumi_test"
        )

    def test_victor_still_uses_sato(self, store):
        """Victor 仍正确命中 sato_takumi_test（match_names）。"""
        cfg = {
            "reply_profiles": {
                "default": "warm_companion",
                "profiles": [
                    {"id": "warm_companion", "persona": {"name": "Camille"}},
                    {
                        "id": "sato_takumi_test",
                        "match_names": ["Victor Zan"],
                        "persona": {"name": "佐藤拓海"},
                    },
                ],
            },
        }
        r = _runner_with_real_store(store, cfg)
        picked = r._pick_reply_profile("test:Victor Zan", "Victor Zan")
        assert picked["id"] == "sato_takumi_test"
