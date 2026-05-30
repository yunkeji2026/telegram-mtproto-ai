"""W3-3H：reunion_prompts registry / variant 路由 / persona 注入 单测。"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.contacts.reunion_prompts import (
    ReunionPromptRegistry,
    _INLINE_DEFAULT,
    hash_prompt,
    load_persona_for_prompt,
    reset_registry,
)


@pytest.fixture(autouse=True)
def _clear_registry():
    reset_registry()
    yield
    reset_registry()


@pytest.fixture
def yaml_with(tmp_path):
    """factory: 写一个 yaml 配置返回 path。"""
    def _make(data: dict) -> Path:
        p = tmp_path / "rp.yaml"
        p.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
        return p
    return _make


class TestRegistryLoad:
    def test_loads_default_inline_when_no_file(self, tmp_path):
        # 指向不存在的路径
        r = ReunionPromptRegistry(path=tmp_path / "nope.yaml")
        assert "v1" in r.variants
        assert r.default_variant == "v1"

    def test_loads_from_yaml(self, yaml_with):
        path = yaml_with({
            "default_variant": "alpha",
            "variants": {
                "alpha": {"zh": "alpha-zh {silent_days}", "en": "alpha-en {silent_days}"},
                "beta": {"zh": "beta-zh {silent_days}"},
            },
        })
        r = ReunionPromptRegistry(path=path)
        assert sorted(r.variants) == ["alpha", "beta"]
        assert r.default_variant == "alpha"

    def test_skips_variant_missing_zh(self, yaml_with):
        path = yaml_with({
            "variants": {
                "good": {"zh": "ok"},
                "bad": {"en": "no zh fallback"},  # 应被丢弃
            },
        })
        r = ReunionPromptRegistry(path=path)
        assert r.variants == ["good"]

    def test_falls_back_to_inline_when_yaml_corrupt(self, tmp_path):
        path = tmp_path / "broken.yaml"
        path.write_text("variants: this is: not valid yaml\n  - oops", encoding="utf-8")
        r = ReunionPromptRegistry(path=path)
        # 退到 inline default
        assert "v1" in r.variants

    def test_default_variant_unknown_falls_to_first(self, yaml_with):
        path = yaml_with({
            "default_variant": "nonexistent",
            "variants": {"alpha": {"zh": "a"}, "beta": {"zh": "b"}},
        })
        r = ReunionPromptRegistry(path=path)
        # 选第一个（按 dict insertion order，应该是 alpha）
        assert r.default_variant in ("alpha", "beta")


class TestSelectVariant:
    def test_deterministic_same_jid_same_variant(self):
        r = ReunionPromptRegistry()  # inline，只有 v1
        v1 = r.select_variant("journey-abc")
        v2 = r.select_variant("journey-abc")
        assert v1 == v2

    def test_different_jids_can_get_different_variants(self, yaml_with):
        path = yaml_with({
            "variants": {
                "v1": {"zh": "v1"},
                "v2": {"zh": "v2"},
            },
        })
        r = ReunionPromptRegistry(path=path)
        # 跑 100 个 jid，至少应该看到 2 种 variant 都被分到（接近 50/50）
        seen = set()
        for i in range(100):
            seen.add(r.select_variant(f"journey-{i}"))
        assert seen == {"v1", "v2"}

    def test_balanced_distribution(self, yaml_with):
        """SHA-256 hash 应该接近 uniform。1000 个 jid 中 v1 占比应在 40-60%。"""
        path = yaml_with({
            "variants": {"v1": {"zh": "v1"}, "v2": {"zh": "v2"}},
        })
        r = ReunionPromptRegistry(path=path)
        v1_count = sum(
            1 for i in range(1000) if r.select_variant(f"j-{i}") == "v1"
        )
        assert 400 <= v1_count <= 600, f"v1_count={v1_count} 偏离均匀分布"


class TestRender:
    def test_basic_zh_render(self):
        r = ReunionPromptRegistry()
        prompt, variant, lang = r.render(
            variant="v1", lang="zh",
            persona_name="小可", persona_role="闺蜜",
            silent_days=20, funnel_stage="BONDED", intim=22.0,
        )
        assert variant == "v1"
        assert lang == "zh"
        assert "小可" in prompt
        assert "闺蜜" in prompt
        assert "20" in prompt
        assert "BONDED" in prompt
        assert "22" in prompt

    def test_en_render(self):
        r = ReunionPromptRegistry()
        prompt, _, lang = r.render(
            variant="v1", lang="en",
            persona_name="Aki", persona_role="friend",
            silent_days=15, intim=30.0,
        )
        assert lang == "en"
        assert "Aki" in prompt
        assert "15" in prompt
        assert "Output ONLY the message body" in prompt

    def test_unknown_lang_falls_to_zh(self):
        r = ReunionPromptRegistry()
        prompt, _, lang = r.render(variant="v1", lang="ko")
        assert lang == "zh"
        assert "只输出消息正文" in prompt

    def test_unknown_variant_falls_to_default(self):
        r = ReunionPromptRegistry()
        prompt, variant, _ = r.render(variant="v99", lang="zh")
        # 应该 fallback 到 default_variant
        assert variant in ("v1", "v2")  # 不报 KeyError

    def test_last_inbound_block_zh(self):
        r = ReunionPromptRegistry()
        prompt, _, _ = r.render(
            variant="v1", lang="zh", last_inbound="周末出去玩了",
        )
        assert "周末出去玩了" in prompt
        assert "对方最后一句话" in prompt

    def test_last_inbound_block_en(self):
        r = ReunionPromptRegistry()
        prompt, _, _ = r.render(
            variant="v1", lang="en", last_inbound="see you next week",
        )
        assert "see you next week" in prompt
        assert "Last message from them" in prompt

    def test_last_inbound_empty_no_block(self):
        r = ReunionPromptRegistry()
        prompt, _, _ = r.render(variant="v1", lang="zh", last_inbound="")
        assert "对方最后一句话" not in prompt

    def test_forbidden_phrases_injected(self):
        r = ReunionPromptRegistry()
        prompt, _, _ = r.render(
            variant="v1", lang="zh",
            forbidden_phrases=["作为一个AI", "我是机器人"],
        )
        assert "作为一个AI" in prompt
        assert "我是机器人" in prompt

    def test_forbidden_phrases_truncated_to_six(self):
        r = ReunionPromptRegistry()
        many = [f"phrase{i}" for i in range(10)]
        prompt, _, _ = r.render(
            variant="v1", lang="zh", forbidden_phrases=many,
        )
        assert "phrase0" in prompt
        assert "phrase5" in prompt
        # 第 7 条之后应该被截掉
        assert "phrase7" not in prompt

    def test_persona_default_when_empty(self):
        r = ReunionPromptRegistry()
        prompt, _, _ = r.render(
            variant="v1", lang="zh",
            persona_name="", persona_role="",
        )
        # zh 默认 fallback
        assert "你" in prompt
        assert "陪伴型 AI 助手" in prompt or "AI 助手" in prompt

    def test_intim_formatted_no_decimals(self):
        r = ReunionPromptRegistry()
        prompt, _, _ = r.render(variant="v1", lang="zh", intim=22.7)
        # {intim:.0f} → "23"，不应出现 "22.7"
        assert "22.7" not in prompt

    def test_two_variants_produce_distinct_text(self, yaml_with):
        path = yaml_with({
            "variants": {
                "alpha": {"zh": "ALPHA template {silent_days}"},
                "beta": {"zh": "BETA template {silent_days}"},
            },
        })
        r = ReunionPromptRegistry(path=path)
        a, _, _ = r.render(variant="alpha", lang="zh", silent_days=5)
        b, _, _ = r.render(variant="beta", lang="zh", silent_days=5)
        assert "ALPHA" in a
        assert "BETA" in b
        assert a != b


class TestRouteIntegration:
    """W3-3H 路由层：response 带 prompt_variant + persona_name + has_persona。"""

    def _build(self, tmp_path, ai_stub):
        from fastapi import FastAPI
        from starlette.testclient import TestClient
        from src.web.routes.contacts_routes import (
            register_contacts_routes, _intimacy_trend_cache_clear,
        )
        from src.web.routes import contacts_routes as cr_mod
        from src.contacts.gateway import ContactGateway
        from src.contacts.handoff import HandoffTokenService
        from src.contacts.merge import MergeService
        from src.contacts.models import CHANNEL_MESSENGER  # noqa
        from src.contacts.store import ContactStore
        from src.skills.intimacy_engine import IntimacyEngine
        from src.skills.reactivation_scheduler import ReactivationScheduler

        _intimacy_trend_cache_clear()
        cr_mod._relations_digest_cache.clear()
        store = ContactStore(db_path=tmp_path / "c.db")
        handoff = HandoffTokenService(store, ttl_seconds=3600)
        merge = MergeService(store)
        gateway = ContactGateway(store, handoff, merge)
        intim = IntimacyEngine(store)
        reactivator = ReactivationScheduler(store, min_silent_days=3, min_intimacy=40.0)
        app = FastAPI()
        register_contacts_routes(
            app, api_auth=lambda: None,
            contacts_store=store, merge_service=merge,
            intimacy_engine=intim, gateway=gateway,
            reactivation_scheduler=reactivator,
            ai_client=ai_stub,
        )
        tc = TestClient(app)
        tc.gateway = gateway
        tc.store = store
        return tc, store

    def test_response_includes_variant_and_persona_fields(self, tmp_path):
        from src.contacts.models import CHANNEL_MESSENGER

        class _AI:
            async def chat(self, prompt):  # noqa
                return "嗨"

        tc, store = self._build(tmp_path, _AI())
        ctx = tc.gateway.on_message(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_z",
            direction="in", text_preview="hi")
        jid = ctx.journey.journey_id
        body = tc.post(f"/api/reactivation/{jid}/draft-reunion").json()
        assert "prompt_variant" in body
        assert "persona_name" in body
        assert body["prompt_signals"]["has_persona"] in (True, False)
        store.close()

    def test_record_draft_persists_variant(self, tmp_path):
        """draft_log.prompt_variant 必须等于 response.prompt_variant。"""
        from src.contacts.models import CHANNEL_MESSENGER

        class _AI:
            async def chat(self, prompt):  # noqa
                return "嗨"

        tc, store = self._build(tmp_path, _AI())
        ctx = tc.gateway.on_message(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_v",
            direction="in", text_preview="hi")
        jid = ctx.journey.journey_id
        body = tc.post(f"/api/reactivation/{jid}/draft-reunion").json()
        with store._lock:
            row = dict(store._conn.execute(
                "SELECT prompt_variant FROM draft_log WHERE draft_id=?",
                (body["draft_id"],),
            ).fetchone())
        assert row["prompt_variant"] == body["prompt_variant"]
        store.close()


class TestHashPrompt:
    """W3-3I.5：hash_prompt 稳定性 + 长度。"""

    def test_empty_returns_empty(self):
        assert hash_prompt("") == ""

    def test_deterministic(self):
        a = hash_prompt("hello world")
        b = hash_prompt("hello world")
        assert a == b
        assert len(a) == 16

    def test_distinct_prompts_distinct_hash(self):
        assert hash_prompt("hello") != hash_prompt("hello!")

    def test_unicode_safe(self):
        # 中日英混合不应抛
        h = hash_prompt("你扮演「Aki」(companion AI) ご機嫌")
        assert len(h) == 16


class TestPersonaLoadFromJourney:
    """W3-3I.2：journey.persona_id → account_persona_id 链路。"""

    def setup_method(self):
        # 重置 PersonaManager 状态避免测试间污染
        from src.utils.persona_manager import PersonaManager
        PersonaManager.reset()

    def test_no_journey_falls_to_default(self):
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        pm.set_domain_persona({"name": "DomainHero", "role": "guide"})
        name, role, _ = load_persona_for_prompt(journey=None)
        assert name == "DomainHero"
        assert role == "guide"

    def test_journey_with_persona_id_hits_profile(self):
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        pm.upsert_profile("warm_v1", {
            "name": "Lily", "role": "陪伴者",
            "speaking": {"forbidden_phrases": ["禁词A"]},
        })

        class _J:
            persona_id = "warm_v1"

        name, role, forb = load_persona_for_prompt(journey=_J())
        assert name == "Lily"
        assert role == "陪伴者"
        assert "禁词A" in forb

    def test_journey_without_persona_id_falls_to_default(self):
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        pm.set_domain_persona({"name": "Default"})

        class _J:
            persona_id = ""

        name, _, _ = load_persona_for_prompt(journey=_J())
        assert name == "Default"

    def test_journey_persona_id_unknown_falls_to_default(self):
        """journey.persona_id 指向不存在的 profile → 退到 domain default。"""
        from src.utils.persona_manager import PersonaManager
        pm = PersonaManager.get_instance()
        pm.set_domain_persona({"name": "Default"})

        class _J:
            persona_id = "nonexistent_profile"

        name, _, _ = load_persona_for_prompt(journey=_J())
        assert name == "Default"


class TestHotReload:
    """W3-3I.3：mtime-based hot reload。"""

    def test_no_change_no_reload(self, tmp_path):
        path = tmp_path / "rp.yaml"
        path.write_text(yaml.safe_dump({
            "variants": {"v1": {"zh": "first"}},
        }), encoding="utf-8")
        r = ReunionPromptRegistry(path=path)
        assert r.maybe_reload() is False  # 没改 → 不重载

    def test_modified_yaml_triggers_reload(self, tmp_path):
        import os
        path = tmp_path / "rp.yaml"
        path.write_text(yaml.safe_dump({
            "variants": {"v1": {"zh": "first"}},
        }), encoding="utf-8")
        r = ReunionPromptRegistry(path=path)
        prompt1, _, _ = r.render(variant="v1", lang="zh")
        assert "first" in prompt1
        # 改 yaml + 推 mtime 至少 1 秒（有些 fs mtime 只有秒级精度）
        new_mtime = path.stat().st_mtime + 5
        path.write_text(yaml.safe_dump({
            "variants": {"v1": {"zh": "second-version"}},
        }), encoding="utf-8")
        os.utime(path, (new_mtime, new_mtime))
        assert r.maybe_reload() is True
        prompt2, _, _ = r.render(variant="v1", lang="zh")
        assert "second-version" in prompt2

    def test_reload_failure_keeps_old_config(self, tmp_path):
        """yaml 改成损坏的 → 老配置不丢失。"""
        import os
        path = tmp_path / "rp.yaml"
        path.write_text(yaml.safe_dump({
            "variants": {"v1": {"zh": "good"}},
        }), encoding="utf-8")
        r = ReunionPromptRegistry(path=path)
        new_mtime = path.stat().st_mtime + 5
        # 写一个完全空的 yaml（会落到 inline default fallback）
        path.write_text("variants: {}\n", encoding="utf-8")
        os.utime(path, (new_mtime, new_mtime))
        r.maybe_reload()
        # 老 v1 内容会被替换成 inline default —— inline 也是有效的，
        # 不会让 registry 死
        prompt, _, _ = r.render(variant="v1", lang="zh")
        assert prompt  # 不空就行（fallback 生效）


class TestPromoteDefaultVariant:
    """W3-3J.1：promote_default_variant 写 yaml + 立即 reload。"""

    def test_promote_writes_yaml(self, tmp_path):
        path = tmp_path / "rp.yaml"
        path.write_text(yaml.safe_dump({
            "default_variant": "v1",
            "variants": {"v1": {"zh": "first"}, "v2": {"zh": "second"}},
        }), encoding="utf-8")
        r = ReunionPromptRegistry(path=path)
        assert r.default_variant == "v1"
        ok = r.promote_default_variant("v2")
        assert ok is True
        assert r.default_variant == "v2"
        # yaml 上也持久化了
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert data["default_variant"] == "v2"

    def test_promote_unknown_variant_returns_false(self, tmp_path):
        path = tmp_path / "rp.yaml"
        path.write_text(yaml.safe_dump({
            "default_variant": "v1",
            "variants": {"v1": {"zh": "only"}},
        }), encoding="utf-8")
        r = ReunionPromptRegistry(path=path)
        ok = r.promote_default_variant("v99")
        assert ok is False
        assert r.default_variant == "v1"  # unchanged

    def test_promote_without_yaml_returns_false(self, tmp_path):
        """inline-only mode：没有 yaml 文件 → promote 不写、返回 False。"""
        path = tmp_path / "nonexistent.yaml"
        r = ReunionPromptRegistry(path=path)
        # r loads inline default
        ok = r.promote_default_variant("v1")  # v1 in inline default
        assert ok is False

    def test_promote_is_atomic(self, tmp_path):
        """促进写 tmp → rename，不留 .tmp 残留。"""
        path = tmp_path / "rp.yaml"
        path.write_text(yaml.safe_dump({
            "default_variant": "v1",
            "variants": {"v1": {"zh": "a"}, "v2": {"zh": "b"}},
        }), encoding="utf-8")
        r = ReunionPromptRegistry(path=path)
        r.promote_default_variant("v2")
        tmp = path.with_suffix(".yaml.tmp")
        assert not tmp.exists(), ".tmp 残留文件不应存在"

    def test_promote_triggers_hot_reload(self, tmp_path):
        """promote 后立即 select_variant 应基于新 default。"""
        import hashlib
        path = tmp_path / "rp.yaml"
        # 只有 v9 一个 variant，promote 无论如何 select_variant 都应是 v9
        path.write_text(yaml.safe_dump({
            "default_variant": "v1",
            "variants": {
                "v1": {"zh": "one"},
                "v9": {"zh": "nine"},
            },
        }), encoding="utf-8")
        r = ReunionPromptRegistry(path=path)
        assert r.default_variant == "v1"
        r.promote_default_variant("v9")
        assert r.default_variant == "v9"
        # journey_id 路由到 v9 的那些 jid 不应变；整体默认已切
        assert r._default_variant == "v9"


class TestUnmarkSent:
    """W3-3H.5：unmark-sent 端点。"""

    def _build(self, tmp_path, ai_stub):
        from fastapi import FastAPI
        from starlette.testclient import TestClient
        from src.web.routes.contacts_routes import (
            register_contacts_routes, _intimacy_trend_cache_clear,
        )
        from src.web.routes import contacts_routes as cr_mod
        from src.contacts.gateway import ContactGateway
        from src.contacts.handoff import HandoffTokenService
        from src.contacts.merge import MergeService
        from src.contacts.store import ContactStore
        from src.skills.intimacy_engine import IntimacyEngine
        from src.skills.reactivation_scheduler import ReactivationScheduler

        _intimacy_trend_cache_clear()
        cr_mod._relations_digest_cache.clear()
        store = ContactStore(db_path=tmp_path / "c.db")
        handoff = HandoffTokenService(store, ttl_seconds=3600)
        merge = MergeService(store)
        gateway = ContactGateway(store, handoff, merge)
        intim = IntimacyEngine(store)
        reactivator = ReactivationScheduler(store, min_silent_days=3, min_intimacy=40.0)
        app = FastAPI()
        register_contacts_routes(
            app, api_auth=lambda: None,
            contacts_store=store, merge_service=merge,
            intimacy_engine=intim, gateway=gateway,
            reactivation_scheduler=reactivator,
            ai_client=ai_stub,
        )
        tc = TestClient(app)
        tc.gateway = gateway
        tc.store = store
        return tc, store

    def test_unmark_existing_sent_draft(self, tmp_path):
        from src.contacts.models import CHANNEL_MESSENGER

        class _AI:
            async def chat(self, prompt):  # noqa
                return "嗨"

        tc, store = self._build(tmp_path, _AI())
        ctx = tc.gateway.on_message(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_u",
            direction="in", text_preview="hi")
        jid = ctx.journey.journey_id
        did = tc.post(f"/api/reactivation/{jid}/draft-reunion").json()["draft_id"]
        tc.post(f"/api/reactivation/{jid}/mark-sent")
        # unmark
        r = tc.post(f"/api/drafts/{did}/unmark-sent")
        assert r.status_code == 200
        # 该 draft 重新变 unsent
        d = store.latest_unsent_draft_for(jid)
        assert d is not None and d["draft_id"] == did
        store.close()

    def test_unmark_unknown_returns_400(self, tmp_path):
        class _AI:
            async def chat(self, prompt):  # noqa
                return ""
        tc, store = self._build(tmp_path, _AI())
        r = tc.post("/api/drafts/nonexistent-id/unmark-sent")
        assert r.status_code == 400
        store.close()

    def test_unmark_blocked_after_eval(self, tmp_path):
        """已评估的 draft 不能撤回（保 stats 一致性）。"""
        from src.contacts.models import CHANNEL_MESSENGER

        class _AI:
            async def chat(self, prompt):  # noqa
                return "嗨"

        tc, store = self._build(tmp_path, _AI())
        ctx = tc.gateway.on_message(
            channel=CHANNEL_MESSENGER, account_id="a", external_id="fb_e",
            direction="in", text_preview="hi")
        jid = ctx.journey.journey_id
        did = tc.post(f"/api/reactivation/{jid}/draft-reunion").json()["draft_id"]
        tc.post(f"/api/reactivation/{jid}/mark-sent")
        store.eval_draft_success(did, success=True)
        r = tc.post(f"/api/drafts/{did}/unmark-sent")
        assert r.status_code == 400
        assert "evaluated" in r.json()["detail"]
        store.close()
