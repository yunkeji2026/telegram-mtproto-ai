"""
Education domain hook — homework questions, course queries, exam topics.
"""

import re
from typing import Any, Dict, List, Optional, Set

from src.hooks.base import DomainHook, HookContext

_HOMEWORK_PAT = re.compile(
    r"作业|homework|习题|题目|证明|calculate|求解|这道题",
    re.IGNORECASE,
)
_COURSE_PAT = re.compile(
    r"课程|课表|大纲|syllabus|学分|选课|教材|课件",
    re.IGNORECASE,
)
_EXAM_PAT = re.compile(
    r"考试|期中|期末|quiz|midterm|final|考点|复习",
    re.IGNORECASE,
)


class EducationDomainHook(DomainHook):
    """Education: route homework / course / exam style messages."""

    def __init__(self, config=None):
        self._config = config

    async def on_intent_resolved(self, intent: str, ctx: HookContext) -> str:
        text = (ctx.text or "").strip()
        if not text:
            return intent
        if _HOMEWORK_PAT.search(text):
            return "homework_help"
        if _EXAM_PAT.search(text):
            return "exam_prep"
        if _COURSE_PAT.search(text):
            return "course_info"
        return intent

    def get_narrow_reply_config(self) -> Optional[Dict[str, Any]]:
        return {
            "education_topic_substrings": [
                "作业", "考试", "课程", "复习", "考点", "证明", "公式",
            ],
        }

    def get_extra_intent_keywords(self) -> Dict[str, List[str]]:
        return {
            "homework_help": ["作业", "习题", "题目", "homework"],
            "exam_prep": ["考试", "复习", "考点", "期末"],
            "course_info": ["课程", "大纲", "课表", "学分"],
        }

    def get_followup_config(self) -> Dict[str, Any]:
        return {
            "followup_intents": {"homework_help", "exam_prep"},
            "is_short_followup": self.is_short_followup,
            "looks_like_summary": self.last_reply_looks_like_summary,
        }

    def is_short_followup(self, text: str) -> bool:
        t = (text or "").strip()
        if not t or self.is_meaningless_interjection(t):
            return False
        if len(t) > 24:
            return False
        short = ("为什么", "然后呢", "哪一步", "不懂", "为什么这样", "why", "how")
        return any(s in t.lower() for s in short)

    def last_reply_looks_like_summary(self, reply: str) -> bool:
        r = (reply or "").strip()
        return len(r) > 40 and ("步骤" in r or "1." in r or "首先" in r)

    def get_ambiguous_tokens(self) -> Set[str]:
        return {"hw", "exam", "gpa"}
