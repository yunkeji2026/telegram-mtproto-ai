"""主动话题"试发采样"评分回流（质量闭环）。

试发(_proactive_generate)生成 AI 实际会发的开场白但不发送；本表把每次采样落库，
并允许运营 👍好 / 👎改（可附改写文案）。累积后按 ``mode`` 看好评率，反推 prompt 与
``min_silent_hours`` / ``context_facts`` 条数等参数——把"能看→能评→能调"闭成数据循环。

约定（镜像 crisis_event_store）：单连接 ``check_same_thread=False`` + 写操作加锁 +
**绝不抛**（评分回流是旁路，不能影响采样/发送主流程）。支持 ``:memory:`` 便于单测。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("CompanionSampleStore")

_VALID_RATINGS = ("up", "down")


class CompanionSampleStore:
    _DDL = """
    CREATE TABLE IF NOT EXISTS companion_sample (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id TEXT NOT NULL DEFAULT '',
        account_id TEXT NOT NULL DEFAULT '',
        mode TEXT NOT NULL DEFAULT '',
        fact TEXT NOT NULL DEFAULT '',
        context_facts_n INTEGER NOT NULL DEFAULT 0,
        silent_hours REAL NOT NULL DEFAULT 0,
        text TEXT NOT NULL DEFAULT '',
        rating TEXT NOT NULL DEFAULT '',
        edited_text TEXT NOT NULL DEFAULT '',
        rated_by TEXT NOT NULL DEFAULT '',
        note TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL,
        rated_at REAL
    );
    CREATE INDEX IF NOT EXISTS idx_sample_created ON companion_sample(created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_sample_rating ON companion_sample(rating, created_at DESC);
    """

    def __init__(self, db_path: Any = ":memory:"):
        self._db_path = str(db_path)
        self._lock = threading.Lock()
        if self._db_path != ":memory:":
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.executescript(self._DDL)
        self._conn.commit()

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    def record_sample(
        self,
        *,
        conversation_id: str = "",
        account_id: str = "",
        mode: str = "",
        fact: str = "",
        context_facts_n: int = 0,
        silent_hours: float = 0.0,
        text: str = "",
    ) -> Optional[int]:
        """落一条采样；返回行 id，失败 None（绝不抛）。"""
        try:
            with self._lock:
                cur = self._conn.execute(
                    "INSERT INTO companion_sample (conversation_id, account_id, mode,"
                    " fact, context_facts_n, silent_hours, text, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(conversation_id), str(account_id), str(mode)[:32],
                        str(fact)[:200], int(context_facts_n or 0),
                        float(silent_hours or 0.0), str(text or "")[:500], time.time(),
                    ),
                )
                self._conn.commit()
                return int(cur.lastrowid) if cur.lastrowid else None
        except Exception as e:  # noqa: BLE001
            logger.debug("companion_sample record failed: %s", e)
            return None

    def rate(
        self, sample_id: int, rating: str, *,
        edited_text: str = "", rated_by: str = "", note: str = "",
    ) -> bool:
        """评分一条采样：rating ∈ {up,down}；可附改写文案。仅按 id 更新（幂等覆盖）。"""
        r = str(rating or "").strip().lower()
        if r not in _VALID_RATINGS:
            return False
        try:
            with self._lock:
                cur = self._conn.execute(
                    "UPDATE companion_sample SET rating = ?, edited_text = ?,"
                    " rated_by = ?, note = ?, rated_at = ? WHERE id = ?",
                    (r, str(edited_text or "")[:500], str(rated_by or "")[:64],
                     str(note or "")[:500], time.time(), int(sample_id)),
                )
                self._conn.commit()
                return bool(cur.rowcount)
        except Exception as e:  # noqa: BLE001
            logger.debug("companion_sample rate failed: %s", e)
            return False

    _COLS = [
        "id", "conversation_id", "account_id", "mode", "fact", "context_facts_n",
        "silent_hours", "text", "rating", "edited_text", "rated_by", "note",
        "created_at", "rated_at",
    ]

    def list_recent(self, *, limit: int = 50, rating: str = "") -> List[Dict[str, Any]]:
        lim = max(1, min(int(limit or 50), 500))
        where, params = "", []
        r = str(rating or "").strip().lower()
        if r in _VALID_RATINGS:
            where = " WHERE rating = ?"
            params.append(r)
        elif r == "unrated":
            where = " WHERE rating = ''"
        params.append(lim)
        try:
            rows = self._conn.execute(
                f"SELECT {', '.join(self._COLS)} FROM companion_sample{where}"
                " ORDER BY created_at DESC LIMIT ?", params,
            ).fetchall()
        except Exception as e:  # noqa: BLE001
            logger.debug("companion_sample list failed: %s", e)
            return []
        return [dict(zip(self._COLS, row)) for row in rows]

    def stats(self) -> Dict[str, Any]:
        """聚合：总数/已评/好评/差评/好评率 + 按 mode 分。供调参看板。"""
        out: Dict[str, Any] = {
            "total": 0, "rated": 0, "up": 0, "down": 0,
            "up_rate": None, "by_mode": {},
        }
        try:
            row = self._conn.execute(
                "SELECT COUNT(*),"
                " SUM(CASE WHEN rating IN ('up','down') THEN 1 ELSE 0 END),"
                " SUM(CASE WHEN rating='up' THEN 1 ELSE 0 END),"
                " SUM(CASE WHEN rating='down' THEN 1 ELSE 0 END)"
                " FROM companion_sample"
            ).fetchone()
            if row:
                out["total"] = int(row[0] or 0)
                out["rated"] = int(row[1] or 0)
                out["up"] = int(row[2] or 0)
                out["down"] = int(row[3] or 0)
                if out["rated"] > 0:
                    out["up_rate"] = round(out["up"] / out["rated"], 3)
            mrows = self._conn.execute(
                "SELECT mode,"
                " SUM(CASE WHEN rating='up' THEN 1 ELSE 0 END),"
                " SUM(CASE WHEN rating='down' THEN 1 ELSE 0 END)"
                " FROM companion_sample GROUP BY mode"
            ).fetchall()
            for m, up, down in mrows:
                out["by_mode"][str(m or "")] = {"up": int(up or 0), "down": int(down or 0)}
        except Exception as e:  # noqa: BLE001
            logger.debug("companion_sample stats failed: %s", e)
        return out

    def count(self) -> int:
        try:
            row = self._conn.execute("SELECT COUNT(*) FROM companion_sample").fetchone()
            return int(row[0]) if row else 0
        except Exception:
            return 0


def _verdict(up_rate: Optional[float], rated: int, min_samples: int,
             low_up_rate: float) -> str:
    if rated < min_samples or up_rate is None:
        return "insufficient"
    if up_rate >= 0.8:
        return "good"
    if up_rate >= low_up_rate:
        return "watch"
    return "low"


# 各 mode 命中低好评率时的针对性调参方向（人审，不自动改配置）。
_MODE_HINTS = {
    "follow_up": [
        "收紧 directive 措辞（一句关心即可，避免追问感）",
        "减少 context_facts 背景条数（可能信息过载，反显刻意）",
        "检查所选记忆是否陈旧/低置信（select_proactive_topic 排序或 stale 过滤）",
    ],
    "gentle_checkin": [
        "无记忆开场易空泛：下调 min_silent_hours 让更多有记忆钩子的会话走 follow_up",
        "丰富温和问候模板，避免千篇一律",
    ],
    "ask_birthday": [
        "问得像查户口/填表：收紧 directive，先共情问候再随口好奇带一句，别一上来就要信息",
        "太频繁/被反感：上调 birthday_ask.cooldown_days 或 min_intimacy，只对够熟的人问",
        "不自然：放在 gentle_checkin（最没话说时）顺势问，别硬塞进有记忆钩子的回访",
    ],
    "story_invite": [
        "邀约太硬/像广告：收紧 directive，先共情再顺势提议，别一上来报剧情名",
        "续作召回核对前传共同经历是否被自然提起（satisfied_prerequisite/ending_memory）",
        "若打扰感强：上调 cooldown_hours 或仅对高 bond 用户邀约（min_bond_level 内容侧调高）",
    ],
    "story_teaser": [
        "预告像推销/逼单：删一切价格暗示，只勾「向往+一起经历」的期待感，softer 措辞",
        "转化低：核对是否只对「关系/前置已满足、只差付费」的用户发（need_unlock-only），别推够不着的",
        "打扰/反感：付费预告比免费邀约更敏感——上调 cooldown 或仅对高 bond/高活跃用户开 paid_teaser",
    ],
    "ritual_morning": [
        "早安太生分/像久别重逢：核对 build_proactive_prompt 走了仪式框定（非「许久未联系」）",
        "千篇一律：让 directive 偶尔自然轻提一句 TA 在意的记忆（一句带过、别追问）",
        "扰民：上调 min_quiet_gap_hours 或收窄 morning_window，只在用户真实活跃晨点发",
    ],
    "ritual_night": [
        "晚安太敷衍/套路：收紧 directive，温柔放松、像睡前真的想起 TA",
        "时点不对：开 personalize_active_hour，按用户历史活跃晚点择时（夜猫子晚发）",
        "扰民：上调 min_quiet_gap_hours，人还在场就别硬道晚安",
    ],
    "milestone_birthday": [
        "生日祝福像贺卡套话：收紧 directive，结合 TA 的具体记忆/近况，写独一无二的那句",
        "发错日子：核对 resolve_birthday 抽取（要求带生日关键词）与时区，宁可漏发别错发",
        "扰民/尴尬：仅对关系够深用户庆生，关系浅时点到为止",
    ],
    "milestone_anniversary": [
        "纪念日太隆重/煽情：收紧 directive，像随口记得的人轻轻一提，别仪式感过载",
        "天数算错/发错日子：核对 first_seen_ts 与 anniversary_days 里程碑配置",
        "扰民：上调 min_intimacy，只对关系够深的用户庆「认识 N 天」",
    ],
    "milestone_holiday": [
        "节日祝福像群发套话：收紧 directive，结合 TA 的具体处境/记忆，去模板化",
        "发错日子：农历节日逐年漂移，别写死，按年在 holiday_calendar 配置当年公历日期",
        "扰民/没共鸣：只对关系够深用户发，冷门节日按受众取舍",
    ],
}


def build_tuning_advice(
    stats: Dict[str, Any],
    rated_samples: List[Dict[str, Any]],
    *,
    min_samples: int = 5,
    low_up_rate: float = 0.6,
    max_examples: int = 5,
) -> Dict[str, Any]:
    """由采样统计 + 已评样本产出"调参建议"（纯函数，只读建议、绝不改配置）。

    Args:
        stats: ``CompanionSampleStore.stats()`` 输出（total/rated/up/down/up_rate/by_mode）。
        rated_samples: 已评样本行（含 rating/text/edited_text/mode），用于挑 few-shot。
        min_samples: 低于此评分数不下结论（样本不足）。
        low_up_rate: 低于此好评率判 "low" 并给针对性建议。

    Returns:
        ``{overall, by_mode:[...], suggestions:[...], few_shot:{liked, improved}}``。
    """
    stats = stats or {}
    rated = int(stats.get("rated") or 0)
    overall_rate = stats.get("up_rate")
    overall_verdict = _verdict(overall_rate, rated, min_samples, low_up_rate)

    by_mode: List[Dict[str, Any]] = []
    for mode, mc in (stats.get("by_mode") or {}).items():
        up = int((mc or {}).get("up") or 0)
        down = int((mc or {}).get("down") or 0)
        mrated = up + down
        rate = round(up / mrated, 3) if mrated else None
        verdict = _verdict(rate, mrated, min_samples, low_up_rate)
        suggestions = list(_MODE_HINTS.get(mode, [])) if verdict == "low" else []
        by_mode.append({
            "mode": mode, "rated": mrated, "up": up, "down": down,
            "up_rate": rate, "verdict": verdict, "suggestions": suggestions,
        })
    by_mode.sort(key=lambda m: (m["up_rate"] if m["up_rate"] is not None else 1.0))

    suggestions: List[str] = []
    if overall_verdict == "insufficient":
        suggestions.append(
            f"样本不足（已评 {rated}，建议先攒到 ≥{min_samples} 条再下结论）。")
    elif overall_verdict == "low":
        suggestions.append(
            f"整体好评率偏低（{round((overall_rate or 0) * 100)}%）：优先看下方低分 mode 的针对性建议。")
    elif overall_verdict == "watch":
        suggestions.append("整体可用但仍有提升空间：参考差评改写样本微调措辞。")
    else:
        suggestions.append("整体表现良好，保持当前 prompt/阈值。")

    liked: List[str] = []
    improved: List[Dict[str, str]] = []
    for row in rated_samples or []:
        rating = str(row.get("rating") or "")
        if rating == "up" and len(liked) < max_examples:
            t = str(row.get("text") or "").strip()
            if t:
                liked.append(t)
        elif rating == "down" and len(improved) < max_examples:
            better = str(row.get("edited_text") or "").strip()
            if better:
                improved.append({
                    "original": str(row.get("text") or "").strip(),
                    "better": better,
                    "mode": str(row.get("mode") or ""),
                })

    return {
        "overall": {"rated": rated, "up_rate": overall_rate, "verdict": overall_verdict},
        "by_mode": by_mode,
        "suggestions": suggestions,
        "few_shot": {"liked": liked, "improved": improved},
    }


def build_few_shot_block(
    rated_samples: List[Dict[str, Any]], *, max_examples: int = 3, mode: str = "",
) -> str:
    """由人工认可样本拼"风格示范" prompt 块；无样本返回 ""（绝不抛）。

    优先级：差评的运营改写文案（``edited_text``，人工亲手打磨的"更好版"）> 高赞原文
    （``rating=='up'`` 的 ``text``）。**只作口吻示范，明确叮嘱不照抄内容**——与
    select_proactive_topic 的克制纪律一致（避免把别人的具体事搬到当前会话）。

    ``mode``：非空时**只用该 mode 的样本**（follow_up / gentle_checkin 各用各的口吻，
    不交叉污染——回访型与温和问候型语气本就不同）。该 mode 暂无样本则返回 ""。
    """
    n = max(0, int(max_examples or 0))
    if n <= 0:
        return ""
    want_mode = str(mode or "").strip()
    improved: List[str] = []
    liked: List[str] = []
    for row in rated_samples or []:
        if want_mode and str(row.get("mode") or "") != want_mode:
            continue
        rating = str(row.get("rating") or "")
        if rating == "down":
            better = str(row.get("edited_text") or "").strip()
            if better:
                improved.append(better)
        elif rating == "up":
            t = str(row.get("text") or "").strip()
            if t:
                liked.append(t)
    examples: List[str] = []
    for e in improved + liked:
        if e and e not in examples:
            examples.append(e)
        if len(examples) >= n:
            break
    if not examples:
        return ""
    lines = "\n".join(f"- {e}" for e in examples)
    return (
        "\n【风格示范】以下是过往人工认可的开场口吻，只学其自然、温暖、简短的风格，"
        "绝不要照抄其中的具体内容/事件：\n" + lines + "\n"
    )


_SINGLETON: Optional[CompanionSampleStore] = None


def get_companion_sample_store(db_path: Any = ":memory:") -> CompanionSampleStore:
    """进程内单例（与 care_schedule 同范式）。"""
    global _SINGLETON
    if _SINGLETON is None:
        _SINGLETON = CompanionSampleStore(db_path)
    return _SINGLETON


__all__ = [
    "CompanionSampleStore", "get_companion_sample_store", "build_tuning_advice",
    "build_few_shot_block",
]
