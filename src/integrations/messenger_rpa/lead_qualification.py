"""Messenger lead qualification for natural, staged customer screening.

The engine is intentionally deterministic and explainable. It does not decide
from avatar/name guesses; it only scores explicit or conversational evidence
found in messages plus language/context signals supplied by the runner.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


_JP_PLACE_RE = re.compile(
    r"(日本|東京|大阪|京都|横浜|神戸|名古屋|福岡|札幌|沖縄|埼玉|千葉|"
    r"北海道|仙台|広島|奈良|鎌倉|湘南|港区|渋谷|世田谷|目黒|青山|麻布|"
    r"六本木|銀座|丸の内|恵比寿|代官山|白金|田園調布|芦屋|西宮|"
    r"Tokyo|Osaka|Kyoto|Yokohama|Japan)",
    re.I,
)
_AGE_RE = re.compile(r"(?<!\d)([3-6]\d|7[0-5])\s*(歳|才|さい|years?\s*old|yo)", re.I)
_AGE_DECADE_RE = re.compile(r"([3-6]0)代(前半|半ば|後半)?")
_AGE_JA_DECADE_RE = re.compile(r"(三十|四十|五十|六十)代(前半|半ば|後半)?")
_CHILD_AGE_RE = re.compile(
    r"(息子|娘|子供|子ども|長男|長女|末っ子)[^。！？\n]{0,16}?([1-3]\d)\s*(歳|才|さい)"
)
_INCOME_RE = re.compile(r"(年収|収入|所得|給料|月収)\s*([0-9]{2,5})\s*(万|万円)?")
_BUDGET_RE = re.compile(r"(予算|費用|料金|相談料|支払い|払える|使える|投資)")

_FEMALE_TERMS = (
    "女性", "女です", "女だよ", "女の子", "主婦", "妻", "母です", "母親",
    "独身女性", "女性です", "彼氏", "夫", "旦那", "息子", "娘が",
    "母子", "シングルマザー", "離婚した女性",
)
_MALE_TERMS = ("男性", "男です", "男だよ", "俺は男", "僕は男", "父親です")
_HIGH_OCCUPATION_TERMS = (
    "経営", "経営者", "社長", "役員", "取締役", "オーナー", "自営業",
    "医師", "医者", "弁護士", "会計士", "税理士", "薬剤師", "歯科医",
    "不動産", "投資", "管理職", "マネージャー", "部長", "課長",
    "コンサル", "美容サロン", "会社を", "事業", "外資", "金融",
    "証券", "保険代理店", "大学教授", "教授", "研究職", "IT企業",
    "エンジニア", "弁理士", "士業", "クリニック", "サロン経営",
    "投資家", "大家", "地主", "会社役員", "個人事業",
)
_STABLE_OCCUPATION_TERMS = (
    "会社員", "公務員", "看護師", "教師", "教員", "銀行", "保険",
    "営業", "事務", "仕事", "職場", "勤務", "働いて",
)
_AFFLUENT_LIFESTYLE_TERMS = (
    "ゴルフ", "海外旅行", "海外によく", "出張", "ホテル", "会員制",
    "エステ", "美容医療", "ブランド", "タワマン", "別荘", "軽井沢",
    "ハワイ", "シンガポール", "ヨーロッパ", "ワイン", "銀座",
    "麻布", "六本木", "青山", "港区", "白金", "丸の内",
)
_RELATIONSHIP_NEED_TERMS = (
    "離婚", "バツイチ", "独身", "一人暮らし", "ひとり暮らし",
    "寂しい", "孤独", "話し相手", "相談したい", "疲れた", "不安",
    "子供が独立", "子どもが独立", "更年期", "パートナー",
)
_LOW_VALUE_TERMS = (
    "学生", "無職", "お金ない", "金ない", "無料", "ただで", "暇つぶし",
)
_NEED_TERMS = (
    "相談", "詳しく", "教えて", "興味", "悩み", "困って", "疲れた",
    "寂しい", "不安", "将来", "サービス", "担当", "話を聞いて",
)


@dataclass(frozen=True)
class LeadDecision:
    profile: Dict[str, Any]
    action: str
    score: int
    stage: str
    prompt_block: str = ""
    forced_reply: str = ""
    result: Optional[Dict[str, Any]] = None


class LeadQualificationEngine:
    """Rule-based ICP scoring and next-step selection.

    Actions:
    - continue: normal generation, with optional profile discovery hint.
    - low_priority: reply briefly, do not ask new profile questions.
    - silent_stop: skip replying after repeated low-value turns.
    - handoff_line: return a deterministic LINE handoff message.
    """

    def __init__(self, cfg: Optional[Dict[str, Any]] = None) -> None:
        self.cfg = dict(cfg or {})

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled", False))

    def update_cfg(self, cfg: Optional[Dict[str, Any]]) -> None:
        self.cfg = dict(cfg or {})

    def evaluate(
        self,
        profile: Optional[Dict[str, Any]],
        *,
        peer_text: str,
        reply_lang: str,
        chat_name: str = "",
        now: Optional[float] = None,
    ) -> LeadDecision:
        now = float(now or time.time())
        p = self._normalize_profile(profile)
        text = (peer_text or "").strip()
        p["turns"] = int(p.get("turns") or 0) + 1
        p["updated_at"] = now

        evidence: List[str] = list(p.get("evidence") or [])[-20:]
        self._extract_country(p, text, reply_lang, evidence)
        self._extract_gender(p, text, evidence)
        self._extract_age(p, text, evidence)
        self._extract_occupation_income_need(p, text, evidence)

        p["evidence"] = evidence[-30:]
        score, score_parts = self._score(p, reply_lang)
        p["icp_score"] = score
        p["score_parts"] = score_parts
        p["missing_fields"] = self._missing_fields(p)

        hard = self._hard_disqualifier(p)
        if hard:
            p["stage"] = "low_priority"
            p["low_priority_reason"] = hard
            p["low_value_turns"] = int(p.get("low_value_turns") or 0) + 1
        else:
            p["low_value_turns"] = 0
            p["stage"] = self._stage_for(p, score)

        action = self._action_for(p, score, hard, now)
        forced = ""
        if action == "handoff_line":
            forced = self._handoff_reply()
            if forced:
                p["line_sent_at"] = now
                p["stage"] = "line_sent"
            else:
                action = "continue"

        prompt = self._prompt_block(p, action)
        result = {
            "stage": p.get("stage", ""),
            "action": action,
            "score": score,
            "missing_fields": list(p.get("missing_fields") or []),
            "next_question": p.get("next_question", ""),
            "low_priority_reason": p.get("low_priority_reason", ""),
            "line_sent": bool(p.get("line_sent_at")),
            "score_parts": score_parts,
        }
        return LeadDecision(
            profile=p,
            action=action,
            score=score,
            stage=str(p.get("stage") or ""),
            prompt_block=prompt,
            forced_reply=forced,
            result=result,
        )

    def _normalize_profile(self, profile: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        p = dict(profile or {})
        p.setdefault("stage", "new_lead")
        p.setdefault("turns", 0)
        p.setdefault("gender", "unknown")
        p.setdefault("gender_confidence", 0.0)
        p.setdefault("country", "unknown")
        p.setdefault("country_confidence", 0.0)
        p.setdefault("age_range", [])
        p.setdefault("age_confidence", 0.0)
        p.setdefault("occupation", "")
        p.setdefault("occupation_tier", "unknown")
        p.setdefault("income_band", "unknown")
        p.setdefault("income_confidence", 0.0)
        p.setdefault("need_tags", [])
        p.setdefault("lifestyle_tags", [])
        p.setdefault("relationship_tags", [])
        p.setdefault("profile_questions_asked", {})
        return p

    def _extract_country(
        self, p: Dict[str, Any], text: str, reply_lang: str, evidence: List[str],
    ) -> None:
        if _JP_PLACE_RE.search(text):
            p["country"] = "JP"
            p["country_confidence"] = max(float(p.get("country_confidence") or 0), 0.9)
            evidence.append("country:jp_explicit")
        elif reply_lang == "ja" and p.get("country") == "unknown":
            p["country"] = "JP_likely"
            p["country_confidence"] = max(float(p.get("country_confidence") or 0), 0.45)

    def _extract_gender(self, p: Dict[str, Any], text: str, evidence: List[str]) -> None:
        if any(t in text for t in _MALE_TERMS):
            p["gender"] = "male"
            p["gender_confidence"] = 0.9
            evidence.append("gender:male_explicit")
            return
        if any(t in text for t in _FEMALE_TERMS):
            p["gender"] = "female"
            p["gender_confidence"] = max(float(p.get("gender_confidence") or 0), 0.75)
            evidence.append("gender:female_signal")

    def _extract_age(self, p: Dict[str, Any], text: str, evidence: List[str]) -> None:
        m = _AGE_RE.search(text)
        if m:
            age = int(m.group(1))
            p["age_range"] = [age, age]
            p["age_confidence"] = 0.9
            evidence.append(f"age:explicit:{age}")
            return
        m = _AGE_DECADE_RE.search(text)
        if m:
            decade = int(m.group(1))
            p["age_range"] = self._decade_range(decade, m.group(2) or "")
            p["age_confidence"] = max(float(p.get("age_confidence") or 0), 0.75)
            evidence.append(f"age:decade:{decade}s")
        else:
            m = _AGE_JA_DECADE_RE.search(text)
            if m:
                decade = {"三十": 30, "四十": 40, "五十": 50, "六十": 60}[m.group(1)]
                p["age_range"] = self._decade_range(decade, m.group(2) or "")
                p["age_confidence"] = max(float(p.get("age_confidence") or 0), 0.75)
                evidence.append(f"age:ja_decade:{decade}s")
                return
            child = _CHILD_AGE_RE.search(text)
            if child:
                child_age = int(child.group(2))
                if child_age >= 18:
                    lo = max(37, child_age + 22)
                    hi = min(65, child_age + 38)
                    p["age_range"] = [lo, hi]
                    p["age_confidence"] = max(float(p.get("age_confidence") or 0), 0.45)
                    evidence.append(f"age:inferred_child:{child_age}")
                    return
        if "アラフォー" in text:
            p["age_range"] = [37, 44]
            p["age_confidence"] = 0.7
            evidence.append("age:around40")
        elif "アラフィフ" in text:
            p["age_range"] = [47, 54]
            p["age_confidence"] = 0.7
            evidence.append("age:around50")

    def _extract_occupation_income_need(
        self, p: Dict[str, Any], text: str, evidence: List[str],
    ) -> None:
        if any(t in text for t in _HIGH_OCCUPATION_TERMS):
            p["occupation_tier"] = "high_income_signal"
            p["occupation"] = self._first_hit(text, _HIGH_OCCUPATION_TERMS) or p.get("occupation", "")
            evidence.append(f"occupation:high:{p['occupation']}")
        elif any(t in text for t in _STABLE_OCCUPATION_TERMS):
            if p.get("occupation_tier") == "unknown":
                p["occupation_tier"] = "stable"
            p["occupation"] = self._first_hit(text, _STABLE_OCCUPATION_TERMS) or p.get("occupation", "")
            evidence.append(f"occupation:stable:{p['occupation']}")
        if any(t in text for t in _LOW_VALUE_TERMS):
            p["low_value_signal"] = True
            evidence.append("low_value:explicit")

        lifestyle_tags = set(p.get("lifestyle_tags") or [])
        hit = self._first_hit(text, _AFFLUENT_LIFESTYLE_TERMS)
        if hit:
            lifestyle_tags.add(hit)
            if p.get("income_band") == "unknown":
                p["income_band"] = "medium_high"
                p["income_confidence"] = max(float(p.get("income_confidence") or 0), 0.45)
            evidence.append(f"lifestyle:affluent:{hit}")
        p["lifestyle_tags"] = sorted(lifestyle_tags)

        relationship_tags = set(p.get("relationship_tags") or [])
        rel_hit = self._first_hit(text, _RELATIONSHIP_NEED_TERMS)
        if rel_hit:
            relationship_tags.add(rel_hit)
            evidence.append(f"relationship:{rel_hit}")
        p["relationship_tags"] = sorted(relationship_tags)

        m = _INCOME_RE.search(text)
        if m:
            amount = int(m.group(2))
            annual = amount if m.group(1) == "年収" or amount >= 300 else amount * 12
            if annual >= 800:
                p["income_band"] = "high"
            elif annual >= 500:
                p["income_band"] = "medium_high"
            else:
                p["income_band"] = "low_or_unknown"
            p["income_confidence"] = 0.85
            evidence.append(f"income:{p['income_band']}:{annual}man")

        need_tags = set(p.get("need_tags") or [])
        if any(t in text for t in _NEED_TERMS):
            need_tags.add("consultation_interest")
            evidence.append("need:consultation_interest")
        if relationship_tags and any(t in text for t in ("寂しい", "孤独", "話し相手", "相談したい", "疲れた", "不安")):
            need_tags.add("emotional_support")
            evidence.append("need:emotional_support")
        if _BUDGET_RE.search(text):
            need_tags.add("budget_discussion")
            evidence.append("need:budget_discussion")
        p["need_tags"] = sorted(need_tags)

    def _score(self, p: Dict[str, Any], reply_lang: str) -> Tuple[int, Dict[str, int]]:
        parts: Dict[str, int] = {}
        country = p.get("country")
        parts["country"] = 15 if country == "JP" else 10 if country == "JP_likely" or reply_lang == "ja" else 0
        gender = p.get("gender")
        if gender == "female":
            parts["gender"] = 15 if float(p.get("gender_confidence") or 0) >= 0.7 else 8
        elif gender == "male":
            parts["gender"] = -20
        else:
            parts["gender"] = 0

        age = self._age_overlap_score(p.get("age_range") or [])
        parts["age"] = age

        occ = p.get("occupation_tier")
        income = p.get("income_band")
        parts["income"] = 20 if income == "high" or occ == "high_income_signal" else 12 if income == "medium_high" or occ == "stable" else 0
        parts["lifestyle"] = 8 if p.get("lifestyle_tags") else 0

        needs = set(p.get("need_tags") or [])
        parts["need"] = 20 if "budget_discussion" in needs else 16 if "emotional_support" in needs else 12 if "consultation_interest" in needs else 0
        turns = int(p.get("turns") or 0)
        parts["trust"] = 10 if turns >= 8 else 7 if turns >= 5 else 4 if turns >= 3 else 0
        if p.get("low_value_signal"):
            parts["low_value_penalty"] = -20
        score = max(0, min(100, sum(parts.values())))
        return score, parts

    def _age_overlap_score(self, age_range: List[Any]) -> int:
        if not age_range or len(age_range) != 2:
            return 0
        lo, hi = int(age_range[0]), int(age_range[1])
        target = (int((self.cfg.get("target") or {}).get("age_min", 37)),
                  int((self.cfg.get("target") or {}).get("age_max", 60)))
        if hi < target[0] or lo > target[1]:
            return -25
        return 20

    def _missing_fields(self, p: Dict[str, Any]) -> List[str]:
        out: List[str] = []
        if p.get("country") == "unknown":
            out.append("country")
        if p.get("occupation_tier") == "unknown":
            out.append("occupation")
        if not p.get("age_range"):
            out.append("age_range")
        if p.get("gender") == "unknown":
            out.append("gender")
        if (
            p.get("income_band") == "unknown"
            and p.get("occupation_tier") != "high_income_signal"
            and not p.get("lifestyle_tags")
        ):
            out.append("income_signal")
        if not p.get("need_tags"):
            out.append("need")
        return out

    def _hard_disqualifier(self, p: Dict[str, Any]) -> str:
        if p.get("gender") == "male" and float(p.get("gender_confidence") or 0) >= 0.75:
            return "male_explicit"
        ar = p.get("age_range") or []
        if len(ar) == 2:
            if int(ar[1]) < int((self.cfg.get("target") or {}).get("age_min", 37)):
                return "age_below_target"
            if int(ar[0]) > int((self.cfg.get("target") or {}).get("age_max", 60)) + 5:
                return "age_above_target"
        if p.get("low_value_signal") and int(p.get("turns") or 0) >= 3:
            return "low_value_explicit"
        return ""

    def _stage_for(self, p: Dict[str, Any], score: int) -> str:
        if p.get("line_sent_at"):
            return "line_sent"
        if score >= int(self.cfg.get("min_score_for_line", 80)):
            return "qualified_lead"
        if int(p.get("turns") or 0) <= 2:
            return "new_lead"
        if len(p.get("missing_fields") or []) >= 3:
            return "profile_discovery"
        return "need_discovery"

    def _action_for(
        self, p: Dict[str, Any], score: int, hard: str, now: float,
    ) -> str:
        low_cfg = self.cfg.get("low_priority") or {}
        if hard:
            if int(p.get("low_value_turns") or 0) >= int(low_cfg.get("stop_after_low_value_turns", 3) or 3):
                return "silent_stop"
            return "low_priority"
        handoff = self.cfg.get("handoff") or {}
        min_score = int(self.cfg.get("min_score_for_line", 80) or 80)
        min_turns = int(handoff.get("min_turns_before_send", 6) or 6)
        cooldown_days = float(handoff.get("resend_cooldown_days", 14) or 14)
        last_line = float(p.get("line_sent_at") or 0)
        line_ready = (
            score >= min_score
            and int(p.get("turns") or 0) >= min_turns
            and (not last_line or now - last_line > cooldown_days * 86400)
            and bool(handoff.get("line_id") or self.cfg.get("line_id"))
            and bool(p.get("need_tags"))
        )
        if line_ready:
            return "handoff_line"
        if score < int(low_cfg.get("score_below", 40) or 40) and int(p.get("turns") or 0) >= 6:
            p["low_value_turns"] = int(p.get("low_value_turns") or 0) + 1
            return "low_priority"
        self._pick_next_question(p)
        return "continue"

    def _pick_next_question(self, p: Dict[str, Any]) -> None:
        turns = int(p.get("turns") or 0)
        q_cfg = self.cfg.get("question_policy") or {}
        min_age_turns = int(q_cfg.get("min_turns_before_age", 4) or 4)
        min_budget_turns = int(q_cfg.get("min_turns_before_budget", 6) or 6)
        missing = list(p.get("missing_fields") or [])
        field = ""
        if "country" in missing:
            field = "country"
        elif "occupation" in missing:
            field = "occupation"
        elif "age_range" in missing and turns >= min_age_turns:
            field = "age_range"
        elif "need" in missing:
            field = "need"
        elif "income_signal" in missing and turns >= min_budget_turns and p.get("need_tags"):
            field = "budget"
        p["next_question"] = field

    def _prompt_block(self, p: Dict[str, Any], action: str) -> str:
        if action == "handoff_line":
            return ""
        next_q = p.get("next_question", "")
        known = []
        for k in ("country", "gender", "age_range", "occupation", "occupation_tier", "income_band"):
            v = p.get(k)
            if v and v not in ("unknown", [], ""):
                known.append(f"{k}={v}")
        if p.get("lifestyle_tags"):
            known.append("lifestyle=" + ",".join(str(x) for x in p.get("lifestyle_tags")[:4]))
        if p.get("relationship_tags"):
            known.append("relationship=" + ",".join(str(x) for x in p.get("relationship_tags")[:4]))
        base = [
            "【内部客户筛选策略，不要向用户透露】",
            f"当前阶段={p.get('stage')}，ICP分={p.get('icp_score', 0)}/100。",
            "目标客户：日本语境、女性、37-60岁、高收入/高预算、有服务需求。",
            "沟通方式：像成熟、有同理心的人在私聊。先接住情绪，再轻轻回应；不要客服腔，不要像问卷。",
            "一次最多问一个轻问题；不要直接问年收入、资产、住址、证件、支付信息。",
            "线索采集优先级：职业/工作节奏 → 生活阶段/年龄段 → 经济能力信号 → 真实需求/困扰 → 是否愿意让人工客服继续沟通。",
            "女性心理沟通原则：尊重边界、给安全感、少评判、多共情；不要制造焦虑、不要承诺关系、不要利用孤独或压力推进转化。",
            "如果对方发图片/表情/语音：先自然回应内容或情绪；能识别内容时结合内容聊，不能识别时礼貌说明稍后认真看/听。",
        ]
        if known:
            base.append("已知线索：" + "；".join(known))
        if action == "low_priority":
            base.append("本轮策略：低优先级，短句礼貌回应，不主动延展，不发LINE。")
        elif next_q:
            prompts = {
                "country": "本轮可自然确认她是否在日本，例如顺着时区/天气问，不要突兀。",
                "occupation": "本轮可自然了解工作/职业，例如“今日はお仕事だったの？”或“普段どんなお仕事？”",
                "age_range": "如果气氛轻松，可用区间式自然确认年龄段；不要审问。",
                "need": "本轮可了解她最近有什么困扰、想咨询什么，先接情绪再轻问。",
                "budget": "只有她已表达需求时，才轻问大致预算感；不要问年收入。",
            }
            base.append("本轮可选目标：" + prompts.get(next_q, next_q))
        else:
            base.append("本轮策略：继续建立信任，少问问题，保持自然。")
        return "\n".join(base)

    def _handoff_reply(self) -> str:
        handoff = self.cfg.get("handoff") or {}
        line_id = str(handoff.get("line_id") or self.cfg.get("line_id") or "").strip()
        if not line_id:
            return ""
        template = str(handoff.get("template") or "").strip()
        if template:
            return template.replace("{LINE_ID}", line_id).strip()
        return (
            "ここだと見落としちゃうことがあるから、ちゃんと相談できる担当のLINEを置いておくね。\n"
            "無理に追加しなくて大丈夫だけど、詳しく聞きたいならこっちの方が早いよ。\n"
            f"LINE: {line_id}"
        )

    @staticmethod
    def _first_hit(text: str, terms: Tuple[str, ...]) -> str:
        for term in terms:
            if term in text:
                return term
        return ""

    @staticmethod
    def _decade_range(decade: int, qualifier: str = "") -> List[int]:
        if qualifier == "前半":
            return [decade, decade + 4]
        if qualifier == "半ば":
            return [decade + 3, decade + 6]
        if qualifier == "後半":
            return [decade + 5, decade + 9]
        return [decade, decade + 9]
