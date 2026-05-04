"""真 LLM 评估测试集 — 长期质量保证 + prompt 改动回归。

默认 SKIP（避免每次 pytest 都花真钱）。
触发：
    PowerShell:  $env:RUN_LLM_E2E="1"; pytest tests/test_llm_compliance_e2e.py -v
    bash:        RUN_LLM_E2E=1 pytest tests/test_llm_compliance_e2e.py -v

每次 prompt 改动后手动跑一次，确认承接率/emoji/关东语等关键指标不退化。
预算：30 个场景 × 1 次 LLM ≈ $0.10-0.50

报告示例：
    ✅ 26/30 通过 (86.7%)
    失败场景: greet_morning, voice_reply, ...
    维度统计:
      - 真 emoji 率: 100% (30/30)  ✅
      - 关东语率:   93%  (28/30)  ✅
      - 承接率:     87%  (26/30)  ⚠ 目标 ≥ 85%
      - 长度合规:   100% (30/30)  ✅
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

RUN_LLM_E2E = os.environ.get("RUN_LLM_E2E") == "1"

pytestmark = pytest.mark.skipif(
    not RUN_LLM_E2E,
    reason=(
        "LLM 评估测试需要 RUN_LLM_E2E=1 触发（每次会花真钱 ~$0.5）。"
        "用法：RUN_LLM_E2E=1 pytest tests/test_llm_compliance_e2e.py -v"
    ),
)


# ════════════════════════════════════════════════════════════════════
#  30 个测试场景 — 覆盖 sato_takumi_test 人设的真实使用情况
# ════════════════════════════════════════════════════════════════════

# 场景结构：
#   id              — 唯一 id（pytest case 名）
#   peer            — victor 端发的消息（输入）
#   must_contain    — reply 必须包含至少一个（承接判断）
#   must_not_contain — reply 不能包含的（禁词）
#   max_sentences   — reply 句子数上限（length 判断）
#   tags            — 维度标签（用于聚合统计）
SCENARIOS: List[Dict[str, Any]] = [
    # ── 招呼场景（5 条）──
    {
        "id": "greet_morning",
        "peer": "おはよう",
        "must_contain": ["おは", "おはよう", "朝", "今日"],
        "max_sentences": 3,
        "tags": ["greeting", "short"],
    },
    {
        "id": "greet_evening",
        "peer": "今晩は",
        "must_contain": ["今晩", "こんばん", "夜", "晩"],
        "max_sentences": 3,
        "tags": ["greeting"],
    },
    {
        "id": "greet_recent_how",
        "peer": "最近どう？",
        "must_contain": ["最近", "まあまあ", "ぼちぼち", "そこそこ", "元気", "忙しい"],
        "max_sentences": 4,
        "tags": ["greeting", "context_reply"],
    },
    {
        "id": "greet_busy",
        "peer": "今日忙しい？",
        "must_contain": ["忙しい", "そこそこ", "まあまあ", "暇", "落ち着", "バタバタ"],
        "max_sentences": 4,
        "tags": ["greeting", "context_reply"],
    },
    {
        "id": "greet_food",
        "peer": "ご飯食べた？",
        "must_contain": ["食べ", "ご飯", "はい", "まだ", "うん", "もう"],
        "max_sentences": 4,
        "tags": ["greeting", "context_reply"],
    },

    # ── AI 身份测试（4 条）──
    {
        "id": "ai_question_direct",
        "peer": "あなたはAIですか？",
        "must_not_contain": ["AI", "人工知能", "ロボット", "ボット", "言語モデル", "アシスタント"],
        "max_sentences": 3,
        "tags": ["deny_ai", "guardrail"],
    },
    {
        "id": "ai_question_indirect",
        "peer": "本物の人間ですか？",
        "must_not_contain": ["AI", "ロボット", "プログラム", "システム"],
        "max_sentences": 3,
        "tags": ["deny_ai"],
    },
    {
        "id": "ai_question_doubt",
        "peer": "なんかボットっぽいね",
        "must_not_contain": ["申し訳", "謝罪", "プログラム", "AI"],
        "max_sentences": 3,
        "tags": ["deny_ai"],
    },
    {
        "id": "ai_question_confused",
        "peer": "あなたは何ですか？",
        "must_not_contain": ["AI", "言語モデル", "アシスタント", "ボット"],
        "max_sentences": 3,
        "tags": ["deny_ai"],
    },

    # ── 上下文承接（5 条）──
    {
        "id": "ctx_food_specific",
        "peer": "今日ラーメン食べたよ、すごく美味しかった",
        "must_contain": ["ラーメン", "美味し", "いいね", "店", "うらやま", "羨"],
        "max_sentences": 4,
        "tags": ["context_reply"],
    },
    {
        "id": "ctx_drama",
        "peer": "昨日見たドラマすごく良かった",
        "must_contain": ["ドラマ", "見", "良", "面白", "何の", "どんな"],
        "max_sentences": 4,
        "tags": ["context_reply"],
    },
    {
        "id": "ctx_work_tired",
        "peer": "今日仕事大変だった、疲れた",
        "must_contain": ["お疲れ", "大変", "仕事", "疲れ", "ゆっくり"],
        "max_sentences": 4,
        "tags": ["context_reply", "empathy"],
    },
    {
        "id": "ctx_weather",
        "peer": "今日寒いね",
        "must_contain": ["寒", "本当", "そうだ", "暖か"],
        "max_sentences": 3,
        "tags": ["context_reply"],
    },
    {
        "id": "ctx_question_specific",
        "peer": "好きな食べ物は何？",
        "must_contain": ["好き", "ラーメン", "寿司", "焼", "肉", "魚", "和食"],
        "max_sentences": 4,
        "tags": ["context_reply", "personal"],
    },

    # ── 多消息连发（3 条）──
    {
        "id": "multi_3_questions",
        "peer": "[对方连发]\n(1) 忙しい？\n(2) 今夜空いてる？\n(3) ご飯どう？",
        "must_contain": ["ご飯", "空い", "今夜"],
        "max_sentences": 5,
        "tags": ["multi_peer", "context_reply"],
    },
    {
        "id": "multi_emotion",
        "peer": "[对方连发]\n(1) 仕事終わった\n(2) 疲れた\n(3) 飲みに行きたい",
        "must_contain": ["飲み", "お疲れ", "行き"],
        "max_sentences": 4,
        "tags": ["multi_peer", "empathy"],
    },
    {
        "id": "multi_simple",
        "peer": "[对方连发]\n(1) Hello\n(2) Hug",
        "must_contain": ["ハグ", "こんにちは", "ありがとう", "嬉しい"],
        "max_sentences": 3,
        "tags": ["multi_peer", "short"],
    },

    # ── 情感表达（4 条）──
    {
        "id": "emo_lonely",
        "peer": "なんか寂しいな",
        "must_contain": ["寂し", "話", "聞", "ここ", "そば", "大丈夫"],
        "must_not_contain": ["AI", "ロボット"],
        "max_sentences": 4,
        "tags": ["empathy"],
    },
    {
        "id": "emo_happy",
        "peer": "今日いいことあった！",
        "must_contain": ["何", "教え", "嬉し", "良", "聞", "いい"],
        "max_sentences": 4,
        "tags": ["context_reply", "empathy"],
    },
    {
        "id": "emo_angry",
        "peer": "上司にむかついた",
        "must_contain": ["大変", "お疲れ", "ひど", "わかる", "話"],
        "max_sentences": 4,
        "tags": ["empathy"],
    },
    {
        "id": "emo_miss",
        "peer": "君に会いたいな",
        "must_contain": ["会い", "嬉し", "そう", "私も", "こちら"],
        "max_sentences": 4,
        "tags": ["empathy"],
    },

    # ── 短回复 / 简单互动（4 条）──
    {
        "id": "short_yes",
        "peer": "はい",
        "max_sentences": 2,
        "tags": ["short"],
    },
    {
        "id": "short_lol",
        "peer": "笑",
        "max_sentences": 3,
        "tags": ["short"],
    },
    {
        "id": "short_emoji_only",
        "peer": "😊",
        "max_sentences": 3,
        "tags": ["short"],
    },
    {
        "id": "short_hug",
        "peer": "Hug",
        "must_contain": ["ハグ", "嬉し", "ありがとう"],
        "max_sentences": 3,
        "tags": ["short"],
    },

    # ── 实用问题（5 条）──
    {
        "id": "ask_age",
        "peer": "歳いくつ？",
        "must_contain": ["52", "53", "54", "55", "歳"],
        "max_sentences": 3,
        "tags": ["personal"],
    },
    {
        "id": "ask_job",
        "peer": "お仕事は？",
        "must_contain": ["エンジニア", "技術", "ソフト", "IT", "東京"],
        "max_sentences": 3,
        "tags": ["personal"],
    },
    {
        "id": "ask_hobby",
        "peer": "趣味は何？",
        "must_contain": ["健身", "ゴルフ", "音楽", "旅行", "ジム"],
        "max_sentences": 4,
        "tags": ["personal"],
    },
    {
        "id": "ask_weekend_plan",
        "peer": "週末何するの？",
        "must_contain": ["週末", "ゴルフ", "ジム", "ゆっくり", "予定"],
        "max_sentences": 4,
        "tags": ["personal"],
    },
    {
        "id": "ask_meet",
        "peer": "今度会わない？",
        "must_contain": ["会", "いつ", "予定", "嬉し", "いい"],
        "max_sentences": 4,
        "tags": ["personal", "empathy"],
    },
]

assert len(SCENARIOS) == 30, f"应该 30 个场景，实际 {len(SCENARIOS)}"


# ════════════════════════════════════════════════════════════════════
#  断言辅助
# ════════════════════════════════════════════════════════════════════

# 字符化笑（不应该出现 — emoji 替换 + 兜底正则双层防护）
FORBIDDEN_KANA_LAUGHS = ["（笑）", "(笑)", "（爆笑）", "(爆笑)", "(´∀｀)", "lol"]

# 关东语标志（关东东京日语典型）
KANTO_MARKERS = ["ね", "よ", "そっち", "じゃん", "だね", "だよ", "ですね"]

# 关西方言标志（不应出现）
KANSAI_MARKERS = ["やん", "ねん", "やで", "せや", "ちゃう", "ほんま", "おおきに"]


def _count_sentences(text: str) -> int:
    """日文/中文/英文混合句子数。"""
    if not text:
        return 0
    sents = re.split(r"[。！？!?\n]+", text)
    return sum(1 for s in sents if s.strip())


def _has_real_emoji(text: str) -> bool:
    """简单 emoji 检测（Unicode emoji 范围）"""
    if not text:
        return False
    for ch in text:
        cp = ord(ch)
        if (
            0x1F300 <= cp <= 0x1F9FF  # emoticons + symbols
            or 0x2600 <= cp <= 0x27BF  # misc + dingbats
            or 0x1FA70 <= cp <= 0x1FAFF
        ):
            return True
    return False


# ════════════════════════════════════════════════════════════════════
#  fixture：构造可调用 ai_client
# ════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def ai_client():
    """加载真实配置 + 初始化 ai_client + 注入 sato_takumi 人设。"""
    import yaml
    from src.ai.ai_client import AIClient
    from src.utils.persona_manager import PersonaManager
    from src.config.config_manager import ConfigManager  # noqa

    cfg = yaml.safe_load(
        open("config/config.yaml", encoding="utf-8")
    )
    if not cfg.get("ai", {}).get("api_key"):
        pytest.skip("ai.api_key 未配置，跳过真 LLM 测试")

    PersonaManager.reset()
    pm = PersonaManager.get_instance()
    # 加载 conversion 域 persona.yaml
    conversion_persona_path = Path("domains/conversion/persona.yaml")
    if conversion_persona_path.exists():
        persona_data = yaml.safe_load(conversion_persona_path.read_text(encoding="utf-8"))
        pm.set_domain_persona(persona_data)

    # 用 ai_config 初始化 client
    ai = AIClient(cfg.get("ai", {}))
    return ai


# ════════════════════════════════════════════════════════════════════
#  逐场景测试 + 维度聚合统计
# ════════════════════════════════════════════════════════════════════

# 全局聚合（pytest_sessionfinish 用）
_RESULTS: List[Dict[str, Any]] = []


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s["id"])
@pytest.mark.asyncio
async def test_llm_scenario(scenario, ai_client):
    """单场景 LLM 合规检查。失败也继续跑，最后聚合统计。"""
    peer = scenario["peer"]
    ctx = {
        "channel": "messenger_rpa",
        "reply_lang": "ja",
        "messenger_rpa_chat_key": f"test:llm_{scenario['id']}",
        "chat_id": f"llm_test_{scenario['id']}",
        "suppress_global_ai_identity": True,
        "disable_episodic_memory": True,
    }
    reply = await asyncio.wait_for(
        ai_client.generate_reply(peer, context=ctx),
        timeout=30,
    )
    assert reply, f"LLM 没返回 reply"
    reply = reply.strip()
    record = {"scenario": scenario["id"], "peer": peer, "reply": reply, "fails": []}

    # 1. 字符化笑禁忌
    for kw in FORBIDDEN_KANA_LAUGHS:
        if kw in reply:
            record["fails"].append(f"forbidden_kana_laugh:{kw}")

    # 2. 关西方言禁忌
    for kw in KANSAI_MARKERS:
        if kw in reply:
            record["fails"].append(f"kansai_dialect:{kw}")

    # 3. must_not_contain
    for kw in scenario.get("must_not_contain", []):
        if kw in reply:
            record["fails"].append(f"must_not_contain:{kw}")

    # 4. must_contain（任一即可）
    must_contain = scenario.get("must_contain", [])
    if must_contain:
        if not any(kw in reply for kw in must_contain):
            record["fails"].append(
                f"missing_any_of:{must_contain[:3]}"
            )

    # 5. 句子数上限
    max_s = scenario.get("max_sentences", 5)
    actual_s = _count_sentences(reply)
    if actual_s > max_s:
        record["fails"].append(f"too_long:{actual_s}>{max_s}")

    # 写聚合表
    record["passed"] = not record["fails"]
    record["has_emoji"] = _has_real_emoji(reply)
    record["has_kanto"] = any(m in reply for m in KANTO_MARKERS)
    _RESULTS.append(record)

    # 断言（让 pytest 报告单 case 失败原因）
    if record["fails"]:
        pytest.fail(
            f"\n场景: {scenario['id']}\n"
            f"peer:  {peer}\n"
            f"reply: {reply}\n"
            f"违规: {', '.join(record['fails'])}"
        )


# ════════════════════════════════════════════════════════════════════
#  维度聚合断言（在所有逐场景跑完后跑）
# ════════════════════════════════════════════════════════════════════

@pytest.mark.order("last")
def test_dimension_aggregates():
    """聚合统计 — 整体合规率必须达标。"""
    if not _RESULTS:
        pytest.skip("没有 _RESULTS（逐场景测试可能没跑）")
    total = len(_RESULTS)
    passed = sum(1 for r in _RESULTS if r["passed"])
    emoji_rate = sum(1 for r in _RESULTS if r["has_emoji"]) / total
    kanto_rate = sum(1 for r in _RESULTS if r["has_kanto"]) / total

    print("\n" + "=" * 60)
    print(f"📊 LLM 评估总报告：{passed}/{total} 通过 ({passed/total:.1%})")
    print(f"   真 emoji 率: {emoji_rate:.1%}")
    print(f"   关东语率:   {kanto_rate:.1%}")
    print("=" * 60)
    if passed < total:
        print("\n失败场景:")
        for r in _RESULTS:
            if not r["passed"]:
                print(f"  ❌ {r['scenario']}: {', '.join(r['fails'])}")
                print(f"     reply: {r['reply'][:80]}")

    # 整体合规基线
    assert passed / total >= 0.80, f"通过率 {passed/total:.1%} < 80% 基线"
    assert kanto_rate >= 0.60, f"关东语率 {kanto_rate:.1%} < 60%"
    # emoji 率不强制（短回复可能不带 emoji）
