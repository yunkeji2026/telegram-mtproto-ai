"""
每日自动学习模块
- 汇总 KB 未命中、弱命中、隐式负反馈
- 用 AI 自动生成知识条目草稿
- 存入 kb_drafts 表等人工审核
- 审核通过后一键入库
"""

import asyncio
import json
import logging
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional

_logger = logging.getLogger("ai_chat_assistant.DailyLearner")


class DailyLearner:

    DRAFTS_SCHEMA = """
    CREATE TABLE IF NOT EXISTS kb_drafts (
        id              TEXT PRIMARY KEY,
        source          TEXT NOT NULL,
        query           TEXT NOT NULL,
        hit_count       INTEGER DEFAULT 1,
        category        TEXT DEFAULT '',
        title           TEXT DEFAULT '',
        triggers        TEXT DEFAULT '',
        example_reply   TEXT DEFAULT '',
        ai_reasoning    TEXT DEFAULT '',
        status          TEXT DEFAULT 'pending',
        reviewed_by     TEXT DEFAULT '',
        created_at      TEXT NOT NULL,
        reviewed_at     TEXT DEFAULT ''
    );
    """

    def __init__(self, kb_store, ai_client, db_path: Optional[Path] = None):
        self._kb = kb_store
        self._ai = ai_client
        self._db_path = db_path or (
            Path(kb_store._db_path).parent / "knowledge_base.db"
        )
        self._ensure_table()

    def _ensure_table(self):
        conn = sqlite3.connect(str(self._db_path))
        conn.execute(self.DRAFTS_SCHEMA)
        conn.commit()
        conn.close()

    def _conn(self):
        c = sqlite3.connect(str(self._db_path))
        c.row_factory = sqlite3.Row
        return c

    def collect_learning_material(self, min_miss_count: int = 2,
                                  max_items: int = 20) -> List[Dict]:
        """
        从三个来源收集需要学习的素材：
        1. miss_log 高频未命中
        2. kb_feedback 负反馈（score <= 0 且未处理）
        3. 弱命中（已有条目但匹配分数低）
        """
        materials = []
        seen_queries = set()

        # 来源 1：高频未命中
        miss_stats = self._kb.get_miss_stats(top_k=30)
        for m in miss_stats:
            q = m["query"].strip()
            if q.startswith("[TRANSLATE:"):
                continue
            if m["cnt"] >= min_miss_count and q not in seen_queries:
                materials.append({
                    "source": "miss",
                    "query": q,
                    "count": m["cnt"],
                    "last_at": m["last_at"],
                })
                seen_queries.add(q)

        # 来源 2：负反馈
        try:
            feedbacks = self._kb.list_feedback(limit=50)
            for fb in feedbacks:
                if fb.get("score", 0) <= 0 and not fb.get("added_to_examples"):
                    q = fb.get("user_message", "").strip()
                    if q and q not in seen_queries:
                        materials.append({
                            "source": "negative_feedback",
                            "query": q,
                            "count": 1,
                            "ai_reply": fb.get("ai_reply", ""),
                            "correction": fb.get("correction", ""),
                        })
                        seen_queries.add(q)
        except Exception:
            pass

        # 来源 3：弱命中
        try:
            suggestions = self._kb.get_auto_suggestions(
                weak_threshold=0.45, hours=168, top_k=15
            )
            for s in suggestions:
                if s.get("source") == "weak_hit":
                    q = s.get("query", "").strip()
                    if q and q not in seen_queries:
                        materials.append({
                            "source": "weak_hit",
                            "query": q,
                            "count": s.get("count", 1),
                            "avg_score": s.get("avg_score", 0),
                        })
                        seen_queries.add(q)
        except Exception:
            pass

        # 去掉已有草稿
        existing = set()
        with self._conn() as c:
            rows = c.execute(
                "SELECT query FROM kb_drafts WHERE status IN ('pending','approved')"
            ).fetchall()
            existing = {r["query"] for r in rows}
        materials = [m for m in materials if m["query"] not in existing]

        materials.sort(key=lambda x: -x["count"])
        return materials[:max_items]

    async def generate_drafts(self, materials: List[Dict],
                              domain_context: str = "") -> List[Dict]:
        """用 AI 批量生成知识条目草稿"""
        if not materials:
            _logger.info("没有需要学习的素材")
            return []

        categories = []
        try:
            from src.utils.kb_store import KB_CATEGORIES
            categories = KB_CATEGORIES
        except Exception:
            categories = ["常规咨询", "其他"]

        drafts = []
        batch_size = 5
        for i in range(0, len(materials), batch_size):
            batch = materials[i:i + batch_size]
            batch_drafts = await self._generate_batch(batch, categories, domain_context)
            drafts.extend(batch_drafts)
            if i + batch_size < len(materials):
                await asyncio.sleep(1)

        return drafts

    async def _generate_batch(self, batch: List[Dict], categories: List[str],
                              domain_context: str) -> List[Dict]:
        questions_text = ""
        for idx, m in enumerate(batch, 1):
            extra = ""
            if m.get("ai_reply"):
                extra += f"\n   AI当时回复: {m['ai_reply'][:100]}"
            if m.get("correction"):
                extra += f"\n   用户纠正: {m['correction'][:100]}"
            if m.get("avg_score"):
                extra += f"\n   匹配分数: {m['avg_score']:.2f}（偏低）"
            questions_text += f"{idx}. 用户问: \"{m['query']}\" (被问{m['count']}次){extra}\n"

        cat_list = "、".join(categories) if categories else "常规咨询、其他"

        prompt = f"""你是知识库管理助手。以下是客服系统中用户经常问但知识库没有覆盖的问题。
请为每个问题生成一个知识条目草稿。

{domain_context}

可选分类: {cat_list}

用户问题列表:
{questions_text}

请为每个问题输出 JSON 数组，每个元素包含:
- "index": 对应问题编号
- "category": 所属分类
- "title": 条目标题（简短概括）
- "triggers": 触发关键词数组（3-5个）
- "example_reply": 建议的标准回复（口语化、简洁、专业）
- "reasoning": 你的判断理由（一句话）

只输出 JSON 数组，不要其他内容。"""

        try:
            response = await self._ai.generate_reply(
                user_message=prompt,
                context={"_skip_emotion": True, "_skip_kb": True},
                strategy_overrides={"temperature": 0.3, "max_output_tokens": 2048}
            )
            if not response:
                return []

            json_str = response.strip()
            if json_str.startswith("```"):
                json_str = json_str.split("\n", 1)[-1].rsplit("```", 1)[0]

            items = json.loads(json_str)
            if not isinstance(items, list):
                return []

            drafts = []
            for item in items:
                idx = item.get("index", 0) - 1
                if 0 <= idx < len(batch):
                    m = batch[idx]
                    drafts.append({
                        "source": m["source"],
                        "query": m["query"],
                        "hit_count": m["count"],
                        "category": item.get("category", "其他"),
                        "title": item.get("title", m["query"][:30]),
                        "triggers": item.get("triggers", []),
                        "example_reply": item.get("example_reply", ""),
                        "ai_reasoning": item.get("reasoning", ""),
                    })
            return drafts

        except json.JSONDecodeError:
            _logger.warning("AI 返回的 JSON 解析失败")
            return []
        except Exception as e:
            _logger.error("AI 生成草稿失败: %s", e)
            return []

    def save_drafts(self, drafts: List[Dict]) -> int:
        """保存草稿到数据库"""
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        saved = 0
        with self._conn() as c:
            for d in drafts:
                draft_id = str(uuid.uuid4())[:8]
                triggers = d.get("triggers", [])
                if isinstance(triggers, list):
                    triggers = ",".join(triggers)
                try:
                    c.execute(
                        "INSERT INTO kb_drafts "
                        "(id,source,query,hit_count,category,title,triggers,"
                        "example_reply,ai_reasoning,status,created_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (draft_id, d["source"], d["query"], d.get("hit_count", 1),
                         d.get("category", ""), d.get("title", ""),
                         triggers, d.get("example_reply", ""),
                         d.get("ai_reasoning", ""), "pending", now)
                    )
                    saved += 1
                except sqlite3.IntegrityError:
                    pass
        return saved

    async def run_daily_learn(self, domain_context: str = "") -> Dict:
        """执行一次完整的学习流程"""
        _logger.info("开始每日自动学习...")

        materials = self.collect_learning_material()
        _logger.info("收集到 %d 条学习素材", len(materials))

        if not materials:
            return {"collected": 0, "generated": 0, "saved": 0}

        drafts = await self.generate_drafts(materials, domain_context)
        _logger.info("AI 生成了 %d 条草稿", len(drafts))

        saved = self.save_drafts(drafts)
        _logger.info("保存了 %d 条草稿，等待人工审核", saved)

        # 清理已处理的 miss_log 条目
        for m in materials:
            if m["source"] == "miss":
                try:
                    self._kb.delete_miss_entry(m["query"])
                except Exception:
                    pass

        return {"collected": len(materials), "generated": len(drafts), "saved": saved}

    # ── 草稿管理 API ──

    def list_drafts(self, status: str = "pending", limit: int = 50) -> List[Dict]:
        with self._conn() as c:
            if status == "all":
                rows = c.execute(
                    "SELECT * FROM kb_drafts ORDER BY created_at DESC LIMIT ?",
                    (limit,)
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM kb_drafts WHERE status=? ORDER BY created_at DESC LIMIT ?",
                    (status, limit)
                ).fetchall()
        return [dict(r) for r in rows]

    def get_draft(self, draft_id: str) -> Optional[Dict]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM kb_drafts WHERE id=?", (draft_id,)).fetchone()
        return dict(row) if row else None

    def update_draft(self, draft_id: str, data: Dict) -> bool:
        """编辑草稿（审核前可以修改标题、回复等）"""
        fields = []
        values = []
        for key in ("category", "title", "triggers", "example_reply"):
            if key in data:
                fields.append(f"{key}=?")
                values.append(data[key])
        if not fields:
            return False
        values.append(draft_id)
        with self._conn() as c:
            c.execute(
                f"UPDATE kb_drafts SET {','.join(fields)} WHERE id=?",
                values
            )
        return True

    def approve_draft(self, draft_id: str, operator: str = "") -> Optional[str]:
        """审核通过：将草稿入库为正式知识条目"""
        draft = self.get_draft(draft_id)
        if not draft or draft["status"] != "pending":
            return None

        triggers = draft.get("triggers", "")
        if isinstance(triggers, str):
            triggers = [t.strip() for t in triggers.split(",") if t.strip()]

        entry_id = self._kb.add_entry({
            "category": draft.get("category", "其他"),
            "title": draft["title"],
            "triggers": triggers,
            "example_reply_zh": draft.get("example_reply", ""),
            "reply_mode": "ai_strict",
            "enabled": True,
        })

        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as c:
            c.execute(
                "UPDATE kb_drafts SET status='approved', reviewed_by=?, reviewed_at=? WHERE id=?",
                (operator, now, draft_id)
            )

        _logger.info("草稿 %s 已审核通过，入库为条目 %s", draft_id, entry_id)
        return entry_id

    def reject_draft(self, draft_id: str, operator: str = "") -> bool:
        """拒绝草稿"""
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as c:
            c.execute(
                "UPDATE kb_drafts SET status='rejected', reviewed_by=?, reviewed_at=? WHERE id=?",
                (operator, now, draft_id)
            )
        return True

    def approve_all_pending(self, operator: str = "") -> int:
        """一键全部通过"""
        drafts = self.list_drafts(status="pending", limit=200)
        count = 0
        for d in drafts:
            if self.approve_draft(d["id"], operator):
                count += 1
        return count

    def stats(self) -> Dict:
        with self._conn() as c:
            pending = c.execute("SELECT COUNT(*) FROM kb_drafts WHERE status='pending'").fetchone()[0]
            approved = c.execute("SELECT COUNT(*) FROM kb_drafts WHERE status='approved'").fetchone()[0]
            rejected = c.execute("SELECT COUNT(*) FROM kb_drafts WHERE status='rejected'").fetchone()[0]
        return {"pending": pending, "approved": approved, "rejected": rejected}
