"""端到端集成测试：persona 字段修改 → 注入到 system prompt 全链路。

覆盖目标（防回归）：
  P1-1：5 条硬约束（直接答问、跟随话题、连发先回最新、禁动作括号、禁列举括号）
        在 full / compact 两种模式下都被注入。
  P1-2：deny_ai 启用时身份硬锁段进入 prompt；关闭时不出现。
  P2-1：web 修改 reply_profile 字段（forbidden_phrases / persona.name 等）后，
        _pick_reply_profile 能选到新值，PersonaManager.format_persona_block
        能输出新字段。

测试不依赖真实文件 / FastAPI / db；只校验 prompt 拼接 + profile 选取的纯函数语义。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.integrations.messenger_rpa.runner import MessengerRpaRunner
from src.utils.persona_manager import PersonaManager


# ── 辅助：和 test_messenger_reply_profiles.py 同一构造手法 ──

class _AI:
    def _detect_message_language(self, text: str) -> str:
        return "zh"


class _SM:
    ai_client = _AI()
    _context_store = None


def _runner(cfg: dict) -> MessengerRpaRunner:
    r = object.__new__(MessengerRpaRunner)
    r._cfg = cfg
    r._sm = _SM()
    return r


def _baseline_persona(*, deny_ai: bool = False, deny_reply: str = "") -> dict:
    return {
        "name": "Mira",
        "role": "情感陪伴",
        "personality": {"traits": ["温柔"], "style": "口语聊天"},
        "speaking": {
            "openers": ["在呀"],
            "forbidden_phrases": ["作为AI"],
            "max_reply_sentences": 6,
            "language_follow": True,
        },
        "identity": {"deny_ai": deny_ai, "deny_ai_reply": deny_reply},
        "boundaries": {"topics_to_avoid": []},
    }


# ════════════════════════════════════════════════════════════════════
#  P1-1：5 条硬约束注入
# ════════════════════════════════════════════════════════════════════

class TestHardRulesInjection:
    def setup_method(self):
        PersonaManager.reset()

    def test_full_mode_includes_hard_constraint_block_marker(self):
        pm = PersonaManager.get_instance()
        pm.set_domain_persona(_baseline_persona())
        block = pm.format_persona_block(detail="full")
        assert "【回复硬约束】" in block, "full 模式必须含硬约束块头"

    def test_full_mode_includes_all_5_rules(self):
        pm = PersonaManager.get_instance()
        pm.set_domain_persona(_baseline_persona())
        block = pm.format_persona_block(detail="full")
        # 5 条规则的标志性词组（不是逐字断言，给文案微调留余地）
        assert "正面回答" in block, "规则1：先正面答问"
        assert "切换话题" in block, "规则2：跟随话题"
        assert "连发" in block or "[对方连发]" in block, "规则3：连发先回最新"
        assert "动作" in block and "()" in block, "规则4：禁动作括号"
        assert "列举" in block or "(1)" in block, "规则5：禁列举括号"

    def test_compact_mode_has_safety_net_rule(self):
        pm = PersonaManager.get_instance()
        pm.set_domain_persona(_baseline_persona())
        block = pm.format_persona_block(detail="compact")
        # compact 模式不需要全 5 条，但必须保留核心 1 条（禁括号 + 直接答问）
        assert "回复硬约束" in block, "compact 模式必须保留核心硬约束"
        assert "正面回答" in block
        assert "()" in block or "(1)(2)" in block, "compact 必须保留禁括号"

    def test_none_mode_returns_empty(self):
        pm = PersonaManager.get_instance()
        pm.set_domain_persona(_baseline_persona())
        block = pm.format_persona_block(detail="none")
        assert block == "", "none 模式返回空，不影响其他系统提示"

    def test_hard_rules_present_for_arbitrary_persona(self):
        """任意人设（不是 baseline）也必须套用硬约束。"""
        pm = PersonaManager.get_instance()
        pm.set_domain_persona({
            "name": "随便起的名字",
            "role": "随便的角色",
        })
        block = pm.format_persona_block(detail="full")
        assert "【回复硬约束】" in block


# ════════════════════════════════════════════════════════════════════
#  P2-A：emoji_level / reply_length 真生效（修真断链）
#  防止"web 后台改了但不进 prompt"的回归。
# ════════════════════════════════════════════════════════════════════

class TestEmojiLevelRealEffect:
    def setup_method(self):
        PersonaManager.reset()

    def test_emoji_none_writes_no_emoji_directive(self):
        pm = PersonaManager.get_instance()
        pm.set_domain_persona({
            "name": "X", "role": "y",
            "personality": {"emoji_level": "none"},
        })
        block = pm.format_persona_block(detail="full")
        assert "不使用任何 emoji" in block or "不用 emoji" in block

    def test_emoji_minimal_writes_low_freq(self):
        pm = PersonaManager.get_instance()
        pm.set_domain_persona({
            "name": "X", "role": "y",
            "personality": {"emoji_level": "minimal"},
        })
        block = pm.format_persona_block(detail="full")
        assert "极少" in block

    def test_emoji_rich_writes_high_freq(self):
        pm = PersonaManager.get_instance()
        pm.set_domain_persona({
            "name": "X", "role": "y",
            "personality": {"emoji_level": "rich"},
        })
        block = pm.format_persona_block(detail="full")
        assert "60%" in block or "活泼" in block

    def test_emoji_unknown_value_does_not_crash(self):
        pm = PersonaManager.get_instance()
        pm.set_domain_persona({
            "name": "X", "role": "y",
            "personality": {"emoji_level": "wtf_unknown"},
        })
        # 不抛异常，未知值就不输出 emoji 段
        block = pm.format_persona_block(detail="full")
        assert "wtf_unknown" not in block

    def test_emoji_in_compact_mode_also_works(self):
        pm = PersonaManager.get_instance()
        pm.set_domain_persona({
            "name": "X", "role": "y",
            "personality": {"emoji_level": "rich"},
        })
        block = pm.format_persona_block(detail="compact")
        assert "60%" in block or "活泼" in block


class TestReplyLengthRealEffect:
    def setup_method(self):
        PersonaManager.reset()

    def test_reply_length_short(self):
        pm = PersonaManager.get_instance()
        pm.set_domain_persona({
            "name": "X", "role": "y",
            "speaking": {"reply_length": "short"},
        })
        block = pm.format_persona_block(detail="full")
        assert "1-2 句" in block

    def test_reply_length_balanced(self):
        pm = PersonaManager.get_instance()
        pm.set_domain_persona({
            "name": "X", "role": "y",
            "speaking": {"reply_length": "balanced"},
        })
        block = pm.format_persona_block(detail="full")
        assert "2-4 句" in block

    def test_reply_length_detailed(self):
        pm = PersonaManager.get_instance()
        pm.set_domain_persona({
            "name": "X", "role": "y",
            "speaking": {"reply_length": "detailed"},
        })
        block = pm.format_persona_block(detail="full")
        assert "4-6 句" in block

    def test_reply_length_overrides_max_sentences(self):
        """reply_length 占主导，max_reply_sentences 仅在 reply_length 缺失时 fallback。"""
        pm = PersonaManager.get_instance()
        pm.set_domain_persona({
            "name": "X", "role": "y",
            "speaking": {
                "reply_length": "short",
                "max_reply_sentences": 99,
            },
        })
        block = pm.format_persona_block(detail="full")
        # reply_length=short 输出"1-2 句"
        assert "1-2 句" in block
        # max_sentences 被压制（99 不该出现作为独立指令）
        assert "不超过 99 句" not in block

    def test_max_sentences_used_when_reply_length_missing(self):
        pm = PersonaManager.get_instance()
        pm.set_domain_persona({
            "name": "X", "role": "y",
            "speaking": {"max_reply_sentences": 5},
        })
        block = pm.format_persona_block(detail="full")
        assert "不超过 5 句" in block


class TestStyleHintAlreadyPiped:
    """确认 style_hint 已经通过 ctx["messenger_rpa_style_hint"] 接通，
    防回归——这条链路被 ai_client._build_context_prompt 消费。"""

    def test_style_hint_path_is_documented_in_runner(self):
        """直接读 runner 源码验证 ctx 注入还在。"""
        from pathlib import Path
        runner_src = Path(
            "src/integrations/messenger_rpa/runner.py"
        ).read_text(encoding="utf-8")
        # 关键链路：reply_profile.style_hint → ctx["messenger_rpa_style_hint"]
        assert 'reply_profile.get("style_hint")' in runner_src
        assert 'ctx["messenger_rpa_style_hint"]' in runner_src

    def test_style_hint_path_is_documented_in_ai_client(self):
        """ai_client 真的读 ctx["messenger_rpa_style_hint"]。"""
        from pathlib import Path
        ai_src = Path("src/ai/ai_client.py").read_text(encoding="utf-8")
        assert 'messenger_rpa_style_hint' in ai_src
        assert "Messenger 人设补充" in ai_src


# ════════════════════════════════════════════════════════════════════
#  P1-2：deny_ai 身份硬锁
# ════════════════════════════════════════════════════════════════════

class TestDenyAiHardLock:
    def setup_method(self):
        PersonaManager.reset()

    def test_deny_ai_off_no_identity_lock_in_full(self):
        pm = PersonaManager.get_instance()
        pm.set_domain_persona(_baseline_persona(deny_ai=False))
        block = pm.format_persona_block(detail="full")
        assert "【身份硬锁】" not in block
        assert "永远不要承认自己是 AI" not in block

    def test_deny_ai_on_full_mode_has_identity_lock(self):
        pm = PersonaManager.get_instance()
        pm.set_domain_persona(
            _baseline_persona(deny_ai=True, deny_reply="哈哈你怎么这么问")
        )
        block = pm.format_persona_block(detail="full")
        assert "【身份硬锁】" in block, "full 模式必须含身份硬锁段"
        assert "Mira" in block
        assert "哈哈你怎么这么问" in block, "deny_ai_reply 必须写入 prompt"
        # 强化指令要明确禁止解释运作原理 + 不说 sorry to confuse
        assert "运作原理" in block
        assert "sorry to confuse" in block

    def test_deny_ai_on_compact_mode_has_lock(self):
        pm = PersonaManager.get_instance()
        pm.set_domain_persona(
            _baseline_persona(deny_ai=True, deny_reply="干嘛突然问这个")
        )
        block = pm.format_persona_block(detail="compact")
        assert "干嘛突然问这个" in block, "compact 模式也必须保留 deny_ai 兜底"
        assert "AI" in block, "compact 必须明确否认 AI 身份"

    def test_deny_ai_reply_can_use_any_language(self):
        """deny_ai_reply 用中文，但 prompt 通过"可改写但不要偏离意思"
        让 LLM 自动适配对话语言，不需要硬编码三语。"""
        pm = PersonaManager.get_instance()
        pm.set_domain_persona(
            _baseline_persona(deny_ai=True, deny_reply="哈哈怎么这么问")
        )
        block = pm.format_persona_block(detail="full")
        assert "可改写" in block or "自然回应" in block


# ════════════════════════════════════════════════════════════════════
#  P2-1：reply_profile 编辑 → _pick_reply_profile → 注入 prompt 全链路
# ════════════════════════════════════════════════════════════════════

class TestReplyProfileToPromptFlow:
    def setup_method(self):
        PersonaManager.reset()

    def test_pick_returns_default_profile_with_persona_block(self):
        """模拟 web 配置一个人设作为 default → _pick_reply_profile 命中。"""
        r = _runner({
            "reply_profiles": {
                "default": "vip",
                "profiles": [
                    {
                        "id": "warm",
                        "language": "zh",
                        "persona": {"name": "暖暖"},
                    },
                    {
                        "id": "vip",
                        "language": "ja",
                        "style_hint": "VIP tone",
                        "persona": {
                            "name": "アヤカ",
                            "speaking": {
                                "forbidden_phrases": ["客服模板", "为您服务"],
                            },
                        },
                    },
                ],
            }
        })
        picked = r._pick_reply_profile("acc_x:佐藤", "佐藤")
        assert picked["id"] == "vip"
        # persona 子字段通过 picked 暴露
        assert picked["persona"]["name"] == "アヤカ"
        forb = picked["persona"]["speaking"]["forbidden_phrases"]
        assert "客服模板" in forb
        assert "为您服务" in forb

    def test_picked_persona_via_format_persona_block_has_new_field(self):
        """模拟 web PATCH 后流程：
          web 改 forbidden_phrases → reply_profiles 更新 →
          _pick_reply_profile 选到新 profile → 通过 bind_chat_persona 注入 →
          format_persona_block 输出包含新词。
        """
        r = _runner({
            "reply_profiles": {
                "default": "p1",
                "profiles": [
                    {
                        "id": "p1",
                        "persona": {
                            "name": "新名字Beta",
                            "role": "测试角色",
                            "speaking": {
                                "forbidden_phrases": ["禁词EDITED"],
                            },
                            "identity": {
                                "deny_ai": True,
                                "deny_ai_reply": "DENY_REPLY_EDITED",
                            },
                        },
                    },
                ],
            }
        })
        picked = r._pick_reply_profile("acc_x:用户A", "用户A")
        persona_data = picked["persona"]

        pm = PersonaManager.get_instance()
        chat_id = "acc_x:用户A"
        pm.bind_chat_persona(chat_id, persona_data)
        block = pm.format_persona_block(chat_id, detail="full")

        # 1. 名字注入
        assert "新名字Beta" in block, "persona.name 必须进入 prompt"
        # 2. 编辑后的禁词注入（防回归：用户在 web 改 forbidden 后能立刻看到）
        assert "禁词EDITED" in block, "新 forbidden_phrases 必须立即生效"
        # 3. 编辑后的 deny_ai_reply 注入
        assert "DENY_REPLY_EDITED" in block
        # 4. 5 条硬约束依然套用
        assert "【回复硬约束】" in block
        # 5. 身份硬锁段已注入
        assert "【身份硬锁】" in block

    def test_pick_match_name_overrides_default(self):
        """match_names 命中优先于 default —— web 上配的"VIP 单聊用 X 人设"。"""
        r = _runner({
            "reply_profiles": {
                "default": "warm",
                "profiles": [
                    {
                        "id": "warm",
                        "persona": {"name": "Default人设"},
                    },
                    {
                        "id": "alice_special",
                        "match_names": ["Alice"],
                        "persona": {"name": "Alice专属人设"},
                    },
                ],
            }
        })
        picked = r._pick_reply_profile("acc_x:Alice", "Alice Chen")
        assert picked["id"] == "alice_special"
        assert picked["persona"]["name"] == "Alice专属人设"

    def test_refresh_cfg_makes_new_profile_visible_immediately(self):
        """模拟 web PATCH 后 _refresh_service_runtime → runner.refresh_cfg(new_cfg)
        热重载链路。改动后下一次 _pick_reply_profile 必须命中新值，无需重启。
        这是用户报告"保存不生效"的核心防回归测试。
        """
        r = _runner({
            "reply_profiles": {
                "default": "old_default",
                "profiles": [
                    {"id": "old_default", "persona": {"name": "旧名字"}},
                ],
            }
        })
        # 1. 旧配置：命中 old_default
        before = r._pick_reply_profile("acc_x:user", "user")
        assert before["id"] == "old_default"
        assert before["persona"]["name"] == "旧名字"

        # 2. 模拟 web 保存：构造新 cfg → refresh_cfg
        new_cfg = {
            "reply_profiles": {
                "default": "new_choice",
                "profiles": [
                    {"id": "old_default", "persona": {"name": "旧名字"}},
                    {
                        "id": "new_choice",
                        "persona": {
                            "name": "热加载新名",
                            "speaking": {"forbidden_phrases": ["新禁词"]},
                        },
                    },
                ],
            }
        }
        r.refresh_cfg(new_cfg)

        # 3. 立即下一次 pick：命中新 default 而不是旧的
        after = r._pick_reply_profile("acc_x:user", "user")
        assert after["id"] == "new_choice", "refresh_cfg 后必须立即生效"
        assert after["persona"]["name"] == "热加载新名"
        assert "新禁词" in after["persona"]["speaking"]["forbidden_phrases"]

    def test_picked_in_compact_mode_still_has_deny_ai_safety_net(self):
        """deny_ai persona 在 persona_block_detail=compact 配置下也必须保留
        身份兜底，防"配置切到 compact → 整个底线消失"。"""
        r = _runner({
            "reply_profiles": {
                "default": "secured",
                "profiles": [
                    {
                        "id": "secured",
                        "persona": {
                            "name": "Sera",
                            "identity": {
                                "deny_ai": True,
                                "deny_ai_reply": "我哪是什么AI啦",
                            },
                            "speaking": {"forbidden_phrases": ["作为AI"]},
                        },
                    },
                ],
            }
        })
        picked = r._pick_reply_profile("acc_x:any", "any")
        pm = PersonaManager.get_instance()
        pm.bind_chat_persona("acc_x:any", picked["persona"])
        compact_block = pm.format_persona_block("acc_x:any", detail="compact")
        # compact 模式：保留 deny_ai 兜底 + 1 条硬约束 + 自定义禁词
        assert "我哪是什么AI啦" in compact_block
        assert "回复硬约束" in compact_block
        assert "作为AI" in compact_block

    def test_pick_logs_match_source_visible_to_operator(self):
        """P0-B 决策可观测性：_pick_reply_profile 必须能被 caplog 捕获到
        命中的 persona id 和 match source，让运营在 web 改完后能在日志对账。"""
        import logging

        logger = logging.getLogger("src.integrations.messenger_rpa.runner")
        old_level = logger.level
        logger.setLevel(logging.INFO)
        # 直接收集 logger 的 records
        records = []

        class _Capture(logging.Handler):
            def emit(self, record):
                records.append(self.format(record))

        h = _Capture(level=logging.INFO)
        h.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(h)
        try:
            r = _runner({
                "reply_profiles": {
                    "default": "trace_target",
                    "profiles": [
                        {
                            "id": "trace_target",
                            "language": "zh",
                            "style_hint": "test trace",
                            "persona": {
                                "name": "TraceBot",
                                "speaking": {
                                    "forbidden_phrases": ["禁A", "禁B", "禁C"],
                                },
                            },
                        },
                    ],
                }
            })
            r._pick_reply_profile("acc_x:观测对账", "观测对账")
        finally:
            logger.removeHandler(h)
            logger.setLevel(old_level)

        joined = "\n".join(records)
        assert "persona pick" in joined
        assert "trace_target" in joined
        assert "TraceBot" in joined
        assert "source=default" in joined
        assert "forbidden_n=3" in joined, "日志应明确暴露禁词数量，让用户对账"
