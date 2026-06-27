"""
Episodic memory: short, persistent user-specific facts for multi-session continuity.
Stored in SQLite (default: same file as ContextStore bot.db), separate table.
"""

from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("EpisodicMemoryStore")

# R3：稳定层（已巩固的人设级记忆）在重排时的小幅加权（仅 use_salience_rerank 时生效）
_STABLE_TIER_BOOST = 0.05


def compute_memory_storage_key(scope: str, user_id_str: str, chat_id: Any) -> str:
    """
    scope=user → 仅 user_id；scope=chat_user → 群为「chat_user_id」，私聊 chat_id==user 时退化为 user。
    """
    if (scope or "user") != "chat_user":
        return user_id_str
    try:
        cid = int(chat_id) if chat_id is not None and str(chat_id).strip() != "" else 0
    except (TypeError, ValueError):
        cid = 0
    try:
        uid = int(str(user_id_str).strip()) if str(user_id_str).strip().isdigit() else 0
    except (TypeError, ValueError):
        uid = 0
    if cid != 0 and uid != 0 and cid == uid:
        return user_id_str
    if cid == 0:
        return user_id_str
    return f"{cid}_{user_id_str}"


def _norm_for_hash(text: str) -> str:
    t = (text or "").strip().lower()
    t = re.sub(r"\s+", " ", t)
    return t[:500]


class EpisodicMemoryStore:
    _DDL = """
    CREATE TABLE IF NOT EXISTS episodic_memory (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        content TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        category TEXT NOT NULL DEFAULT 'general',
        created_at REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_epi_user_created ON episodic_memory(user_id, created_at DESC);
    CREATE UNIQUE INDEX IF NOT EXISTS idx_epi_user_hash ON episodic_memory(user_id, content_hash);
    """

    def __init__(self, db_path: Path):
        self._db_path = Path(db_path)
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    def _ensure_embedding_column(self) -> None:
        cur = self._conn.execute("PRAGMA table_info(episodic_memory)")
        cols = [str(r[1]) for r in cur.fetchall()]
        if "embedding" not in cols:
            self._conn.execute("ALTER TABLE episodic_memory ADD COLUMN embedding BLOB")
            self._conn.commit()
            logger.info("episodic_memory: added column embedding")

    def _ensure_consolidation_columns(self) -> None:
        """R3：分层巩固所需列（写入即定权 + 复发计数 + 分层），向后兼容 ALTER。"""
        cur = self._conn.execute("PRAGMA table_info(episodic_memory)")
        cols = [str(r[1]) for r in cur.fetchall()]
        migrations = [
            ("salience", "ALTER TABLE episodic_memory ADD COLUMN salience REAL"),
            ("tier", "ALTER TABLE episodic_memory ADD COLUMN tier TEXT NOT NULL DEFAULT 'raw'"),
            ("hits", "ALTER TABLE episodic_memory ADD COLUMN hits INTEGER NOT NULL DEFAULT 1"),
            ("last_seen", "ALTER TABLE episodic_memory ADD COLUMN last_seen REAL"),
        ]
        changed = False
        for col, ddl in migrations:
            if col not in cols:
                self._conn.execute(ddl)
                changed = True
                logger.info("episodic_memory: added column %s", col)
        if changed:
            # 老行 last_seen 回填为 created_at，便于衰减/巩固判断
            self._conn.execute(
                "UPDATE episodic_memory SET last_seen = created_at WHERE last_seen IS NULL"
            )
            self._conn.commit()

    def _ensure_source_column(self) -> None:
        """R12：事实来源标注列（``user_stated`` / ``ai_inferred``），向后兼容 ALTER。

        旧行默认 ``user_stated``（视既有事实为用户明说，行为不变）；新写入由调用方按
        来源标注——启发式（从用户原话正则提取）= ``user_stated``，LLM 抽取（对话推断/
        概括）= ``ai_inferred``。用于晋升/推翻 stable 时按置信分级。
        """
        cur = self._conn.execute("PRAGMA table_info(episodic_memory)")
        cols = [str(r[1]) for r in cur.fetchall()]
        if "source" not in cols:
            self._conn.execute(
                "ALTER TABLE episodic_memory ADD COLUMN source TEXT NOT NULL"
                " DEFAULT 'user_stated'"
            )
            self._conn.commit()
            logger.info("episodic_memory: added column source")

    def _init_db(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.executescript(self._DDL)
        self._conn.commit()
        self._ensure_embedding_column()
        self._ensure_consolidation_columns()
        self._ensure_source_column()

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def add_fact(
        self,
        user_id: str,
        content: str,
        category: str = "general",
        embedding_blob: Optional[bytes] = None,
        source: str = "user_stated",
    ) -> Optional[int]:
        """Insert one fact; returns new row id, or None if duplicate / failed.

        R3：写入即算情绪显著性落 ``salience`` 列；重复事实（同 hash）不再插入，但
        **累加 ``hits`` 并刷新 ``last_seen``**——把"反复提起"沉淀为复发信号，供
        ``consolidate`` 把高复发事实晋升为 ``stable`` 稳定层。重复仍返回 None（向后兼容）。

        R12：``source`` 标注来源（``user_stated`` / ``ai_inferred``），供 source-aware
        巩固/推翻按置信分级（默认 ``user_stated``，兼容旧调用）。
        """
        c = (content or "").strip()
        if len(c) < 2 or len(c) > 500:
            return None
        src = source if source in ("user_stated", "ai_inferred") else "user_stated"
        h = hashlib.sha256(_norm_for_hash(c).encode("utf-8")).hexdigest()
        now = time.time()
        sal = self._compute_salience(c)
        try:
            cur = self._conn.execute(
                "INSERT INTO episodic_memory (user_id, content, content_hash, category,"
                " created_at, embedding, salience, tier, hits, last_seen, source)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 'raw', 1, ?, ?)",
                (user_id, c, h, (category or "general")[:32], now, embedding_blob, sal, now, src),
            )
            self._conn.commit()
            return int(cur.lastrowid) if cur.lastrowid else None
        except sqlite3.IntegrityError:
            # 复发：累加 hits + 刷新 last_seen（不新增行；返回 None 保持兼容）。
            # R12：若本次为用户明说（user_stated），把既有行升格为 user_stated——
            # "AI 先推断、用户后亲口确认" = 置信升级；绝不反向降级。
            try:
                if src == "user_stated":
                    self._conn.execute(
                        "UPDATE episodic_memory SET hits = hits + 1, last_seen = ?,"
                        " source = 'user_stated'"
                        " WHERE user_id = ? AND content_hash = ?",
                        (now, user_id, h),
                    )
                else:
                    self._conn.execute(
                        "UPDATE episodic_memory SET hits = hits + 1, last_seen = ?"
                        " WHERE user_id = ? AND content_hash = ?",
                        (now, user_id, h),
                    )
                self._conn.commit()
            except Exception as e:  # noqa: BLE001
                logger.debug("episodic recurrence bump failed: %s", e)
            return None
        except Exception as e:
            logger.debug("episodic insert failed: %s", e)
            return None

    def list_key_stats(self) -> List[Tuple[str, int]]:
        """返回 ``[(user_id_key, fact_count), ...]``，按事实数降序。

        供跨平台记忆 key 迁移工具盘点：哪些是「裸 key」（旧产线遗留、未带 platform
        前缀），需要并入 canonical key 才能被新收件箱产线命中。
        """
        try:
            rows = self._conn.execute(
                "SELECT user_id, COUNT(*) FROM episodic_memory"
                " GROUP BY user_id ORDER BY COUNT(*) DESC"
            ).fetchall()
            return [(str(r[0]), int(r[1])) for r in rows]
        except Exception as e:
            logger.debug("list_key_stats failed: %s", e)
            return []

    def key_health(self, sample: int = 10) -> Dict[str, Any]:
        """记忆 key 健康概览：盘点「裸 key」（无 ``platform:`` 前缀）漂移。

        裸 key 是旧产线遗留或某入口漏传 platform 的产物——新收件箱引擎按 canonical
        (``platform:uid``) 读取，裸 key 下的记忆对其不可见 → 拉低命中率。一次性迁移
        （:mod:`src.utils.episodic_key_migration`）清掉存量后，本探针让**复发可观测**：
        运维/看板随时能看到 bare_keys 是否回升，而非靠低命中率事后倒查。

        返回 ``{total_keys, canonical_keys, bare_keys, bare_facts, bare_ratio,
        bare_samples:[{key,facts}...]}``；store 异常返回零值（绝不抛）。
        """
        try:
            stats = self.list_key_stats()
        except Exception:
            stats = []
        total_keys = len(stats)
        bare = [(k, n) for k, n in stats if ":" not in str(k)]
        n_sample = max(0, int(sample or 0))
        return {
            "total_keys": total_keys,
            "canonical_keys": total_keys - len(bare),
            "bare_keys": len(bare),
            "bare_facts": sum(n for _, n in bare),
            "bare_ratio": round(len(bare) / total_keys, 4) if total_keys else 0.0,
            "bare_samples": [{"key": k, "facts": n} for k, n in bare[:n_sample]],
        }

    def merge_key(self, old_key: str, new_key: str) -> int:
        """把 ``old_key`` 的事实并入 ``new_key``（幂等、按 content_hash 去重）。

        用 ``UPDATE OR IGNORE`` 迁移：与目标 key 内容重复的行迁移被忽略（保留目标既
        有），再 ``DELETE`` 掉旧 key 残留行。返回成功迁移（非重复）的行数。
        """
        old_key = str(old_key or "")
        new_key = str(new_key or "")
        if not old_key or not new_key or old_key == new_key:
            return 0
        try:
            cur = self._conn.execute(
                "UPDATE OR IGNORE episodic_memory SET user_id=? WHERE user_id=?",
                (new_key, old_key),
            )
            moved = int(cur.rowcount or 0)
            # 删除因 (user_id, content_hash) 冲突未能迁移的旧残留行
            self._conn.execute(
                "DELETE FROM episodic_memory WHERE user_id=?", (old_key,)
            )
            self._conn.commit()
            return moved
        except Exception as e:
            logger.debug("merge_key %s→%s failed: %s", old_key, new_key, e)
            try:
                self._conn.rollback()
            except Exception:
                pass
            return 0

    @staticmethod
    def _compute_salience(content: str) -> Optional[float]:
        """写入期算一次情绪显著性（0-1）；失败回 None，不阻断写入。"""
        try:
            from src.utils.memory_salience import salience_score
            return round(float(salience_score(content)), 4)
        except Exception:
            return None

    def update_embedding(self, row_id: int, embedding_blob: bytes) -> bool:
        if not embedding_blob:
            return False
        try:
            cur = self._conn.execute(
                "UPDATE episodic_memory SET embedding = ? WHERE id = ?",
                (embedding_blob, int(row_id)),
            )
            self._conn.commit()
            return int(cur.rowcount or 0) > 0
        except Exception as e:
            logger.debug("episodic update_embedding failed: %s", e)
            return False

    def count(self, user_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM episodic_memory WHERE user_id = ?", (user_id,)
        ).fetchone()
        return int(row[0]) if row else 0

    def prune_oldest(self, user_id: str, keep: int) -> int:
        """Keep at most `keep` rows (by recency). Returns deleted count.

        R3：``stable`` 稳定层（已巩固的人设级记忆）**永不被裁剪**——只淘汰 ``raw`` 层
        的最旧者，使长期重要记忆不会因近期琐事刷量而被挤掉。
        """
        n = self.count(user_id)
        if n <= keep:
            return 0
        to_drop = n - keep
        cur = self._conn.execute(
            """
            DELETE FROM episodic_memory WHERE id IN (
                SELECT id FROM episodic_memory
                WHERE user_id = ? AND COALESCE(tier, 'raw') != 'stable'
                ORDER BY created_at ASC LIMIT ?
            )
            """,
            (user_id, to_drop),
        )
        self._conn.commit()
        return int(cur.rowcount or 0)

    def resolve_contradictions(
        self,
        user_id: str,
        *,
        max_scan: int = 200,
        supersede_stable: bool = False,
        stable_min_hits: int = 2,
        source_aware: bool = False,
    ) -> Dict[str, int]:
        """R10/R11/R12 矛盾消解：同一**单值属性槽**出现冲突值时，保留最新、旧值标 ``stale``。

        例：旧"住在北京" vs 新"住在上海" → 旧条降为 ``stale``（排除出 prompt 注入，
        但保留备查/审计）。``raw`` 内消解（R10）：newest 以 ``last_seen``（回退
        ``created_at``）为准。

        R11 ``supersede_stable``：允许**新 raw 证据推翻已晋升的 ``stable`` 结论**
        （如"搬家/分手"——旧住址早已是稳定结论，新址该取代它）。但 stable 是高置信结论，
        不能被一次随口提及（"出差去上海"）冲掉，故设更高门槛：只有当 newest raw 槽值
        的**累计 hits ≥ ``stable_min_hits``**（反复提及=真的变了）时，才把同槽冲突的
        stable 条标 ``stale``。

        R12 ``source_aware``：推翻 stable 的证据**只数 ``user_stated``**（用户明说）的
        hits——AI 推断（``ai_inferred``）再多也不该推翻用户亲口确认的稳定结论。

        返回 ``{"superseded", "conflicts", "stable_superseded"}``。
        """
        from src.utils.memory_slots import extract_slot, slots_conflict

        try:
            rows = self._conn.execute(
                """
                SELECT id, content, created_at, last_seen, hits,
                       COALESCE(source, 'user_stated')
                FROM episodic_memory
                WHERE user_id = ? AND COALESCE(tier, 'raw') = 'raw'
                ORDER BY created_at DESC LIMIT ?
                """,
                (user_id, max(2, min(int(max_scan), 500))),
            ).fetchall()
        except Exception as e:  # noqa: BLE001
            logger.debug("episodic contradiction fetch failed: %s", e)
            return {"superseded": 0, "conflicts": 0, "stable_superseded": 0}

        # 按槽 base key 分组（pref 槽含对象，身份槽即 slot 名）
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for r in rows:
            slot = extract_slot(r[1] or "")
            if not slot:
                continue
            ts = float(r[3] if r[3] is not None else (r[2] or 0.0))
            groups.setdefault(slot[0], []).append(
                {
                    "id": int(r[0]), "slot": slot, "ts": ts,
                    "hits": int(r[4] or 1), "source": str(r[5] or "user_stated"),
                }
            )

        # R11：可选地把 stable 层也按槽分组，供新 raw 证据推翻
        stable_groups: Dict[str, List[Dict[str, Any]]] = {}
        if supersede_stable:
            try:
                srows = self._conn.execute(
                    """
                    SELECT id, content
                    FROM episodic_memory
                    WHERE user_id = ? AND COALESCE(tier, 'raw') = 'stable'
                    ORDER BY created_at DESC LIMIT ?
                    """,
                    (user_id, max(2, min(int(max_scan), 500))),
                ).fetchall()
                for sr in srows:
                    sslot = extract_slot(sr[1] or "")
                    if sslot:
                        stable_groups.setdefault(sslot[0], []).append(
                            {"id": int(sr[0]), "slot": sslot}
                        )
            except Exception as e:  # noqa: BLE001
                logger.debug("episodic stable fetch failed: %s", e)

        stale_ids: List[int] = []
        stable_stale_ids: List[int] = []
        conflicts = 0
        min_hits = max(1, int(stable_min_hits))
        for base, items in groups.items():
            if len(items) < 2 and not stable_groups.get(base):
                continue
            newest = max(items, key=lambda x: x["ts"])
            group_conflicted = False
            for it in items:
                if it["id"] == newest["id"]:
                    continue
                if slots_conflict(it["slot"], newest["slot"]):
                    stale_ids.append(it["id"])
                    group_conflicted = True
            if group_conflicted:
                conflicts += 1
            # R11/R12：新 raw 证据足够强 → 推翻同槽冲突的 stable 结论；
            # source_aware 时只数 user_stated 的 hits（AI 推断不足以推翻用户明说）。
            if supersede_stable and stable_groups.get(base):
                evidence = sum(
                    it["hits"]
                    for it in items
                    if it["slot"] == newest["slot"]
                    and (not source_aware or it["source"] == "user_stated")
                )
                if evidence >= min_hits:
                    for st in stable_groups[base]:
                        if slots_conflict(st["slot"], newest["slot"]):
                            stable_stale_ids.append(st["id"])
        all_stale = stale_ids + stable_stale_ids
        if all_stale:
            try:
                self._conn.executemany(
                    "UPDATE episodic_memory SET tier = 'stale' WHERE id = ?",
                    [(i,) for i in all_stale],
                )
                self._conn.commit()
            except Exception as e:  # noqa: BLE001
                logger.debug("episodic contradiction mark failed: %s", e)
                return {"superseded": 0, "conflicts": conflicts, "stable_superseded": 0}
        return {
            "superseded": len(stale_ids),
            "conflicts": conflicts,
            "stable_superseded": len(stable_stale_ids),
        }

    def merge_near_duplicates(
        self,
        user_id: str,
        *,
        threshold: float = 0.92,
        max_scan: int = 200,
        min_raw: int = 6,
    ) -> Dict[str, int]:
        """R5 近似去重巩固：把 ``raw`` 层里**语义近似**的事实归并为一条。

        承接 R3——R3 只折叠**完全相同**（hash 相等）的事实，但"喜欢猫"/"我养了只猫"/
        "家里有猫"是同一件事的不同说法，会各占一行、稀释复发信号、挤占 prune 名额。
        本方法用 embedding 余弦相似度做**贪心聚类**（阈值默认 0.92，高=只并近义），
        每簇择优留一条（salience→hits→新近→更长者胜），并把其余条的 ``hits`` 累加到
        survivor（让"换着说法反复提"也累积成复发证据，反哺 ``consolidate`` 晋升）。

        仅作用于 ``raw``（``stable`` 是已巩固结论，不动）；O(n²) 但 n≤max_scan 且向量
        预归一化为点积；raw 不足 ``min_raw`` 直接跳过（早期/小历史不值当）。

        返回 ``{"merged": 被删条数, "clusters": 发生归并的簇数}``。
        """
        from src.utils.episodic_vector import blob_to_vec

        if self._count_tier(user_id, "raw") < max(2, int(min_raw)):
            return {"merged": 0, "clusters": 0}
        thr = max(0.5, min(float(threshold or 0.92), 0.999))
        try:
            rows = self._conn.execute(
                """
                SELECT id, content, embedding, created_at, hits, salience, last_seen
                FROM episodic_memory
                WHERE user_id = ? AND COALESCE(tier, 'raw') = 'raw'
                  AND embedding IS NOT NULL
                ORDER BY created_at DESC LIMIT ?
                """,
                (user_id, max(2, min(int(max_scan), 500))),
            ).fetchall()
        except Exception as e:  # noqa: BLE001
            logger.debug("episodic dedupe fetch failed: %s", e)
            return {"merged": 0, "clusters": 0}

        # 预归一化向量 → 余弦退化为点积，省去 n² 次开方
        items: List[Dict[str, Any]] = []
        for r in rows:
            vec = blob_to_vec(r[2])
            if not vec:
                continue
            norm = sum(x * x for x in vec) ** 0.5
            if norm < 1e-9:
                continue
            inv = 1.0 / norm
            items.append({
                "id": int(r[0]),
                "content": (r[1] or "").strip(),
                "nvec": [x * inv for x in vec],
                "created_at": float(r[3] or 0.0),
                "hits": int(r[4] or 1),
                "salience": (float(r[5]) if r[5] is not None else 0.0),
                "last_seen": float(r[6] or r[3] or 0.0),
            })
        if len(items) < 2:
            return {"merged": 0, "clusters": 0}

        used: set = set()
        merged_total = 0
        clusters = 0
        for i in range(len(items)):
            if items[i]["id"] in used:
                continue
            a = items[i]["nvec"]
            cluster = [items[i]]
            for j in range(i + 1, len(items)):
                if items[j]["id"] in used:
                    continue
                b = items[j]["nvec"]
                dot = 0.0
                for x, y in zip(a, b):
                    dot += x * y
                if dot >= thr:
                    cluster.append(items[j])
                    used.add(items[j]["id"])
            if len(cluster) < 2:
                continue
            survivor = max(
                cluster,
                key=lambda c: (c["salience"], c["hits"], c["created_at"], len(c["content"])),
            )
            others = [c for c in cluster if c["id"] != survivor["id"]]
            total_hits = survivor["hits"] + sum(o["hits"] for o in others)
            max_sal = max(c["salience"] for c in cluster)
            max_last = max(c["last_seen"] for c in cluster)
            try:
                self._conn.execute(
                    "UPDATE episodic_memory SET hits = ?, salience = ?, last_seen = ?"
                    " WHERE id = ?",
                    (total_hits, max_sal, max_last, survivor["id"]),
                )
                self._conn.executemany(
                    "DELETE FROM episodic_memory WHERE id = ?",
                    [(o["id"],) for o in others],
                )
                merged_total += len(others)
                clusters += 1
            except Exception as e:  # noqa: BLE001
                logger.debug("episodic dedupe merge failed: %s", e)
        if merged_total:
            self._conn.commit()
        return {"merged": merged_total, "clusters": clusters}

    def consolidate(
        self,
        user_id: str,
        *,
        min_hits: int = 2,
        min_salience: Optional[float] = None,
        dedup_threshold: Optional[float] = None,
        resolve_contradictions: bool = False,
        supersede_stable: bool = False,
        stable_min_hits: int = 2,
        source_aware: bool = False,
        inferred_min_hits: Optional[int] = None,
    ) -> Dict[str, int]:
        """离线巩固：把 ``raw`` 层里**复发**（hits≥min_hits）或**情绪浓**
        （salience≥min_salience，若给）的事实晋升为 ``stable`` 稳定层。

        稳定层享受检索加权（见 ``get_bullets_for_prompt``）且永不被 prune 裁剪——
        即 PersonaTree/REMT 的"复发证据 → 稳定结论"思想的轻量落地。

        顺序：R10/R11 矛盾消解（旧冲突值标 stale，含新证据推翻 stable）→ R5 近义去重
        （合并近义、累加 hits）→ 晋升。先消矛盾再去重，避免把"住北京/住上海"误并。

        ``supersede_stable``（R11）：开后新 raw 证据（累计 hits≥``stable_min_hits``）
        可推翻同槽的旧 stable 结论，承接"搬家/分手"这类真实变更。

        ``source_aware``（R12）：开后按来源分级置信——``ai_inferred``（LLM 推断）晋升
        stable 需更高复发门槛（``inferred_min_hits``，默认 ``min_hits+1``），且推翻 stable
        的证据只数 ``user_stated``。``user_stated`` 走原门槛，行为不变。

        返回 ``{"promoted", "stable_total", "raw_total", "merged", "superseded",
        "stable_superseded"}``。
        """
        superseded = 0
        stable_superseded = 0
        if resolve_contradictions:
            _rc = self.resolve_contradictions(
                user_id,
                supersede_stable=supersede_stable,
                stable_min_hits=stable_min_hits,
                source_aware=source_aware,
            )
            superseded = _rc.get("superseded", 0)
            stable_superseded = _rc.get("stable_superseded", 0)
        merged = 0
        if dedup_threshold is not None:
            merged = self.merge_near_duplicates(
                user_id, threshold=float(dedup_threshold)
            ).get("merged", 0)
        mh = max(2, int(min_hits or 2))
        # 既有"明说"晋升条件：复发 hits 达标，或（若给）情绪显著性达标
        stated_cond = "hits >= ?"
        stated_params: List[Any] = [mh]
        if min_salience is not None:
            try:
                ms = float(min_salience)
                stated_cond = "(hits >= ? OR COALESCE(salience, 0) >= ?)"
                stated_params = [mh, ms]
            except (TypeError, ValueError):
                pass
        if source_aware:
            # R12：ai_inferred 需更高复发门槛（默认 min_hits+1），且不走情绪捷径——
            # "AI 推断 + 情绪浓"仍只是猜测，不该轻易固化为稳定人设。
            imh = max(mh, int(inferred_min_hits)) if inferred_min_hits is not None else mh + 1
            cond = (
                "((COALESCE(source, 'user_stated') = 'user_stated' AND " + stated_cond + ")"
                " OR (COALESCE(source, 'user_stated') = 'ai_inferred' AND hits >= ?))"
            )
            params: List[Any] = [user_id] + stated_params + [imh]
        else:
            cond = stated_cond
            params = [user_id] + stated_params
        try:
            cur = self._conn.execute(
                f"""
                UPDATE episodic_memory SET tier = 'stable'
                WHERE user_id = ? AND COALESCE(tier, 'raw') = 'raw' AND {cond}
                """,
                params,
            )
            self._conn.commit()
            promoted = int(cur.rowcount or 0)
        except Exception as e:  # noqa: BLE001
            logger.debug("episodic consolidate failed: %s", e)
            promoted = 0
        return {
            "promoted": promoted,
            "stable_total": self._count_tier(user_id, "stable"),
            "raw_total": self._count_tier(user_id, "raw"),
            "merged": merged,
            "superseded": superseded,
            "stable_superseded": stable_superseded,
        }

    def profile_summary(self, user_id: str, *, top_stable: int = 3) -> Dict[str, Any]:
        """R14：记忆画像聚合——按 tier/source 计数 + 取若干稳定事实摘要。

        供坐席侧栏一眼掌握"对这个用户我们确切知道什么（stable/user_stated）、哪些还只是
        AI 猜的（ai_inferred）"。排除 ``stale``（已弃）；store 异常返回空概览（绝不抛）。
        返回 ``{total, stable, raw, user_stated, ai_inferred, top_stable: [content...]}``。
        """
        empty = {
            "total": 0, "stable": 0, "raw": 0,
            "user_stated": 0, "ai_inferred": 0, "top_stable": [],
            "pending_inferred": [],
        }
        uid = str(user_id or "").strip()
        if not uid:
            return empty
        try:
            rows = self._conn.execute(
                "SELECT COALESCE(tier, 'raw'), COALESCE(source, 'user_stated'), COUNT(*)"
                " FROM episodic_memory"
                " WHERE user_id = ? AND COALESCE(tier, 'raw') != 'stale'"
                " GROUP BY 1, 2",
                (uid,),
            ).fetchall()
        except Exception as e:  # noqa: BLE001
            logger.debug("episodic profile_summary failed: %s", e)
            return empty
        out = dict(empty)
        out["top_stable"] = []
        for tier, source, cnt in rows:
            c = int(cnt or 0)
            out["total"] += c
            if tier == "stable":
                out["stable"] += c
            else:
                out["raw"] += c
            if source == "ai_inferred":
                out["ai_inferred"] += c
            else:
                out["user_stated"] += c
        if out["total"] == 0:
            return empty
        try:
            n = max(0, min(int(top_stable), 10))
            if n:
                tops = self._conn.execute(
                    "SELECT content FROM episodic_memory"
                    " WHERE user_id = ? AND COALESCE(tier, 'raw') = 'stable'"
                    " ORDER BY COALESCE(salience, 0) DESC, COALESCE(hits, 1) DESC,"
                    " created_at DESC LIMIT ?",
                    (uid, n),
                ).fetchall()
                out["top_stable"] = [str(r[0]) for r in tops if r and r[0]]
        except Exception:  # noqa: BLE001
            pass
        # R15：待确认的 AI 推断（raw + ai_inferred），供坐席一键转明说
        if out["ai_inferred"]:
            try:
                pend = self._conn.execute(
                    "SELECT id, content FROM episodic_memory"
                    " WHERE user_id = ? AND COALESCE(tier, 'raw') = 'raw'"
                    " AND COALESCE(source, 'user_stated') = 'ai_inferred'"
                    " ORDER BY COALESCE(salience, 0) DESC, COALESCE(hits, 1) DESC,"
                    " created_at DESC LIMIT 6",
                    (uid,),
                ).fetchall()
                out["pending_inferred"] = [
                    {"id": int(r[0]), "content": str(r[1])}
                    for r in pend if r and r[1]
                ]
            except Exception:  # noqa: BLE001
                pass
        return out

    def confirm_inferred_fact(self, row_id: int) -> Optional[str]:
        """R15：坐席确认一条 AI 推断为属实——升格为 user_stated 且直接置 stable。

        人工背书是比"复发"更强的置信信号，故直接转稳定（而非等 consolidate）。
        仅作用于当前还是 ``ai_inferred`` 的行，避免误改用户明说事实的 tier。
        R16：返回被确认的 ``content``（供调用方写审计留痕），未命中返回 ``None``。
        """
        try:
            rid = int(row_id)
        except (TypeError, ValueError):
            return None
        try:
            row = self._conn.execute(
                "SELECT content FROM episodic_memory"
                " WHERE id = ? AND COALESCE(source, 'user_stated') = 'ai_inferred'",
                (rid,),
            ).fetchone()
            if not row:
                return None
            self._conn.execute(
                "UPDATE episodic_memory"
                " SET source = 'user_stated', tier = 'stable', last_seen = ?"
                " WHERE id = ?",
                (int(time.time()), rid),
            )
            self._conn.commit()
            return str(row[0])
        except Exception as e:  # noqa: BLE001
            logger.debug("episodic confirm_inferred_fact failed: %s", e)
            return None

    def inferred_counts(self) -> Dict[str, int]:
        """R17：全库 AI 推断计数——``pending``（raw 待确认）与 ``total``（任意 tier）。

        确认后事实会翻成 ``user_stated``，故已从 ai_inferred 集合移出；``pending`` 是
        当前仍待坐席核实的 raw 推断（不含 stale）。store 异常返回零。
        """
        out = {"pending": 0, "total": 0}
        try:
            row = self._conn.execute(
                "SELECT"
                " SUM(CASE WHEN COALESCE(tier, 'raw') = 'raw' THEN 1 ELSE 0 END),"
                " COUNT(*)"
                " FROM episodic_memory"
                " WHERE COALESCE(source, 'user_stated') = 'ai_inferred'",
            ).fetchone()
            if row:
                out["pending"] = int(row[0] or 0)
                out["total"] = int(row[1] or 0)
        except Exception as e:  # noqa: BLE001
            logger.debug("episodic inferred_counts failed: %s", e)
        return out

    def _count_tier(self, user_id: str, tier: str) -> int:
        try:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM episodic_memory"
                " WHERE user_id = ? AND COALESCE(tier, 'raw') = ?",
                (user_id, tier),
            ).fetchone()
            return int(row[0]) if row else 0
        except Exception:
            return 0

    @staticmethod
    def _keyword_overlap_score(query: str, content: str) -> float:
        """Lightweight overlap: 2–4 char substrings from query (no extra deps)."""
        q = (query or "").strip()
        c = (content or "").strip()
        if len(q) < 2 or not c:
            return 0.0
        q = q[:200]
        score = 0.0
        step = 1 if len(q) < 24 else 2
        for L in (4, 3, 2):
            for i in range(0, max(1, len(q) - L + 1), step):
                frag = q[i : i + L]
                if len(frag) < L:
                    break
                if frag in c:
                    score += float(L)
        return score

    def get_bullets_for_prompt(
        self,
        user_id: str,
        max_items: int = 8,
        max_chars: int = 1200,
        query_text: Optional[str] = None,
        rerank_keywords: bool = False,
        query_embedding: Optional[List[float]] = None,
        use_vector_fusion: bool = False,
        vector_weight: float = 0.5,
        keyword_weight: float = 0.5,
        use_salience_rerank: bool = False,
        salience_weight: float = 0.15,
        recency_weight: float = 0.10,
        recency_half_life_days: float = 30.0,
    ) -> str:
        """Newline bullets; optional vector+keyword fusion when query_embedding set.

        R2（REMT-lite）：``use_salience_rerank`` 开启后，在既有相关度之上叠加
        情绪显著性 + 时间衰减重排（默认关 → 行为与旧版完全一致）。
        """
        from src.utils.episodic_vector import blob_to_vec, cosine_similarity

        max_items = max(1, min(int(max_items or 8), 40))
        max_chars = max(100, min(int(max_chars or 1200), 8000))
        qt = (query_text or "").strip()
        want_kw = rerank_keywords and len(qt) >= 2
        want_vec = bool(
            use_vector_fusion and query_embedding and len(query_embedding) >= 8
        )
        fetch_n = max_items * 6 if (want_kw or want_vec) else max_items * 2
        fetch_n = min(fetch_n, 120)

        rows = self._conn.execute(
            """
            SELECT content, embedding, created_at, salience, tier
            FROM episodic_memory WHERE user_id = ?
              AND COALESCE(tier, 'raw') != 'stale'
            ORDER BY created_at DESC LIMIT ?
            """,
            (user_id, fetch_n),
        ).fetchall()
        if not rows:
            return ""

        pairs: List[Tuple[str, Optional[bytes], float, Optional[float], str]] = [
            (
                r[0].strip(),
                r[1],
                float(r[2] or 0.0),
                (float(r[3]) if r[3] is not None else None),
                (str(r[4]) if r[4] else "raw"),
            )
            for r in rows if r and r[0]
        ]
        if not pairs:
            return ""

        # R2（REMT-lite）+ R3（分层）：可选"显著性×时间衰减 + 稳定层加权"重排
        # （默认关，零行为变化）。R3：优先用写入期落库的 salience，省去每次重算。
        _rerank = None
        if use_salience_rerank:
            try:
                from src.utils.memory_salience import (
                    blend_rank,
                    recency_factor,
                    salience_score,
                )
                _now = time.time()

                def _rerank(  # noqa: E306
                    base: float,
                    text: str,
                    ts: float,
                    stored_sal: Optional[float] = None,
                    tier: str = "raw",
                ) -> float:
                    sal = stored_sal if stored_sal is not None else salience_score(text)
                    score = blend_rank(
                        base,
                        sal,
                        recency_factor(ts, _now, recency_half_life_days),
                        salience_weight=salience_weight,
                        recency_weight=recency_weight,
                    )
                    if tier == "stable":
                        score += _STABLE_TIER_BOOST
                    return score
            except Exception:
                _rerank = None

        contents: List[str]
        if want_vec:
            vw = max(0.0, min(1.0, float(vector_weight)))
            kw_w = max(0.0, min(1.0, float(keyword_weight)))
            s = vw + kw_w
            if s > 1e-9:
                vw, kw_w = vw / s, kw_w / s
            kws = [
                self._keyword_overlap_score(qt, t) if want_kw else 0.0
                for t, _, _, _, _ in pairs
            ]
            max_kw = max(kws) if kws else 0.0
            scored_rows: List[Tuple[float, str]] = []
            for (t, emb_blob, ts, sal, tier), kw in zip(pairs, kws):
                kw_n = (kw / max_kw) if max_kw > 1e-9 else 0.0
                ev = blob_to_vec(emb_blob)
                vs = cosine_similarity(query_embedding, ev) if ev else 0.0
                vs = max(0.0, min(1.0, (vs + 1.0) / 2.0))
                fusion = vw * vs + kw_w * kw_n
                final = _rerank(fusion, t, ts, sal, tier) if _rerank else fusion
                scored_rows.append((final, t))
            scored_rows.sort(key=lambda x: (-x[0], -len(x[1])))
            contents = [x[1] for x in scored_rows]
        elif want_kw:
            scored: List[Tuple[float, str]] = []
            kws2 = [self._keyword_overlap_score(qt, t) for t, _, _, _, _ in pairs]
            max_kw2 = max(kws2) if kws2 else 0.0
            for (t, _, ts, sal, tier), sc in zip(pairs, kws2):
                if _rerank:
                    base = (sc / max_kw2) if max_kw2 > 1e-9 else 0.0
                    final = _rerank(base, t, ts, sal, tier)
                else:
                    final = sc
                scored.append((final, t))
            scored.sort(key=lambda x: (-x[0], -len(x[1])))
            contents = [x[1] for x in scored]
        elif _rerank:
            # 无 query（纯近期）但开了重排：以新鲜度为 base 叠加显著性 + 稳定层加权
            from src.utils.memory_salience import recency_factor as _rf
            scored3: List[Tuple[float, str]] = []
            for t, _, ts, sal, tier in pairs:
                base = _rf(ts, None, recency_half_life_days)
                scored3.append((_rerank(base, t, ts, sal, tier), t))
            scored3.sort(key=lambda x: (-x[0], -len(x[1])))
            contents = [x[1] for x in scored3]
        else:
            contents = [p[0] for p in pairs]

        lines: List[str] = []
        total = 0
        for content in contents:
            line = f"- {content}"
            if total + len(line) + 1 > max_chars:
                break
            lines.append(line)
            total += len(line) + 1
            if len(lines) >= max_items:
                break
        return "\n".join(lines)

    def list_rows(
        self,
        prefix: str = "",
        limit: int = 100,
        source: str = "",
    ) -> List[Dict[str, Any]]:
        """Admin: recent rows, optional filter on memory key (user_id) and source。

        R13：``source`` 可选筛选（``user_stated`` / ``ai_inferred``），让运营一眼分辨
        哪些记忆是 AI 推断、便于人工纠错。
        """
        limit = max(1, min(int(limit or 100), 500))
        p = (prefix or "").strip()
        src = source if source in ("user_stated", "ai_inferred") else ""
        where = []
        params: List[Any] = []
        if p:
            where.append("user_id LIKE ?")
            params.append(f"%{p}%")
        if src:
            where.append("COALESCE(source, 'user_stated') = ?")
            params.append(src)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        params.append(limit)
        rows = self._conn.execute(
            f"""
            SELECT id, user_id, content, category, created_at,
              CASE WHEN embedding IS NOT NULL AND length(embedding) >= 8 THEN 1 ELSE 0 END,
              COALESCE(source, 'user_stated'), COALESCE(tier, 'raw'), COALESCE(hits, 1)
            FROM episodic_memory{clause}
            ORDER BY created_at DESC LIMIT ?
            """,
            params,
        ).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            out.append({
                "id": r[0],
                "memory_key": r[1],
                "content": r[2],
                "category": r[3],
                "created_at": r[4],
                "has_embedding": bool(r[5]),
                "source": r[6],
                "tier": r[7],
                "hits": int(r[8] or 1),
            })
        return out

    def delete_by_id(self, row_id: int) -> bool:
        cur = self._conn.execute(
            "DELETE FROM episodic_memory WHERE id = ?", (int(row_id),)
        )
        self._conn.commit()
        return int(cur.rowcount or 0) > 0

    def fetch_rows_missing_embedding(
        self, limit: int = 20, memory_key_prefix: str = ""
    ) -> List[Tuple[int, str, str]]:
        """Rows (id, memory_key, content) needing vector backfill. Optional filter on user_id."""
        limit = max(1, min(int(limit or 20), 200))
        p = (memory_key_prefix or "").strip()
        if p:
            rows = self._conn.execute(
                """
                SELECT id, user_id, content FROM episodic_memory
                WHERE (embedding IS NULL OR length(embedding) < 8)
                  AND user_id LIKE ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (f"%{p}%", limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT id, user_id, content FROM episodic_memory
                WHERE embedding IS NULL OR length(embedding) < 8
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [(int(r[0]), str(r[1]), str(r[2])) for r in rows]

    def clear_user(self, user_id: str) -> int:
        cur = self._conn.execute(
            "DELETE FROM episodic_memory WHERE user_id = ?", (user_id,)
        )
        self._conn.commit()
        return int(cur.rowcount or 0)
