"""
知识库存储与检索引擎
- SQLite 持久化（5 张表）
- 中文字符级 BM25 加权检索（无外部依赖）
- 向量化语义检索（numpy 加速 / 纯 Python 兜底，双路径）
- BM25 + 向量 RRF 融合排序
- SQLite 热备份 + 知识库版本管理
- 多语言翻译结构支持
"""

import json
import math
import re
import shutil
import sqlite3
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# 尝试导入 numpy（可选加速，不可用则纯 Python 兜底）
try:
    import numpy as _np
    _HAS_NUMPY = True
except ImportError:
    _np = None        # type: ignore
    _HAS_NUMPY = False


# ── 分类常量（默认值，可被域包覆盖） ────────────────────────
_DEFAULT_KB_CATEGORIES = [
    "订单查询",
    "余额汇率",
    "通道状态",
    "退款投诉",
    "常规咨询",
    "系统指令",
    "系统话术",
    "其他",
]

KB_CATEGORIES = list(_DEFAULT_KB_CATEGORIES)


def set_kb_categories(categories: list):
    """Override KB_CATEGORIES with domain-specific values."""
    global KB_CATEGORIES
    if categories:
        KB_CATEGORIES.clear()
        KB_CATEGORIES.extend(categories)

REPLY_MODES = ["direct", "ai_guided", "ai_strict"]

SUPPORTED_LANGUAGES = ["zh", "en", "ur", "pt", "ar"]

# ── 向量工具 ──────────────────────────────────────────────

def _l2_norm(vec: List[float]) -> float:
    return math.sqrt(sum(x * x for x in vec))


def _cosine_sim_pure(a: List[float], b: List[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = _l2_norm(a), _l2_norm(b)
    return dot / (na * nb) if (na and nb) else 0.0


class _VectorIndex:
    """
    内存向量索引，支持纯 Python 和 numpy 两种计算路径。
    存储归一化向量；搜索时只做点积（= 余弦相似度）。
    """

    def __init__(self):
        self._ids: List[str] = []
        self._vecs: List[List[float]] = []
        self._matrix = None   # Optional[np.ndarray]

    def load(self, rows: List[Dict]):
        """从 kb_entries 加载已有 embedding，跳过空值"""
        self._ids = []
        self._vecs = []
        for r in rows:
            raw = r.get("embedding")
            if not raw:
                continue
            try:
                vec = json.loads(raw) if isinstance(raw, str) else raw
                n = _l2_norm(vec)
                if n > 0:
                    self._ids.append(str(r["id"]))
                    self._vecs.append([x / n for x in vec])
            except Exception:
                pass
        self._build_matrix()

    def _build_matrix(self):
        if _HAS_NUMPY and self._vecs:
            self._matrix = _np.array(self._vecs, dtype=_np.float32)
        else:
            self._matrix = None

    def search(self, query_vec: List[float], top_k: int = 5) -> List[Tuple[str, float]]:
        if not self._ids:
            return []
        n = _l2_norm(query_vec)
        if n == 0:
            return []
        q_norm = [x / n for x in query_vec]

        if self._matrix is not None:
            q_arr = _np.array(q_norm, dtype=_np.float32)
            sims = self._matrix.dot(q_arr)
            k = min(top_k, len(sims))
            idx = sims.argsort()[-k:][::-1]
            return [(self._ids[i], float(sims[i])) for i in idx]
        else:
            scores = [
                (self._ids[i], sum(a * b for a, b in zip(self._vecs[i], q_norm)))
                for i in range(len(self._ids))
            ]
            scores.sort(key=lambda x: -x[1])
            return scores[:top_k]

    def update(self, entry_id: str, vec: List[float]):
        entry_id = str(entry_id)
        n = _l2_norm(vec)
        if n == 0:
            return
        norm_vec = [x / n for x in vec]
        if entry_id in self._ids:
            idx = self._ids.index(entry_id)
            self._vecs[idx] = norm_vec
        else:
            self._ids.append(entry_id)
            self._vecs.append(norm_vec)
        self._build_matrix()

    def remove(self, entry_id: str):
        entry_id = str(entry_id)
        if entry_id not in self._ids:
            return
        idx = self._ids.index(entry_id)
        self._ids.pop(idx)
        self._vecs.pop(idx)
        self._build_matrix()

    def has_embedding(self, entry_id: str) -> bool:
        return str(entry_id) in self._ids

    def count(self) -> int:
        return len(self._ids)

    def all_ids(self) -> List[str]:
        return list(self._ids)


def _rrf_merge(
    bm25_results: List[Tuple[str, float]],
    vector_results: List[Tuple[str, float]],
    k: int = 60,
) -> List[Tuple[str, float]]:
    """
    Reciprocal Rank Fusion — 融合两路检索结果。
    RRF(d) = 1/(k + rank_bm25(d)) + 1/(k + rank_vector(d))
    不依赖各路分数的绝对值，比加权求和更鲁棒。
    """
    bm25_rank = {doc: r + 1 for r, (doc, _) in enumerate(bm25_results)}
    vec_rank  = {doc: r + 1 for r, (doc, _) in enumerate(vector_results)}
    all_ids   = set(bm25_rank) | set(vec_rank)
    n_bm25    = len(bm25_results)
    n_vec     = len(vector_results)

    scores: Dict[str, float] = {}
    for doc_id in all_ids:
        r_b = bm25_rank.get(doc_id, n_bm25 + k)
        r_v = vec_rank.get(doc_id, n_vec + k)
        scores[doc_id] = 1.0 / (k + r_b) + 1.0 / (k + r_v)

    return sorted(scores.items(), key=lambda x: -x[1])


# ── 中文感知 BM25 ──────────────────────────────────────────

_CJK_STOPWORDS = frozenset(
    "的了吗呢啊哦呀吧嘛么呃嗯哈是在有不和与"
    "我你他她它们这那个一二三四五六七八九十"
)
_EN_STOPWORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been",
    "am", "do", "does", "did", "to", "of", "in", "on", "at", "for",
    "and", "or", "but", "it", "its", "my", "your", "this", "that",
})


def _tokenize(text: str) -> List[str]:
    """
    中英文混合分词（含停用词过滤）：
    - 中文字符 → 单字 + 双字 bigram；单字停用词被过滤（防止'吗''在'等高频虚词引发误匹配）
    - 英文/数字 → 小写单词；英文停用词被过滤
    """
    tokens: List[str] = []
    i = 0
    en_buf = ""
    while i < len(text):
        ch = text[i]
        if "\u4e00" <= ch <= "\u9fff":
            if en_buf:
                for w in re.split(r"\W+", en_buf.lower()):
                    if w and w not in _EN_STOPWORDS:
                        tokens.append(w)
                en_buf = ""
            if ch not in _CJK_STOPWORDS:
                tokens.append(ch)
            if i + 1 < len(text) and "\u4e00" <= text[i + 1] <= "\u9fff":
                bigram = text[i : i + 2]
                tokens.append(bigram)
        else:
            en_buf += ch
        i += 1
    if en_buf:
        for w in re.split(r"\W+", en_buf.lower()):
            if w and w not in _EN_STOPWORDS:
                tokens.append(w)
    return tokens


class _BM25Index:
    """
    加权字段 BM25（k1=1.5, b=0.75）
    field_weights: {"triggers": 3.0, "title": 2.0, "scenario": 1.5, "steps": 1.0}
    """

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self._docs: List[Dict[str, str]] = []    # raw field dicts
        self._doc_ids: List[str] = []
        self._field_tokens: List[Dict[str, List[str]]] = []
        self._idf: Dict[str, float] = {}
        self._avgdl: Dict[str, float] = {}

    _FIELD_WEIGHTS: Dict[str, float] = {
        "triggers": 3.0,
        "title": 2.0,
        "scenario": 1.5,
        "steps": 1.0,
        "principles": 0.8,
    }

    def build(self, docs: List[Dict[str, Any]]):
        """docs: list of {id, triggers, title, scenario, steps, principles}"""
        self._docs = docs
        self._doc_ids = [d["id"] for d in docs]
        fields = list(self._FIELD_WEIGHTS.keys())

        self._field_tokens = []
        for doc in docs:
            ft: Dict[str, List[str]] = {}
            for f in fields:
                ft[f] = _tokenize(doc.get(f, "") or "")
            self._field_tokens.append(ft)

        # IDF per field
        n = len(docs)
        self._idf = {}
        self._avgdl = {}
        for f in fields:
            df: Counter = Counter()
            total_len = 0
            for ft in self._field_tokens:
                toks = ft[f]
                total_len += len(toks)
                df.update(set(toks))
            self._avgdl[f] = total_len / n if n else 1
            for term, cnt in df.items():
                key = f"{f}:{term}"
                self._idf[key] = math.log((n - cnt + 0.5) / (cnt + 0.5) + 1)

    def search(self, query: str, top_k: int = 5) -> List[Tuple[str, float]]:
        if not self._docs:
            return []
        q_tokens = _tokenize(query)
        scores: List[float] = []
        for idx, ft in enumerate(self._field_tokens):
            score = 0.0
            for f, w in self._FIELD_WEIGHTS.items():
                toks = ft[f]
                dl = len(toks)
                freq_map: Counter = Counter(toks)
                avdl = self._avgdl.get(f, 1) or 1
                for term in q_tokens:
                    key = f"{f}:{term}"
                    idf = self._idf.get(key, 0)
                    freq = freq_map.get(term, 0)
                    num = idf * freq * (self.k1 + 1)
                    den = freq + self.k1 * (1 - self.b + self.b * dl / avdl)
                    score += w * num / den if den else 0
            scores.append(score)

        ranked = sorted(
            [(self._doc_ids[i], scores[i]) for i in range(len(scores)) if scores[i] > 0],
            key=lambda x: -x[1],
        )
        return ranked[:top_k]


# ── KnowledgeBaseStore ────────────────────────────────────

class _LRUCache:
    """简易 LRU 缓存，带 TTL 过期"""

    def __init__(self, maxsize: int = 128, ttl: float = 120):
        self._maxsize = maxsize
        self._ttl = ttl
        self._cache: Dict[str, Tuple[float, Any]] = {}
        self._order: List[str] = []

    def get(self, key: str) -> Optional[Any]:
        if key not in self._cache:
            return None
        ts, val = self._cache[key]
        if time.time() - ts > self._ttl:
            self._evict(key)
            return None
        self._order.remove(key)
        self._order.append(key)
        return val

    def put(self, key: str, val: Any):
        if key in self._cache:
            self._order.remove(key)
        elif len(self._cache) >= self._maxsize:
            self._evict(self._order[0])
        self._cache[key] = (time.time(), val)
        self._order.append(key)

    def _evict(self, key: str):
        self._cache.pop(key, None)
        try:
            self._order.remove(key)
        except ValueError:
            pass

    def clear(self):
        self._cache.clear()
        self._order.clear()


class KnowledgeBaseStore:
    """知识库主类，线程安全（每次操作独立 connection）"""

    _index: _BM25Index = _BM25Index()     # BM25 文本索引
    _vindex: _VectorIndex = _VectorIndex() # 向量语义索引
    _index_dirty: bool = True
    _tpl_cache: Dict[str, Dict] = {}       # template_key → {replies, vars, mode}
    _tpl_cache_ts: float = 0

    def __init__(self, db_path: Path):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._search_cache = _LRUCache(maxsize=256, ttl=180)
        self._init_db()
        self._rebuild_index()

    # ── DB 初始化 ──────────────────────────────────────────

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self):
        with self._conn() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS kb_entries (
                id          TEXT PRIMARY KEY,
                category    TEXT NOT NULL DEFAULT '其他',
                title       TEXT NOT NULL,
                triggers    TEXT DEFAULT '[]',
                scenario    TEXT DEFAULT '',
                steps       TEXT DEFAULT '',
                principles  TEXT DEFAULT '',
                example_reply_zh TEXT DEFAULT '',
                forbidden   TEXT DEFAULT '',
                embedding   TEXT DEFAULT NULL,
                enabled     INTEGER DEFAULT 1,
                use_count   INTEGER DEFAULT 0,
                rating      REAL DEFAULT 0.0,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS kb_translations (
                id          TEXT PRIMARY KEY,
                entry_id    TEXT NOT NULL,
                lang        TEXT NOT NULL,
                title       TEXT DEFAULT '',
                scenario    TEXT DEFAULT '',
                steps       TEXT DEFAULT '',
                principles  TEXT DEFAULT '',
                example_reply TEXT DEFAULT '',
                forbidden   TEXT DEFAULT '',
                auto_translated INTEGER DEFAULT 1,
                updated_at  TEXT NOT NULL,
                UNIQUE(entry_id, lang)
            );

            CREATE TABLE IF NOT EXISTS kb_error_codes (
                id              TEXT PRIMARY KEY,
                code            TEXT NOT NULL,
                source_text     TEXT DEFAULT '',
                explanation_zh  TEXT DEFAULT '',
                suggestion_zh   TEXT DEFAULT '',
                explanation_en  TEXT DEFAULT '',
                suggestion_en   TEXT DEFAULT '',
                enabled         INTEGER DEFAULT 1,
                created_at      TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS kb_examples (
                id              TEXT PRIMARY KEY,
                category        TEXT DEFAULT '其他',
                user_message    TEXT NOT NULL,
                bot_notification TEXT DEFAULT '',
                correct_reply   TEXT NOT NULL,
                language        TEXT DEFAULT 'zh',
                quality         INTEGER DEFAULT 0,
                source          TEXT DEFAULT 'manual',
                embedding       TEXT DEFAULT NULL,
                created_at      TEXT NOT NULL,
                updated_at      TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS kb_rules (
                id                TEXT PRIMARY KEY,
                priority          INTEGER DEFAULT 5,
                description       TEXT NOT NULL,
                trigger_condition TEXT DEFAULT '',
                constraint_text   TEXT NOT NULL,
                is_global         INTEGER DEFAULT 1,
                enabled           INTEGER DEFAULT 1,
                created_at        TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS kb_feedback (
                id              TEXT PRIMARY KEY,
                user_message    TEXT DEFAULT '',
                ai_reply        TEXT DEFAULT '',
                score           INTEGER DEFAULT 0,
                correction      TEXT DEFAULT '',
                operator        TEXT DEFAULT '',
                added_to_examples INTEGER DEFAULT 0,
                created_at      TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS kb_miss_log (
                query   TEXT PRIMARY KEY,
                cnt     INTEGER DEFAULT 1,
                last_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS kb_entry_versions (
                id         TEXT PRIMARY KEY,
                entry_id   TEXT NOT NULL,
                snapshot   TEXT NOT NULL,
                editor     TEXT DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_kbv_entry ON kb_entry_versions(entry_id);

            CREATE TABLE IF NOT EXISTS kb_query_log (
                id          TEXT PRIMARY KEY,
                query       TEXT NOT NULL,
                hit         INTEGER DEFAULT 0,
                score       REAL DEFAULT 0.0,
                matched_entry_id TEXT DEFAULT '',
                search_mode TEXT DEFAULT 'bm25',
                category    TEXT DEFAULT '',
                lang        TEXT DEFAULT 'zh',
                ts          REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_kql_ts ON kb_query_log(ts);

            CREATE TABLE IF NOT EXISTS kb_entry_images (
                id          TEXT PRIMARY KEY,
                entry_id    TEXT NOT NULL,
                filename    TEXT NOT NULL,
                caption     TEXT DEFAULT '',
                size_bytes  INTEGER DEFAULT 0,
                created_at  TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_kei_entry ON kb_entry_images(entry_id);

            CREATE TABLE IF NOT EXISTS kb_meta (
                k TEXT PRIMARY KEY,
                v TEXT NOT NULL DEFAULT ''
            );
            """)
            # 迁移旧 kb_query_log 表（新增 score / matched_entry_id 列）
            for col, ddl in [
                ("score", "REAL DEFAULT 0.0"),
                ("matched_entry_id", "TEXT DEFAULT ''"),
            ]:
                try:
                    c.execute(f"ALTER TABLE kb_query_log ADD COLUMN {col} {ddl}")
                except sqlite3.OperationalError:
                    pass

            # E0: 统一话术管理 — kb_entries 新增字段
            for col, ddl in [
                ("reply_mode",     "TEXT DEFAULT 'ai_guided'"),
                ("template_key",   "TEXT DEFAULT ''"),
                ("template_vars",  "TEXT DEFAULT '[]'"),
                ("fallback_group", "TEXT DEFAULT ''"),
                ("reply_direct_spec", "TEXT DEFAULT ''"),
                ("negative_triggers", "TEXT DEFAULT '[]'"),
            ]:
                try:
                    c.execute(f"ALTER TABLE kb_entries ADD COLUMN {col} {ddl}")
                except sqlite3.OperationalError:
                    pass
            # template_key 唯一索引（仅非空值）
            try:
                c.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_kb_template_key "
                    "ON kb_entries(template_key) WHERE template_key != ''"
                )
            except sqlite3.OperationalError:
                pass

    # ── BM25 索引管理 ──────────────────────────────────────

    def _rebuild_index(self):
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, title, triggers, scenario, steps, principles, embedding "
                "FROM kb_entries WHERE enabled=1"
            ).fetchall()
        docs = []
        vec_rows = []
        for r in rows:
            triggers_raw = r["triggers"] or "[]"
            try:
                trig_list = json.loads(triggers_raw)
                triggers_text = " ".join(trig_list) if isinstance(trig_list, list) else str(trig_list)
            except Exception:
                triggers_text = triggers_raw
            _eid = str(r["id"])
            docs.append({
                "id": _eid,
                "title": r["title"] or "",
                "triggers": triggers_text,
                "scenario": r["scenario"] or "",
                "steps": r["steps"] or "",
                "principles": r["principles"] or "",
            })
            if r["embedding"]:
                vec_rows.append({"id": _eid, "embedding": r["embedding"]})
        self._index.build(docs)
        self._vindex.load(vec_rows)   # 加载已有向量到内存索引
        KnowledgeBaseStore._index_dirty = False

    def _touch_index(self):
        KnowledgeBaseStore._index_dirty = True
        KnowledgeBaseStore._tpl_cache = {}
        KnowledgeBaseStore._tpl_cache_ts = 0
        self._search_cache.clear()
        self._rebuild_index()

    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """读取 kb_meta 键值（用于「是否已做过首次种子」等持久标记）。"""
        try:
            with self._conn() as c:
                row = c.execute("SELECT v FROM kb_meta WHERE k=?", (key,)).fetchone()
            return row[0] if row else default
        except sqlite3.OperationalError:
            return default

    def set_meta(self, key: str, value: str) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO kb_meta(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (key, value),
            )

    # ── 系统话术缓存 & 查询 ─────────────────────────────────

    def _ensure_tpl_cache(self):
        if KnowledgeBaseStore._tpl_cache:
            return
        with self._conn() as c:
            rows = c.execute(
                "SELECT template_key, example_reply_zh, reply_mode, template_vars, "
                "fallback_group FROM kb_entries "
                "WHERE enabled=1 AND template_key != '' AND template_key IS NOT NULL"
            ).fetchall()
        cache: Dict[str, Dict] = {}
        for r in rows:
            key = r["template_key"]
            raw = r["example_reply_zh"] or ""
            variants = [v.strip() for v in raw.split("\n---\n") if v.strip()]
            if not variants and raw.strip():
                variants = [raw.strip()]
            t_vars: list = []
            try:
                t_vars = json.loads(r["template_vars"] or "[]")
            except Exception:
                pass
            cache[key] = {
                "replies":  variants,
                "mode":     r["reply_mode"] or "ai_guided",
                "vars":     t_vars,
                "group":    r["fallback_group"] or "",
            }
        KnowledgeBaseStore._tpl_cache = cache
        KnowledgeBaseStore._tpl_cache_ts = time.time()

    def get_direct_reply(self, template_key: str, **kwargs) -> Optional[str]:
        """
        按 template_key 获取一条直接回复（reply_mode=direct 时使用）。
        多条变体用 \\n---\\n 分隔，随机选一条。支持 {var} 插值。
        """
        import random as _rnd
        self._ensure_tpl_cache()
        entry = KnowledgeBaseStore._tpl_cache.get(template_key)
        if not entry or not entry["replies"]:
            return None
        reply = _rnd.choice(entry["replies"])
        if kwargs:
            try:
                reply = reply.format(**kwargs)
            except (KeyError, IndexError):
                pass
        return reply

    def get_fallback(self, intent: str) -> str:
        """
        获取兜底话术：先按意图查 {intent}_fallback，未找到则查 global_fallback。
        最终兜底返回硬编码安全字符串（仅此一处保留硬编码）。
        """
        reply = self.get_direct_reply(f"{intent}_fallback")
        if reply:
            return reply
        reply = self.get_direct_reply("global_fallback")
        if reply:
            return reply
        return "在的，有什么可以帮您的？"

    def get_reply_mode(self, template_key: str) -> str:
        """获取指定 template_key 的 reply_mode"""
        self._ensure_tpl_cache()
        entry = KnowledgeBaseStore._tpl_cache.get(template_key)
        return entry["mode"] if entry else "ai_guided"

    # ── 错误码检测（优先于 BM25）──────────────────────────

    def detect_error_codes(self, text: str) -> List[Dict]:
        """从文本中找匹配的错误码条目（精确子串匹配）"""
        with self._conn() as c:
            codes = c.execute(
                "SELECT * FROM kb_error_codes WHERE enabled=1"
            ).fetchall()
        matches = []
        text_lower = text.lower()
        for row in codes:
            code = (row["code"] or "").lower()
            source = (row["source_text"] or "").lower()
            if code and code in text_lower:
                matches.append(dict(row))
            elif source and source in text_lower:
                matches.append(dict(row))
        return matches

    # ── 搜索（BM25 + 错误码双路）────────────────────────

    def search(self, query: str, top_k: int = 5, lang: str = "zh",
               query_vec: Optional[List[float]] = None) -> Dict:
        """
        混合检索：BM25 + 向量 RRF 融合（向量不可用时自动降级为纯 BM25）。
        query_vec: 可选，外部调用方预先计算好的查询向量（避免重复 API 调用）。
        返回：{
          "entries": [...],      # 匹配的知识条目
          "error_codes": [...],  # 匹配的错误码
          "examples": [...],     # 相关对话示例
          "rules": [...],        # 全局硬规则
          "search_mode": str,    # bm25 | hybrid
        }
        """
        if KnowledgeBaseStore._index_dirty:
            self._rebuild_index()

        _cache_key = f"{query[:200]}|{top_k}|{lang}|{'v' if query_vec else 'b'}"
        cached = self._search_cache.get(_cache_key)
        if cached is not None:
            return cached

        # ── BM25 检索 ──────────────────────────────────────
        bm25_results = self._index.search(query, top_k=max(top_k * 2, 10))

        # ── 向量检索（有向量索引且有查询向量时）───────────
        search_mode = "bm25"
        if query_vec and self._vindex.count() > 0:
            vec_results = self._vindex.search(query_vec, top_k=max(top_k * 2, 10))
            if vec_results:
                merged = _rrf_merge(bm25_results, vec_results)
                ranked = merged[:top_k]
                search_mode = "hybrid"
            else:
                ranked = bm25_results[:top_k]
        else:
            ranked = bm25_results[:top_k]

        # ── 加载条目详情 ───────────────────────────────────
        entry_ids = [doc_id for doc_id, _ in ranked]
        entries: List[Dict] = []
        if entry_ids:
            with self._conn() as c:
                placeholders = ",".join("?" * len(entry_ids))
                rows = c.execute(
                    f"SELECT * FROM kb_entries WHERE id IN ({placeholders}) AND enabled=1",
                    entry_ids,
                ).fetchall()
                row_map = {r["id"]: dict(r) for r in rows}
                for doc_id, score in ranked:
                    if doc_id in row_map:
                        entry = row_map[doc_id]
                        entry["_score"] = round(score, 4)
                        entry["_mode"] = search_mode
                        if lang != "zh":
                            trans = c.execute(
                                "SELECT * FROM kb_translations WHERE entry_id=? AND lang=?",
                                (doc_id, lang)
                            ).fetchone()
                            if trans:
                                for field in ("title", "scenario", "steps",
                                              "principles", "example_reply", "forbidden"):
                                    if trans[field]:
                                        entry[f"{field}_{lang}"] = trans[field]
                        entries.append(entry)

        query_lower = query.lower()
        filtered_entries: List[Dict] = []
        for entry in entries:
            neg_raw = entry.get("negative_triggers") or "[]"
            try:
                neg_list = json.loads(neg_raw) if isinstance(neg_raw, str) else neg_raw
            except Exception:
                neg_list = []
            if isinstance(neg_list, list) and neg_list:
                _hit_neg = [kw for kw in neg_list if kw and kw.lower() in query_lower]
                if _hit_neg:
                    entry["_neg_filtered"] = True
                    continue
            filtered_entries.append(entry)

        result = {
            "entries": filtered_entries,
            "error_codes": self.detect_error_codes(query),
            "examples": self._search_examples(query, lang=lang, top_k=3),
            "rules": self.get_rules(enabled_only=True, global_only=True),
            "search_mode": search_mode,
        }
        self._search_cache.put(_cache_key, result)
        return result

    def _search_examples(self, query: str, lang: str = "zh", top_k: int = 3) -> List[Dict]:
        with self._conn() as c:
            try:
                rows = c.execute(
                    "SELECT * FROM kb_examples ORDER BY quality DESC, created_at DESC LIMIT 200"
                ).fetchall()
            except Exception:
                rows = []
        q_toks = set(_tokenize(query))
        scored = []
        for row in rows:
            d = dict(row)
            msg_toks = set(_tokenize(d.get("user_message", "")))
            overlap = len(q_toks & msg_toks)
            if overlap > 0:
                scored.append((d, overlap))
        scored.sort(key=lambda x: -x[1])
        return [d for d, _ in scored[:top_k]]

    # ── 构建 AI 提示词上下文 ──────────────────────────────

    def _format_ai_context(self, result: Dict, lang: str = "zh") -> str:
        """将检索结果字典格式化为 AI system prompt 参考材料（内部复用）"""
        parts: List[str] = []

        if result.get("error_codes"):
            parts.append("【错误码解读】")
            for ec in result["error_codes"]:
                parts.append(
                    f"- 错误码 {ec['code']}：{ec['explanation_zh']}。建议操作：{ec['suggestion_zh']}"
                )

        if result.get("rules"):
            parts.append("【必须遵守的规则】")
            for rule in result["rules"][:5]:
                parts.append(f"- {rule['constraint_text']}")

        if result.get("entries"):
            mode = result.get("search_mode", "bm25")
            parts.append(f"【业务知识库条目（必须按步骤执行，不得跳过）】（检索模式: {mode}）")
            for entry in result["entries"]:
                r_mode = entry.get("reply_mode", "ai_guided")
                if r_mode == "direct":
                    continue
                example_key = f"example_reply_{lang}" if lang != "zh" else "example_reply_zh"
                example = entry.get(example_key) or entry.get("example_reply_zh", "")
                steps = entry.get('steps', '').strip()
                principles = entry.get('principles', '').strip()
                if r_mode == "ai_strict" and example:
                    parts.append(
                        f"▶ [{entry['category']}] {entry['title']}\n"
                        + (f"  【必须执行的步骤】:\n{steps}\n" if steps else "")
                        + (f"  【注意事项】: {principles}\n" if principles else "")
                        + f"  【必须使用以下模板回复（仅允许微调措辞，不得偏离结构和要点）】:\n  {example}"
                    )
                else:
                    parts.append(
                        f"▶ [{entry['category']}] {entry['title']}\n"
                        + (f"  【必须执行的步骤】:\n{steps}\n" if steps else "")
                        + (f"  【注意事项】: {principles}\n" if principles else "")
                        + (f"  【示例回复】: {example}" if example else "")
                    )

        if result.get("examples"):
            parts.append("【参考对话示例】")
            for ex in result["examples"]:
                parts.append(
                    f"  用户：{ex['user_message']}\n"
                    f"  正确回复：{ex['correct_reply']}"
                )

        return "\n".join(parts) if parts else ""

    def build_ai_context(self, query: str, lang: str = "zh") -> str:
        """
        把检索结果格式化成可直接插入 AI system prompt 的参考材料。
        （内部调用 search，适合不需要懒触发向量化的场景）
        """
        result = self.search(query, top_k=4, lang=lang)
        return self._format_ai_context(result, lang=lang)

    def build_ai_context_from_result(self, result: Dict, lang: str = "zh") -> str:
        """
        接收外部已有的 search() 结果，直接格式化为 AI context。
        供 skill_manager 懒触发混合检索时使用，避免重复调用 search()。
        """
        return self._format_ai_context(result, lang=lang)

    # ── 知识条目 CRUD ─────────────────────────────────────

    def add_entry(self, data: Dict) -> str:
        entry_id = data.get("id") or str(uuid.uuid4())[:8]
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        triggers = data.get("triggers", [])
        if isinstance(triggers, str):
            triggers = [t.strip() for t in triggers.split(",") if t.strip()]
        t_vars = data.get("template_vars", [])
        if isinstance(t_vars, list):
            t_vars = json.dumps(t_vars, ensure_ascii=False)
        _rds = data.get("reply_direct_spec")
        if _rds is None:
            rds_str = ""
        elif isinstance(_rds, str):
            rds_str = _rds
        else:
            rds_str = json.dumps(_rds, ensure_ascii=False)
        neg_trig = data.get("negative_triggers", [])
        if isinstance(neg_trig, str):
            neg_trig = [t.strip() for t in neg_trig.split(",") if t.strip()]
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO kb_entries "
                "(id,category,title,triggers,scenario,steps,principles,example_reply_zh,"
                "forbidden,enabled,use_count,rating,reply_mode,template_key,"
                "template_vars,fallback_group,reply_direct_spec,negative_triggers,"
                "created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    entry_id,
                    data.get("category", "其他"),
                    data.get("title", ""),
                    json.dumps(triggers, ensure_ascii=False),
                    data.get("scenario", ""),
                    data.get("steps", ""),
                    data.get("principles", ""),
                    data.get("example_reply_zh", ""),
                    data.get("forbidden", ""),
                    int(data.get("enabled", 1)),
                    int(data.get("use_count", 0)),
                    float(data.get("rating", 0.0)),
                    data.get("reply_mode", "ai_guided"),
                    data.get("template_key", ""),
                    t_vars if isinstance(t_vars, str) else json.dumps(t_vars, ensure_ascii=False),
                    data.get("fallback_group", ""),
                    rds_str,
                    json.dumps(neg_trig, ensure_ascii=False),
                    now, now,
                ),
            )
        self._touch_index()
        return entry_id

    # ── 版本快照 ──────────────────────────────────────────

    def save_version(self, entry_id: str, editor: str = "") -> Optional[str]:
        """
        在修改前保存当前条目快照到 kb_entry_versions。
        返回新版本 ID，条目不存在时返回 None。
        每个条目最多保留 10 个历史版本（自动清理最旧的）。
        """
        entry = self.get_entry(entry_id)
        if not entry:
            return None
        # 清理 embedding 大字段，节省存储
        entry.pop("embedding", None)
        vid = str(uuid.uuid4())[:8]
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as c:
            c.execute(
                "INSERT INTO kb_entry_versions (id, entry_id, snapshot, editor, created_at) "
                "VALUES (?,?,?,?,?)",
                (vid, entry_id, json.dumps(entry, ensure_ascii=False), editor, now)
            )
            # 保留最近 10 个版本，删除更旧的
            old = c.execute(
                "SELECT id FROM kb_entry_versions WHERE entry_id=? "
                "ORDER BY created_at DESC LIMIT -1 OFFSET 10",
                (entry_id,)
            ).fetchall()
            if old:
                c.execute(
                    f"DELETE FROM kb_entry_versions WHERE id IN "
                    f"({','.join('?' * len(old))})",
                    [r["id"] for r in old]
                )
        return vid

    def list_versions(self, entry_id: str) -> List[Dict]:
        """返回条目的版本历史列表（最新在前，不含 snapshot 字段以减少传输量）"""
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, entry_id, editor, created_at FROM kb_entry_versions "
                "WHERE entry_id=? ORDER BY created_at DESC",
                (entry_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def get_version(self, version_id: str) -> Optional[Dict]:
        """获取指定版本的完整快照"""
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM kb_entry_versions WHERE id=?", (version_id,)
            ).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["snapshot"] = json.loads(d["snapshot"])
        except Exception:
            pass
        return d

    def restore_version(self, version_id: str, editor: str = "") -> bool:
        """
        恢复到指定历史版本：
        1. 先保存当前为新版本（防止误操作）
        2. 将历史快照写回 kb_entries
        """
        ver = self.get_version(version_id)
        if not ver:
            return False
        snap = ver["snapshot"]
        if not isinstance(snap, dict):
            return False
        entry_id = ver["entry_id"]
        # 保存当前作为新快照
        self.save_version(entry_id, editor=f"before_restore_{editor}")
        # 恢复快照（只写允许的字段）
        return self.update_entry(entry_id, snap)

    def update_entry(self, entry_id: str, data: Dict) -> bool:
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        triggers = data.get("triggers")
        if triggers is not None:
            if isinstance(triggers, str):
                triggers = [t.strip() for t in triggers.split(",") if t.strip()]
            data["triggers"] = json.dumps(triggers, ensure_ascii=False)
        if "reply_direct_spec" in data and isinstance(data["reply_direct_spec"], dict):
            data["reply_direct_spec"] = json.dumps(
                data["reply_direct_spec"], ensure_ascii=False
            )
        neg_trig = data.get("negative_triggers")
        if neg_trig is not None:
            if isinstance(neg_trig, str):
                neg_trig = [t.strip() for t in neg_trig.split(",") if t.strip()]
            data["negative_triggers"] = json.dumps(neg_trig, ensure_ascii=False)
        allowed = ["category","title","triggers","scenario","steps","principles",
                   "example_reply_zh","forbidden","enabled",
                   "reply_mode","template_key","template_vars","fallback_group",
                   "reply_direct_spec","negative_triggers"]
        sets = ", ".join(f"{k}=?" for k in allowed if k in data)
        vals = [data[k] for k in allowed if k in data]
        if not sets:
            return False
        vals += [now, entry_id]
        with self._conn() as c:
            c.execute(f"UPDATE kb_entries SET {sets}, updated_at=? WHERE id=?", vals)
        self._touch_index()
        return True

    def delete_entry(self, entry_id: str) -> bool:
        with self._conn() as c:
            c.execute("DELETE FROM kb_entries WHERE id=?", (entry_id,))
            c.execute("DELETE FROM kb_translations WHERE entry_id=?", (entry_id,))
        self._touch_index()
        return True

    def purge_all_data(self) -> Dict[str, Any]:
        """
        清空知识库全部表数据与 kb_images 目录下文件，并重建 BM25/向量索引。
        kb_meta（含 kb_seeded_once）会保留：表示「曾部署过知识库」，重启后不会自动灌回默认种子。
        """
        img_dir = self.db_path.parent / "kb_images"
        removed_files = 0
        if img_dir.is_dir():
            for fp in img_dir.iterdir():
                if fp.is_file():
                    try:
                        fp.unlink()
                        removed_files += 1
                    except OSError:
                        pass
        tables = [
            "kb_entry_images",
            "kb_entry_versions",
            "kb_translations",
            "kb_entries",
            "kb_examples",
            "kb_rules",
            "kb_feedback",
            "kb_miss_log",
            "kb_query_log",
            "kb_error_codes",
        ]
        counts: Dict[str, int] = {}
        with self._conn() as c:
            for t in tables:
                try:
                    cur = c.execute(f"DELETE FROM {t}")
                    counts[t] = int(cur.rowcount or 0)
                except sqlite3.Error:
                    counts[t] = -1
            try:
                c.execute("VACUUM")
            except sqlite3.Error:
                pass
        self._touch_index()
        return {
            "db": str(self.db_path),
            "deleted_rows": counts,
            "kb_images_files_removed": removed_files,
        }

    def get_entry(self, entry_id: str) -> Optional[Dict]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM kb_entries WHERE id=?", (entry_id,)).fetchone()
            if not row:
                return None
            entry = dict(row)
            trans = c.execute(
                "SELECT * FROM kb_translations WHERE entry_id=?", (entry_id,)
            ).fetchall()
            entry["translations"] = {r["lang"]: dict(r) for r in trans}
        return entry

    def list_entries(self, category: str = "", enabled_only: bool = False,
                     search: str = "") -> List[Dict]:
        conds, params = [], []
        if category:
            conds.append("category=?"); params.append(category)
        if enabled_only:
            conds.append("enabled=1")
        where = ("WHERE " + " AND ".join(conds)) if conds else ""
        with self._conn() as c:
            rows = c.execute(
                f"SELECT * FROM kb_entries {where} ORDER BY category, title",
                params
            ).fetchall()
        entries = [dict(r) for r in rows]
        if search:
            q_toks = set(_tokenize(search))
            entries = [
                e for e in entries
                if q_toks & set(_tokenize(e.get("title","") + " " + e.get("scenario","")))
            ]
        return entries

    def find_trigger_overlaps(
        self,
        exclude_entry_id: Optional[str],
        triggers: Any,
    ) -> List[Dict[str, Any]]:
        """
        检测当前条目的触发词与其他知识条目的重复 / 包含关系（保存前校验用）。

        - exact: 规范化后完全相同（忽略英文大小写）
        - contains: 一方完整包含另一方（双方长度均 >= 2，且非 exact）
        """
        if isinstance(triggers, str):
            raw_list = [
                t.strip()
                for t in triggers.replace("，", ",").replace(";", ",").split(",")
                if t.strip()
            ]
        elif isinstance(triggers, list):
            raw_list = [str(t).strip() for t in triggers if str(t).strip()]
        else:
            raw_list = []
        if not raw_list:
            return []

        def _norm(s: str) -> str:
            return s.strip().lower()

        my_norms = [_norm(t) for t in raw_list if _norm(t)]

        with self._conn() as c:
            rows = c.execute(
                "SELECT id, title, category, enabled, triggers FROM kb_entries"
            ).fetchall()

        seen: set = set()
        out: List[Dict[str, Any]] = []

        for row in rows:
            oid = row["id"]
            if exclude_entry_id and oid == exclude_entry_id:
                continue
            try:
                o_triggers = json.loads(row["triggers"] or "[]")
            except Exception:
                o_triggers = []
            if not isinstance(o_triggers, list):
                continue
            o_list = [str(t).strip() for t in o_triggers if str(t).strip()]
            for my_raw in raw_list:
                mn = _norm(my_raw)
                if not mn:
                    continue
                for ot_raw in o_list:
                    on = _norm(ot_raw)
                    if not on:
                        continue
                    kind = ""
                    if mn == on:
                        kind = "exact"
                    elif len(mn) >= 2 and len(on) >= 2:
                        if mn in on or on in mn:
                            kind = "contains"
                    if not kind:
                        continue
                    dedup = (kind, mn, oid, on)
                    if dedup in seen:
                        continue
                    seen.add(dedup)
                    out.append(
                        {
                            "kind": kind,
                            "my_trigger": my_raw.strip(),
                            "other_id": oid,
                            "other_title": row["title"] or oid,
                            "other_category": row["category"] or "",
                            "other_trigger": ot_raw.strip(),
                            "other_enabled": int(row["enabled"] or 0),
                        }
                    )
        return out

    def inc_use_count(self, entry_id: str):
        with self._conn() as c:
            c.execute("UPDATE kb_entries SET use_count=use_count+1 WHERE id=?", (entry_id,))

    # ── 翻译 CRUD ──────────────────────────────────────────

    def upsert_translation(self, entry_id: str, lang: str, fields: Dict,
                           auto: bool = True) -> str:
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        trans_id = f"{entry_id}_{lang}"
        with self._conn() as c:
            existing = c.execute(
                "SELECT id FROM kb_translations WHERE entry_id=? AND lang=?",
                (entry_id, lang)
            ).fetchone()
            if existing:
                sets = ", ".join(f"{k}=?" for k in fields if k in
                    ("title","scenario","steps","principles","example_reply","forbidden"))
                vals = [fields[k] for k in fields if k in
                    ("title","scenario","steps","principles","example_reply","forbidden")]
                if sets:
                    c.execute(
                        f"UPDATE kb_translations SET {sets}, auto_translated=?, updated_at=? "
                        "WHERE entry_id=? AND lang=?",
                        vals + [int(auto), now, entry_id, lang]
                    )
            else:
                c.execute(
                    "INSERT INTO kb_translations (id,entry_id,lang,title,scenario,steps,"
                    "principles,example_reply,forbidden,auto_translated,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (trans_id, entry_id, lang,
                     fields.get("title",""), fields.get("scenario",""),
                     fields.get("steps",""), fields.get("principles",""),
                     fields.get("example_reply",""), fields.get("forbidden",""),
                     int(auto), now)
                )
        return trans_id

    # ── 错误码 CRUD ────────────────────────────────────────

    def add_error_code(self, data: Dict) -> str:
        ec_id = data.get("id") or str(uuid.uuid4())[:8]
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO kb_error_codes "
                "(id,code,source_text,explanation_zh,suggestion_zh,explanation_en,suggestion_en,enabled,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (ec_id, data.get("code",""), data.get("source_text",""),
                 data.get("explanation_zh",""), data.get("suggestion_zh",""),
                 data.get("explanation_en",""), data.get("suggestion_en",""),
                 int(data.get("enabled",1)), now)
            )
        return ec_id

    def list_error_codes(self) -> List[Dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM kb_error_codes ORDER BY code"
            ).fetchall()
        return [dict(r) for r in rows]

    def update_error_code(self, ec_id: str, data: Dict) -> bool:
        allowed = ["code","source_text","explanation_zh","suggestion_zh",
                   "explanation_en","suggestion_en","enabled"]
        sets = ", ".join(f"{k}=?" for k in allowed if k in data)
        vals = [data[k] for k in allowed if k in data] + [ec_id]
        if not sets:
            return False
        with self._conn() as c:
            c.execute(f"UPDATE kb_error_codes SET {sets} WHERE id=?", vals)
        return True

    def delete_error_code(self, ec_id: str) -> bool:
        with self._conn() as c:
            c.execute("DELETE FROM kb_error_codes WHERE id=?", (ec_id,))
        return True

    # ── 对话示例 CRUD ─────────────────────────────────────

    def add_example(self, data: Dict) -> str:
        ex_id = data.get("id") or str(uuid.uuid4())[:8]
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO kb_examples "
                "(id,category,user_message,bot_notification,correct_reply,language,quality,source,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (ex_id, data.get("category","其他"), data.get("user_message",""),
                 data.get("bot_notification",""), data.get("correct_reply",""),
                 data.get("language","zh"), int(data.get("quality",0)),
                 data.get("source","manual"), now, now)
            )
        return ex_id

    def list_examples(self, category: str = "", language: str = "") -> List[Dict]:
        conds, params = [], []
        if category:
            conds.append("category=?"); params.append(category)
        if language:
            conds.append("language=?"); params.append(language)
        where = ("WHERE " + " AND ".join(conds)) if conds else ""
        with self._conn() as c:
            rows = c.execute(
                f"SELECT * FROM kb_examples {where} ORDER BY quality DESC, created_at DESC",
                params
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_example(self, ex_id: str) -> bool:
        with self._conn() as c:
            c.execute("DELETE FROM kb_examples WHERE id=?", (ex_id,))
        return True

    # ── 硬规则 CRUD ───────────────────────────────────────

    def get_rules(self, enabled_only: bool = True, global_only: bool = False) -> List[Dict]:
        conds = []
        if enabled_only:
            conds.append("enabled=1")
        if global_only:
            conds.append("is_global=1")
        where = ("WHERE " + " AND ".join(conds)) if conds else ""
        with self._conn() as c:
            rows = c.execute(
                f"SELECT * FROM kb_rules {where} ORDER BY priority DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def add_rule(self, data: Dict) -> str:
        rule_id = data.get("id") or str(uuid.uuid4())[:8]
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO kb_rules "
                "(id,priority,description,trigger_condition,constraint_text,is_global,enabled,created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (rule_id, int(data.get("priority",5)), data.get("description",""),
                 data.get("trigger_condition",""), data.get("constraint_text",""),
                 int(data.get("is_global",1)), int(data.get("enabled",1)), now)
            )
        return rule_id

    def delete_rule(self, rule_id: str) -> bool:
        with self._conn() as c:
            c.execute("DELETE FROM kb_rules WHERE id=?", (rule_id,))
        return True

    # ── 反馈 CRUD ─────────────────────────────────────────

    def add_feedback(self, data: Dict) -> str:
        fb_id = str(uuid.uuid4())[:8]
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as c:
            c.execute(
                "INSERT INTO kb_feedback "
                "(id,user_message,ai_reply,score,correction,operator,added_to_examples,created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (fb_id, data.get("user_message",""), data.get("ai_reply",""),
                 int(data.get("score",0)), data.get("correction",""),
                 data.get("operator",""), 0, now)
            )
        return fb_id

    def list_feedback(self, limit: int = 50) -> List[Dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM kb_feedback ORDER BY created_at DESC LIMIT ?",
                (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def promote_feedback_to_example(self, fb_id: str) -> bool:
        with self._conn() as c:
            row = c.execute("SELECT * FROM kb_feedback WHERE id=?", (fb_id,)).fetchone()
            if not row or not row["correction"]:
                return False
            self.add_example({
                "user_message": row["user_message"],
                "correct_reply": row["correction"],
                "source": "feedback",
            })
            c.execute("UPDATE kb_feedback SET added_to_examples=1 WHERE id=?", (fb_id,))
        return True

    # ── 统计 ─────────────────────────────────────────────

    def stats(self) -> Dict:
        with self._conn() as c:
            total = c.execute("SELECT COUNT(*) FROM kb_entries").fetchone()[0]
            enabled = c.execute("SELECT COUNT(*) FROM kb_entries WHERE enabled=1").fetchone()[0]
            examples = c.execute("SELECT COUNT(*) FROM kb_examples").fetchone()[0]
            error_codes = c.execute("SELECT COUNT(*) FROM kb_error_codes").fetchone()[0]
            rules = c.execute("SELECT COUNT(*) FROM kb_rules WHERE enabled=1").fetchone()[0]
            feedback = c.execute("SELECT COUNT(*) FROM kb_feedback").fetchone()[0]
            good = c.execute("SELECT COUNT(*) FROM kb_feedback WHERE score=1").fetchone()[0]
            cats = c.execute(
                "SELECT category, COUNT(*) as cnt FROM kb_entries GROUP BY category"
            ).fetchall()
        return {
            "total_entries": total,
            "enabled_entries": enabled,
            "examples": examples,
            "error_codes": error_codes,
            "rules": rules,
            "feedback": feedback,
            "good_feedback": good,
            "satisfaction_rate": round(good / feedback * 100, 1) if feedback else 0,
            "by_category": {r["category"]: r["cnt"] for r in cats},
        }

    # ── 向量化接口（智能体 Embedding 接入点）────────────────
    # Phase 2：传入 embedding_fn(texts) -> List[List[float]]
    # 调用后自动填充所有条目的 embedding 字段并升级检索为余弦相似度

    # ── Miss 日志（未命中查询追踪）────────────────────────

    def log_miss(self, query: str):
        """记录未命中 KB 的查询，自动聚合计数（用于发现知识盲区）"""
        query = query.strip()[:200]
        if not query:
            return
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as c:
            existing = c.execute(
                "SELECT cnt FROM kb_miss_log WHERE query=?", (query,)
            ).fetchone()
            if existing:
                c.execute(
                    "UPDATE kb_miss_log SET cnt=cnt+1, last_at=? WHERE query=?",
                    (now, query)
                )
            else:
                c.execute(
                    "INSERT INTO kb_miss_log (query, cnt, last_at) VALUES (?,1,?)",
                    (query, now)
                )

    def get_miss_stats(self, top_k: int = 10) -> List[Dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT query, cnt, last_at FROM kb_miss_log ORDER BY cnt DESC LIMIT ?",
                (top_k,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ── 翻译审核 ──────────────────────────────────────────

    def get_pending_translations(self, limit: int = 100) -> List[Dict]:
        """获取所有待人工审核的自动翻译条目"""
        with self._conn() as c:
            rows = c.execute(
                "SELECT t.*, e.title AS entry_title, e.category, "
                "e.example_reply_zh, e.scenario, e.steps "
                "FROM kb_translations t "
                "JOIN kb_entries e ON t.entry_id = e.id "
                "WHERE t.auto_translated = 1 "
                "ORDER BY t.updated_at DESC LIMIT ?",
                (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def confirm_translation(self, trans_id: str) -> bool:
        """确认翻译（去掉自动标记，表示已人工审核）"""
        with self._conn() as c:
            c.execute(
                "UPDATE kb_translations SET auto_translated=0 WHERE id=?",
                (trans_id,)
            )
        return True

    # ── 过期条目管理 ───────────────────────────────────────

    def get_stale_entries(self, days: int = 7) -> List[Dict]:
        """获取启用但从未被使用超过 N 天的条目"""
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM kb_entries WHERE use_count=0 AND enabled=1 "
                "AND CAST(julianday('now') - julianday(created_at) AS INTEGER) > ? "
                "ORDER BY created_at ASC",
                (days,)
            ).fetchall()
        return [dict(r) for r in rows]

    def bulk_disable(self, entry_ids: List[str]) -> int:
        with self._conn() as c:
            for eid in entry_ids:
                c.execute("UPDATE kb_entries SET enabled=0 WHERE id=?", (eid,))
        self._touch_index()
        return len(entry_ids)

    # ── H2: 知识库自愈引擎 ───────────────────────────────────

    def run_self_heal(self, stale_days: int = 14) -> Dict:
        """
        H2: 执行一轮自愈巡检。返回执行结果摘要。

        1. 弱命中 → 自动扩展 triggers
        2. 零使用条目（>stale_days天）→ 自动禁用
        3. 过载条目 → 生成拆分建议（不自动执行）
        """
        result = {
            "triggers_expanded": 0,
            "entries_archived": 0,
            "overloaded_flagged": 0,
            "details": [],
        }

        # 1. 弱命中 → 自动扩展 triggers
        weak = self.get_weak_hits(score_threshold=0.45, hours=168, top_k=20)
        for w in weak:
            if w["count"] < 2:
                continue
            for eid in w.get("matched_entries", []):
                entry = self.get_entry(eid)
                if not entry or not entry.get("enabled"):
                    continue
                existing = json.loads(entry.get("triggers", "[]"))
                if not isinstance(existing, list):
                    existing = []
                query_kw = w["query"].strip().lower()
                existing_lower = [t.lower() for t in existing]
                if query_kw not in existing_lower and len(existing) < 15:
                    existing.append(w["query"].strip()[:50])
                    with self._conn() as c:
                        c.execute(
                            "UPDATE kb_entries SET triggers=?, updated_at=? WHERE id=?",
                            (json.dumps(existing, ensure_ascii=False),
                             time.strftime("%Y-%m-%dT%H:%M:%S"), eid)
                        )
                    result["triggers_expanded"] += 1
                    result["details"].append(
                        f"扩展 [{entry.get('title','')}] 触发词: +{query_kw}")
        self._touch_index()

        # 2. 零使用条目 → 自动禁用
        stale = self.get_stale_entries(days=stale_days)
        if stale:
            ids = [e["id"] for e in stale[:10]]
            self.bulk_disable(ids)
            result["entries_archived"] = len(ids)
            for e in stale[:10]:
                result["details"].append(
                    f"归档 [{e.get('title','')}] ({stale_days}天零使用)")

        # 3. 过载条目 → 标记（不自动执行拆分）
        overloaded = self.get_overloaded_entries(hours=168, min_diversity=5)
        result["overloaded_flagged"] = len(overloaded)
        for o in overloaded[:5]:
            result["details"].append(
                f"过载警告 [{o.get('title','')}] 承载 {o.get('diversity',0)} 种查询")

        return result

    def delete_miss_entry(self, query: str):
        with self._conn() as c:
            c.execute("DELETE FROM kb_miss_log WHERE query=?", (query,))

    def get_pending_translate_requests(self, limit: int = 10) -> list:
        """提取 miss_log 中的 [TRANSLATE:lang:entry_id] 翻译请求"""
        with self._conn() as c:
            rows = c.execute(
                "SELECT query, cnt, last_at FROM kb_miss_log "
                "WHERE query LIKE '[TRANSLATE:%' ORDER BY cnt DESC LIMIT ?",
                (limit,)
            ).fetchall()
        results = []
        import re
        pat = re.compile(r"^\[TRANSLATE:(\w+):([a-f0-9-]+)\]")
        for r in rows:
            m = pat.match(r["query"])
            if m:
                results.append({
                    "lang": m.group(1),
                    "entry_id": m.group(2),
                    "query": r["query"],
                    "cnt": r["cnt"],
                })
        return results

    # ── 查询日志（命中率分析）────────────────────────────

    def log_query(self, query: str, hit: bool,
                  search_mode: str = "bm25",
                  category: str = "", lang: str = "zh",
                  score: float = 0.0,
                  matched_entry_id: str = ""):
        """
        记录每次 KB 检索事件（含匹配分数和命中条目），用于命中率 + 质量趋势分析。
        写入同时清理 7 天前的旧记录（滚动保留）。
        """
        qid = str(uuid.uuid4())[:8]
        now_ts = time.time()
        with self._conn() as c:
            c.execute(
                "INSERT INTO kb_query_log "
                "(id,query,hit,score,matched_entry_id,search_mode,category,lang,ts) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (qid, query[:200], int(hit), round(score, 4),
                 matched_entry_id[:20], search_mode, category[:50], lang, now_ts),
            )
            c.execute("DELETE FROM kb_query_log WHERE ts < ?", (now_ts - 7 * 86400,))

    def get_query_analytics(self, hours: int = 24) -> Dict:
        """
        返回过去 N 小时的每小时命中率 + 分数质量分布统计。
        """
        since = time.time() - hours * 3600
        with self._conn() as c:
            rows = c.execute(
                "SELECT ts, hit, score, search_mode, category "
                "FROM kb_query_log WHERE ts >= ?",
                (since,),
            ).fetchall()

        buckets: Dict[str, Dict] = {}
        cat_count: Dict[str, int] = {}
        scores: List[float] = []

        for r in rows:
            hk = time.strftime("%H", time.localtime(r["ts"]))
            if hk not in buckets:
                buckets[hk] = {"hour": hk, "hits": 0, "misses": 0,
                               "weak_hits": 0, "hybrid": 0, "bm25": 0}
            b = buckets[hk]
            sc = r["score"] or 0.0
            scores.append(sc)
            if r["hit"]:
                b["hits"] += 1
                if sc < 0.50:
                    b["weak_hits"] += 1
            else:
                b["misses"] += 1
            if r["search_mode"] == "hybrid":
                b["hybrid"] += 1
            else:
                b["bm25"] += 1
            cat = r["category"] or ""
            if cat:
                cat_count[cat] = cat_count.get(cat, 0) + 1

        total  = len(rows)
        hits   = sum(1 for r in rows if r["hit"])
        weak   = sum(1 for r in rows if r["hit"] and (r["score"] or 0) < 0.50)
        hybrid = sum(1 for r in rows if r["search_mode"] == "hybrid")
        top_cats = sorted(cat_count.items(), key=lambda x: -x[1])[:5]
        avg_score = round(sum(scores) / len(scores), 3) if scores else 0

        return {
            "hours": sorted(buckets.values(), key=lambda x: x["hour"]),
            "totals": {
                "total":        total,
                "hits":         hits,
                "hit_pct":      round(hits / total * 100) if total else 0,
                "weak_hits":    weak,
                "weak_pct":     round(weak / total * 100) if total else 0,
                "avg_score":    avg_score,
                "hybrid":       hybrid,
                "hybrid_pct":   round(hybrid / total * 100) if total else 0,
                "top_categories": [{"cat": c, "count": n} for c, n in top_cats],
            },
        }

    def get_today_hit_rate(self) -> Dict:
        """快捷方法：今日命中率摘要（供 dashboard 使用）"""
        since = time.time() - 86400
        with self._conn() as c:
            total = c.execute(
                "SELECT COUNT(*) FROM kb_query_log WHERE ts >= ?", (since,)
            ).fetchone()[0]
            hits = c.execute(
                "SELECT COUNT(*) FROM kb_query_log WHERE ts >= ? AND hit=1", (since,)
            ).fetchone()[0]
        return {
            "total":   total,
            "hits":    hits,
            "hit_pct": round(hits / total * 100) if total else 0,
        }

    # ── 导出 / 导入 ───────────────────────────────────────

    def export_all(self) -> Dict:
        """
        导出所有启用条目、错误码、硬规则为可序列化字典。
        不含 embedding 字段（体积大且可重新生成）。
        """
        with self._conn() as c:
            entries = [dict(r) for r in c.execute(
                "SELECT * FROM kb_entries WHERE enabled=1 ORDER BY category, created_at"
            ).fetchall()]
            error_codes = [dict(r) for r in c.execute(
                "SELECT * FROM kb_error_codes WHERE enabled=1"
            ).fetchall()]
            rules = [dict(r) for r in c.execute(
                "SELECT * FROM kb_rules WHERE enabled=1"
            ).fetchall()]

        # 解析 JSON 字段，移除大字段
        for e in entries:
            e.pop("embedding", None)
            if e.get("triggers"):
                try:
                    e["triggers"] = json.loads(e["triggers"])
                except Exception:
                    pass

        return {
            "version":     "1.0",
            "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "entries":     entries,
            "error_codes": error_codes,
            "rules":       rules,
        }

    # ── CSV 导出 / 导入 ─────────────────────────────────────────

    _CSV_FIELDS = [
        "category", "title", "triggers", "scenario",
        "steps", "principles", "example_reply_zh", "forbidden",
        "reply_mode", "template_key", "fallback_group", "reply_direct_spec",
    ]

    # ── 图片附件 CRUD ──────────────────────────────────────────

    def add_entry_image(self, entry_id: str, filename: str,
                        caption: str = "", size_bytes: int = 0) -> str:
        """关联一张图片到知识条目，返回图片记录 id"""
        from datetime import datetime, timezone
        img_id  = str(uuid.uuid4())
        now     = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._conn() as c:
            c.execute(
                "INSERT INTO kb_entry_images(id,entry_id,filename,caption,size_bytes,created_at)"
                " VALUES(?,?,?,?,?,?)",
                (img_id, entry_id, filename, caption, size_bytes, now),
            )
        return img_id

    def get_entry_images(self, entry_id: str) -> List[dict]:
        """返回某条目的所有图片列表"""
        with self._conn() as c:
            rows = c.execute(
                "SELECT id,filename,caption,size_bytes,created_at"
                " FROM kb_entry_images WHERE entry_id=? ORDER BY created_at",
                (entry_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_entry_image(self, img_id: str) -> Optional[str]:
        """删除图片记录，返回被删除的 filename（用于删除物理文件）"""
        with self._conn() as c:
            row = c.execute(
                "SELECT filename FROM kb_entry_images WHERE id=?", (img_id,)
            ).fetchone()
            if not row:
                return None
            c.execute("DELETE FROM kb_entry_images WHERE id=?", (img_id,))
        return row[0]

    def delete_all_entry_images(self, entry_id: str) -> List[str]:
        """删除某条目的所有图片记录，返回 filename 列表"""
        with self._conn() as c:
            rows = c.execute(
                "SELECT filename FROM kb_entry_images WHERE entry_id=?", (entry_id,)
            ).fetchall()
            c.execute("DELETE FROM kb_entry_images WHERE entry_id=?", (entry_id,))
        return [r[0] for r in rows]

    def export_csv(self) -> str:
        """
        导出启用条目为 CSV 字符串（UTF-8 with BOM，Excel 可直接打开）。
        triggers 字段转为分号分隔字符串，方便人工编辑。
        """
        import csv
        import io
        output = io.StringIO()
        writer = csv.DictWriter(
            output, fieldnames=self._CSV_FIELDS,
            extrasaction="ignore", lineterminator="\n",
        )
        writer.writeheader()
        with self._conn() as c:
            rows = c.execute(
                "SELECT category,title,triggers,scenario,steps,principles,"
                "example_reply_zh,forbidden FROM kb_entries WHERE enabled=1 "
                "ORDER BY category,title"
            ).fetchall()
        for r in rows:
            row = dict(r)
            if row.get("triggers"):
                try:
                    tl = json.loads(row["triggers"])
                    row["triggers"] = "; ".join(tl) if isinstance(tl, list) else row["triggers"]
                except Exception:
                    pass
            writer.writerow(row)
        return "\ufeff" + output.getvalue()   # BOM → Excel 自动识别 UTF-8

    def import_from_csv(self, csv_text: str, mode: str = "skip") -> Dict:
        """
        从 CSV 字符串批量导入知识条目。
        - 第一行必须是字段名（至少含 title）
        - triggers 字段支持分号分隔的字符串
        - mode 同 import_from_data
        """
        import csv
        import io
        text = csv_text.lstrip("\ufeff")   # 剥离 BOM
        reader = csv.DictReader(io.StringIO(text))
        entries = []
        for row in reader:
            if not row.get("title"):
                continue
            entry: Dict = {"enabled": 1}
            for field in self._CSV_FIELDS:
                if field in row:
                    entry[field] = row[field]
            # 把分号字符串转回 JSON 数组
            if entry.get("triggers"):
                parts = [t.strip() for t in entry["triggers"].split(";") if t.strip()]
                entry["triggers"] = json.dumps(parts, ensure_ascii=False)
            entries.append(entry)
        return self.import_from_data({"entries": entries, "version": "csv"}, mode=mode)

    def import_from_data(self, data: Dict, mode: str = "skip") -> Dict:
        """
        批量导入知识库数据。
        mode="skip"  → 遇到同名条目跳过（安全模式）
        mode="update"→ 遇到同名条目覆盖更新
        按 title 去重（跨环境迁移时 ID 不可信，title 才是语义唯一键）。
        返回: {"added": N, "updated": N, "skipped": N, "failed": N}
        """
        result = {"added": 0, "updated": 0, "skipped": 0, "failed": 0}
        with self._conn() as c:
            existing = {r[0]: r[1] for r in c.execute(
                "SELECT title, id FROM kb_entries"
            ).fetchall()}   # title → id

        for entry in data.get("entries", []):
            try:
                title = (entry.get("title") or "").strip()
                if not title:
                    result["failed"] += 1
                    continue
                if title in existing:
                    if mode == "update":
                        self.save_version(existing[title], editor="import")
                        self.update_entry(existing[title], entry)
                        result["updated"] += 1
                    else:
                        result["skipped"] += 1
                else:
                    new_id = self.add_entry(entry)
                    existing[title] = new_id   # 防止同批次重复
                    result["added"] += 1
            except Exception:
                result["failed"] += 1

        for ec in data.get("error_codes", []):
            try:
                code = (ec.get("code") or "").strip()
                if not code:
                    continue
                with self._conn() as c:
                    if not c.execute("SELECT 1 FROM kb_error_codes WHERE code=?",
                                     (code,)).fetchone():
                        self.add_error_code(ec)
                        result["added"] += 1
                    else:
                        result["skipped"] += 1
            except Exception:
                result["failed"] += 1

        for rule in data.get("rules", []):
            try:
                txt = (rule.get("constraint_text") or "").strip()
                if not txt:
                    continue
                with self._conn() as c:
                    if not c.execute(
                        "SELECT 1 FROM kb_rules WHERE constraint_text=?", (txt,)
                    ).fetchone():
                        self.add_rule(rule)
                        result["added"] += 1
                    else:
                        result["skipped"] += 1
            except Exception:
                result["failed"] += 1

        return result

    # ── 维护建议（健康诊断） ──────────────────────────────

    def get_maintenance_advice(self) -> Dict:
        """
        扫描知识库，返回可操作的维护建议 + 健康分（0–100）。
        优先级：high（无触发词）> medium（从未命中）> low（缺翻译/示例）
        """
        advice: List[Dict] = []

        with self._conn() as c:
            total = c.execute(
                "SELECT COUNT(*) FROM kb_entries WHERE enabled=1"
            ).fetchone()[0]

            # HIGH: 无触发词（永远不会被检索到）
            no_trig = c.execute(
                "SELECT id, title FROM kb_entries "
                "WHERE enabled=1 AND (triggers='[]' OR triggers IS NULL OR triggers='') "
                "ORDER BY created_at DESC LIMIT 10"
            ).fetchall()
            for r in no_trig:
                advice.append({
                    "priority": "high", "type": "no_triggers",
                    "entry_id": r["id"], "title": r["title"],
                    "message": "无触发词，此条目永远不会被检索命中",
                    "action": "edit",
                })

            # MEDIUM: 创建 7 天以上但从未命中（use_count=0）
            stale = c.execute(
                "SELECT id, title, created_at FROM kb_entries "
                "WHERE enabled=1 AND use_count=0 "
                "AND created_at < datetime('now','-7 days') "
                "ORDER BY created_at ASC LIMIT 8"
            ).fetchall()
            for r in stale:
                advice.append({
                    "priority": "medium", "type": "stale",
                    "entry_id": r["id"], "title": r["title"],
                    "message": f"创建于 {r['created_at'][:10]}，从未被命中，建议优化触发词",
                    "action": "edit",
                })

            # MEDIUM: 无示例回复（AI 无参考话术）
            no_ex = c.execute(
                "SELECT id, title FROM kb_entries "
                "WHERE enabled=1 AND (example_reply_zh IS NULL OR example_reply_zh='') "
                "AND use_count > 0 "
                "ORDER BY use_count DESC LIMIT 5"
            ).fetchall()
            for r in no_ex:
                advice.append({
                    "priority": "medium", "type": "no_example",
                    "entry_id": r["id"], "title": r["title"],
                    "message": "高频命中但缺少示例回复，AI 难以生成标准话术",
                    "action": "edit",
                })

            # LOW: 缺少英文翻译的活跃条目（用 title IS NOT NULL 判断已有翻译）
            untrans = c.execute(
                "SELECT e.id, e.title FROM kb_entries e "
                "WHERE e.enabled=1 AND e.use_count > 0 "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM kb_translations t "
                "  WHERE t.entry_id=e.id AND t.lang='en' AND t.title IS NOT NULL AND t.title != ''"
                ") "
                "ORDER BY e.use_count DESC LIMIT 5"
            ).fetchall()
            for r in untrans:
                advice.append({
                    "priority": "low", "type": "untranslated",
                    "entry_id": r["id"], "title": r["title"],
                    "message": "被频繁使用但缺少英文翻译，影响非中文用户体验",
                    "action": "translate",
                })

        # 计算健康分
        priority_map = {"high": 0, "medium": 1, "low": 2}
        advice.sort(key=lambda x: priority_map[x["priority"]])
        h = sum(1 for a in advice if a["priority"] == "high")
        m = sum(1 for a in advice if a["priority"] == "medium")
        lo = sum(1 for a in advice if a["priority"] == "low")
        score = max(0, 100 - h * 20 - m * 8 - lo * 3)

        return {
            "score":   score,
            "grade":   "优秀" if score >= 90 else "良好" if score >= 70 else "待改进" if score >= 50 else "需关注",
            "total":   total,
            "advice":  advice,
            "counts":  {"high": h, "medium": m, "low": lo},
        }

    # ── 弱命中分析 & 自动建议 ──────────────────────────────

    def get_weak_hits(self, score_threshold: float = 0.50,
                      hours: int = 168, top_k: int = 20) -> List[Dict]:
        """
        获取"弱命中"查询：技术上匹配了（hit=1）但分数低于阈值的查询。
        按出现频次聚合，返回高频弱命中列表。
        """
        since = time.time() - hours * 3600
        with self._conn() as c:
            rows = c.execute(
                "SELECT query, score, matched_entry_id, category "
                "FROM kb_query_log "
                "WHERE ts >= ? AND hit=1 AND score > 0 AND score < ?",
                (since, score_threshold),
            ).fetchall()

        agg: Dict[str, Dict] = {}
        for r in rows:
            q = (r["query"] or "").strip().lower()[:100]
            if not q:
                continue
            if q not in agg:
                agg[q] = {
                    "query": r["query"][:100],
                    "count": 0,
                    "avg_score": 0.0,
                    "scores": [],
                    "matched_entries": set(),
                    "categories": set(),
                }
            a = agg[q]
            a["count"] += 1
            a["scores"].append(r["score"] or 0)
            if r["matched_entry_id"]:
                a["matched_entries"].add(r["matched_entry_id"])
            if r["category"]:
                a["categories"].add(r["category"])

        result = []
        for q, a in agg.items():
            a["avg_score"] = round(sum(a["scores"]) / len(a["scores"]), 3)
            result.append({
                "query":           a["query"],
                "count":           a["count"],
                "avg_score":       a["avg_score"],
                "matched_entries": list(a["matched_entries"]),
                "categories":      list(a["categories"]),
            })
        result.sort(key=lambda x: (-x["count"], x["avg_score"]))
        return result[:top_k]

    def get_overloaded_entries(self, hours: int = 168, min_diversity: int = 5) -> List[Dict]:
        """
        检测"过载条目" — 一个条目被大量不同查询命中（说明太泛需要拆分）。
        """
        since = time.time() - hours * 3600
        with self._conn() as c:
            rows = c.execute(
                "SELECT matched_entry_id, query, score "
                "FROM kb_query_log "
                "WHERE ts >= ? AND hit=1 AND matched_entry_id != ''",
                (since,),
            ).fetchall()

        entry_queries: Dict[str, Dict] = {}
        for r in rows:
            eid = r["matched_entry_id"]
            if eid not in entry_queries:
                entry_queries[eid] = {"queries": set(), "scores": [], "count": 0}
            e = entry_queries[eid]
            e["queries"].add((r["query"] or "")[:60].lower())
            e["scores"].append(r["score"] or 0)
            e["count"] += 1

        result = []
        for eid, e in entry_queries.items():
            diversity = len(e["queries"])
            if diversity < min_diversity:
                continue
            avg_sc = round(sum(e["scores"]) / len(e["scores"]), 3) if e["scores"] else 0
            title = ""
            with self._conn() as c:
                row = c.execute(
                    "SELECT title, category FROM kb_entries WHERE id=?", (eid,)
                ).fetchone()
                if row:
                    title = row["title"]
                    cat = row["category"]
                else:
                    cat = ""
            result.append({
                "entry_id":   eid,
                "title":      title,
                "category":   cat,
                "hit_count":  e["count"],
                "diversity":  diversity,
                "avg_score":  avg_sc,
                "sample_queries": list(e["queries"])[:8],
            })
        result.sort(key=lambda x: -x["diversity"])
        return result[:10]

    def get_auto_suggestions(self, weak_threshold: float = 0.50,
                             hours: int = 168, top_k: int = 10) -> List[Dict]:
        """
        综合 miss_log + 弱命中 + 过载条目分析，自动生成知识条目建议。
        返回可直接展示给运营人员审核的建议列表。
        """
        suggestions: List[Dict] = []

        # 来源 1：miss_log 高频未命中
        miss_stats = self.get_miss_stats(top_k=15)
        for m in miss_stats:
            if m["cnt"] >= 2:
                suggestions.append({
                    "source":   "miss",
                    "priority": "high",
                    "query":    m["query"],
                    "count":    m["cnt"],
                    "reason":   f"被问 {m['cnt']} 次但知识库无匹配",
                    "suggested_title": m["query"][:30],
                    "suggested_category": self._guess_category(m["query"]),
                    "suggested_triggers": self._extract_keywords(m["query"]),
                })

        # 来源 2：高频弱命中（匹配质量差）
        weak_hits = self.get_weak_hits(
            score_threshold=weak_threshold, hours=hours, top_k=15
        )
        for w in weak_hits:
            if w["count"] >= 2:
                suggestions.append({
                    "source":   "weak_hit",
                    "priority": "medium",
                    "query":    w["query"],
                    "count":    w["count"],
                    "avg_score": w["avg_score"],
                    "reason":   f"匹配分数仅 {w['avg_score']:.2f}，内容可能不够精准",
                    "suggested_title": w["query"][:30],
                    "suggested_category": (
                        w["categories"][0] if w["categories"] else
                        self._guess_category(w["query"])
                    ),
                    "suggested_triggers": self._extract_keywords(w["query"]),
                })

        # 来源 3：过载条目（一个条目匹配太多不同查询）
        overloaded = self.get_overloaded_entries(hours=hours)
        for o in overloaded:
            suggestions.append({
                "source":   "overloaded",
                "priority": "low",
                "query":    f"条目「{o['title']}」承载 {o['diversity']} 种不同查询",
                "count":    o["hit_count"],
                "reason":   f"此条目过于宽泛，建议拆分为更具体的条目",
                "entry_id": o["entry_id"],
                "sample_queries": o["sample_queries"],
                "suggested_category": o["category"],
            })

        priority_map = {"high": 0, "medium": 1, "low": 2}
        suggestions.sort(key=lambda x: (priority_map[x["priority"]], -x["count"]))
        return suggestions[:top_k]

    @staticmethod
    def _guess_category(query: str) -> str:
        q = (query or "").lower()
        if any(w in q for w in ("通道", "额度", "费率", "汇率", "代收", "代付")):
            return "通道状态类"
        if any(w in q for w in ("订单", "没到账", "到账", "成功", "失败", "超时")):
            return "订单查询类"
        if any(w in q for w in ("退款", "退钱", "赔偿", "投诉")):
            return "退款/投诉类"
        if any(w in q for w in ("错误", "报错", "error", "异常")):
            return "错误码类"
        return "其他"

    @staticmethod
    def _extract_keywords(query: str) -> List[str]:
        import re as _re
        q = (query or "").strip()
        words = _re.findall(r"[\u4e00-\u9fff]{2,6}", q)
        ascii_words = _re.findall(r"[a-zA-Z]{3,}", q)
        all_words = words + ascii_words
        stopwords = {"什么", "怎么", "为什么", "是不是", "有没有", "可以", "能不能",
                     "请问", "你好", "我想", "帮我", "一下", "问一下"}
        return [w for w in all_words if w not in stopwords][:6]

    # ── 回复质量追踪（M2） ────────────────────────────────

    def get_reply_quality_stats(self, days: int = 7) -> Dict:
        """
        聚合反馈数据，返回回复质量统计：
        - 总体满意度
        - 按来源（auto_detect / auto_repeat_detect / manual）分类
        - 低质量信号趋势
        """
        since = time.strftime(
            "%Y-%m-%dT%H:%M:%S",
            time.localtime(time.time() - days * 86400)
        )
        with self._conn() as c:
            rows = c.execute(
                "SELECT score, operator, correction, created_at "
                "FROM kb_feedback WHERE created_at >= ?",
                (since,)
            ).fetchall()

        total = len(rows)
        pos = sum(1 for r in rows if r["score"] > 0)
        neg = sum(1 for r in rows if r["score"] < 0)
        auto_repeat = sum(1 for r in rows if r["operator"] == "auto_repeat_detect")
        auto_detect = sum(1 for r in rows if r["operator"] == "auto_detect")
        manual = sum(1 for r in rows if r["operator"] not in
                     ("auto_detect", "auto_repeat_detect", ""))

        by_day: Dict[str, Dict] = {}
        for r in rows:
            day = (r["created_at"] or "")[:10]
            if day not in by_day:
                by_day[day] = {"pos": 0, "neg": 0, "total": 0}
            by_day[day]["total"] += 1
            if r["score"] > 0:
                by_day[day]["pos"] += 1
            elif r["score"] < 0:
                by_day[day]["neg"] += 1

        return {
            "total":        total,
            "positive":     pos,
            "negative":     neg,
            "satisfaction": round(pos / total * 100) if total else 0,
            "by_source": {
                "auto_repeat":  auto_repeat,
                "auto_detect":  auto_detect,
                "manual":       manual,
            },
            "by_day": [
                {"date": d, **v}
                for d, v in sorted(by_day.items())
            ],
        }

    # ── 向量化管理 ────────────────────────────────────────

    def set_embeddings(self, entry_embeddings: Dict[str, List[float]]):
        """批量写入 embedding（entry_id → vector）"""
        with self._conn() as c:
            for eid, vec in entry_embeddings.items():
                se = str(eid)
                c.execute(
                    "UPDATE kb_entries SET embedding=? WHERE id=?",
                    (json.dumps(vec), se)
                )
                self._vindex.update(se, vec)   # 实时更新内存索引
        self._touch_index()

    def set_single_embedding(self, entry_id: str, vec: List[float]):
        """写入单条 embedding 并立即生效（无需全量重建）"""
        eid = str(entry_id)
        with self._conn() as c:
            c.execute(
                "UPDATE kb_entries SET embedding=? WHERE id=?",
                (json.dumps(vec), eid)
            )
        self._vindex.update(eid, vec)
        KnowledgeBaseStore._index_dirty = False   # 仅向量层变更，BM25 无需重建

    def get_entries_without_embedding(self) -> List[Dict]:
        """返回尚未向量化的启用条目（以数据库为准，避免与内存索引不一致）"""
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, title, triggers, scenario, steps, principles, example_reply_zh "
                "FROM kb_entries WHERE enabled=1 AND ("
                "embedding IS NULL OR TRIM(IFNULL(embedding, '')) = '' "
                "OR TRIM(IFNULL(embedding, '')) = '[]'"
                ")"
            ).fetchall()
        return [dict(r) for r in rows]

    def embedding_coverage(self) -> Dict:
        """返回向量化覆盖率统计"""
        with self._conn() as c:
            total = c.execute("SELECT COUNT(*) FROM kb_entries WHERE enabled=1").fetchone()[0]
            done  = c.execute(
                "SELECT COUNT(*) FROM kb_entries WHERE enabled=1 AND embedding IS NOT NULL "
                "AND TRIM(IFNULL(embedding, '')) != '' AND TRIM(IFNULL(embedding, '')) != '[]'"
            ).fetchone()[0]
        return {
            "total":    total,
            "done":     done,
            "pending":  total - done,
            "pct":      round(done / total * 100, 1) if total else 0,
            "has_numpy": _HAS_NUMPY,
            "mode":     "hybrid (BM25+Vector)" if done > 0 else "bm25-only",
        }

    # ── 查重（相似条目检测）──────────────────────────────

    def find_duplicates(self, threshold: float = 0.85) -> List[Dict]:
        """
        利用向量余弦相似度找可能重复的条目对（threshold≥0.85 说明内容高度相似）。
        """
        if self._vindex.count() < 2:
            return []
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, title, category, embedding FROM kb_entries "
                "WHERE embedding IS NOT NULL AND enabled=1"
            ).fetchall()
        if len(rows) < 2:
            return []

        # 解析向量
        id_vec: List[Tuple[str, str, str, List[float]]] = []
        for r in rows:
            try:
                vec = json.loads(r["embedding"])
                id_vec.append((r["id"], r["title"], r["category"], vec))
            except Exception:
                pass

        duplicates: List[Dict] = []
        for i in range(len(id_vec)):
            for j in range(i + 1, len(id_vec)):
                sim = _cosine_sim_pure(id_vec[i][3], id_vec[j][3])
                if sim >= threshold:
                    duplicates.append({
                        "id1":    id_vec[i][0], "title1": id_vec[i][1],
                        "cat1":   id_vec[i][2],
                        "id2":    id_vec[j][0], "title2": id_vec[j][1],
                        "cat2":   id_vec[j][2],
                        "similarity": round(sim, 3),
                    })
        return sorted(duplicates, key=lambda x: -x["similarity"])

    # ── 知识库备份 ────────────────────────────────────────

    def backup(self, backup_dir: Path) -> str:
        """创建 SQLite 热备份（使用官方 backup API，WAL 安全）"""
        backup_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        backup_path = backup_dir / f"kb_{ts}.db"
        src = sqlite3.connect(str(self.db_path), check_same_thread=False)
        dst = sqlite3.connect(str(backup_path))
        src.backup(dst)
        dst.close()
        src.close()
        return str(backup_path)

    def restore(self, backup_path: Path):
        """从备份恢复（关闭连接、覆盖文件、重建索引）"""
        if not backup_path.exists():
            raise FileNotFoundError(f"备份文件不存在: {backup_path}")
        shutil.copy2(str(backup_path), str(self.db_path))
        self._rebuild_index()

    @staticmethod
    def list_backups(backup_dir: Path) -> List[Dict]:
        if not backup_dir.exists():
            return []
        files = sorted(backup_dir.glob("kb_*.db"), reverse=True)
        result = []
        for f in files[:20]:  # 最多展示 20 个
            stat = f.stat()
            result.append({
                "filename": f.name,
                "size_kb":  round(stat.st_size / 1024, 1),
                "created":  time.strftime("%Y-%m-%d %H:%M:%S",
                                          time.localtime(stat.st_mtime)),
            })
        return result


# ── 全局默认错误码预置数据 ────────────────────────────────

DEFAULT_ERROR_CODES = [
    {
        "id": "ec_x004",
        "code": "X004",
        "source_text": "Transaction Failed please contact support X004",
        "explanation_zh": "支付系统层面拒绝了该交易，属于系统级错误",
        "suggestion_zh": "建议用户重新发起一笔新订单，如多次失败请联系技术支持",
        "explanation_en": "The transaction was rejected by the payment system (system-level error)",
        "suggestion_en": "Please initiate a new order. If the issue persists, contact support.",
    },
    {
        "id": "ec_g2p_t97",
        "code": "G2P-T-97",
        "source_text": "There was a problem with your request. Please recheck the parameters/format and try again",
        "explanation_zh": "请求参数或格式有误，订单已退款",
        "suggestion_zh": "该笔订单已退款，请确认参数格式后重新提交",
        "explanation_en": "Request parameter or format error, order has been refunded",
        "suggestion_en": "Order has been refunded. Please check parameters and resubmit.",
    },
    {
        "id": "ec_txn_failed",
        "code": "Transaction has been failed",
        "source_text": "Transaction has been failed",
        "explanation_zh": "网络超时或支付处理失败，订单已自动退款",
        "suggestion_zh": "网络超时导致，款项已退回，请重新发起订单",
        "explanation_en": "Network timeout or payment processing failure, order auto-refunded",
        "suggestion_en": "Network timeout occurred. Funds have been returned. Please retry.",
    },
    {
        "id": "ec_not_found",
        "code": "未查询到该笔订单",
        "source_text": "未查询到该笔订单",
        "explanation_zh": "在本系统中未找到该订单号，可能是单号有误或不属于本系统",
        "suggestion_zh": "请核对订单号是否正确，或确认是否走的是本平台通道",
        "explanation_en": "Order not found in our system",
        "suggestion_en": "Please verify the order number is correct and belongs to this platform.",
    },
]

DEFAULT_RULES = [
    {
        "id": "rule_no_fabricate",
        "priority": 10,
        "description": "禁止编造订单信息",
        "trigger_condition": "任何涉及订单的回复",
        "constraint_text": "严禁编造任何金额、单号、时间、状态信息。只能基于系统返回的真实数据回复。",
        "is_global": 1,
    },
    {
        "id": "rule_no_time_promise",
        "priority": 9,
        "description": "禁止承诺具体到账时间",
        "trigger_condition": "用户询问何时到账",
        "constraint_text": "不能说「马上到」「1分钟内」等具体时间承诺。可说「收到后系统自动处理」「请耐心等待」。",
        "is_global": 1,
    },
    {
        "id": "rule_apologize_first",
        "priority": 8,
        "description": "投诉场景先道歉",
        "trigger_condition": "用户表达不满、投诉、等待太久",
        "constraint_text": "检测到负面情绪时，回复第一句必须是道歉，再索要信息。",
        "is_global": 1,
    },
    {
        "id": "rule_short_reply",
        "priority": 5,
        "description": "回复简短克制",
        "trigger_condition": "所有场景",
        "constraint_text": "回复不超过3句话，移动端友好，避免大段文字。",
        "is_global": 1,
    },
]

# 标记「本库曾经存在过业务数据」。清空 kb_entries 后若仍为 1，则不再自动灌回默认种子，避免「删光重启又长回来」。
KB_SEEDED_ONCE_KEY = "kb_seeded_once"


def ensure_kb_seeded_once_meta(store: "KnowledgeBaseStore") -> None:
    """旧库迁移：仅当 kb_entries 曾非空时打标；避免仅靠错误码/规则触发表导致误抑制系统话术种子。"""
    if store.get_meta(KB_SEEDED_ONCE_KEY):
        return
    with store._conn() as c:
        n_ent = c.execute("SELECT COUNT(*) FROM kb_entries").fetchone()[0]
    if n_ent > 0:
        store.set_meta(KB_SEEDED_ONCE_KEY, "1")


def seed_default_data(store: "KnowledgeBaseStore"):
    """首次初始化时写入默认错误码和规则（已存在则跳过）"""
    ensure_kb_seeded_once_meta(store)
    with store._conn() as c:
        existing_codes = c.execute("SELECT COUNT(*) FROM kb_error_codes").fetchone()[0]
        existing_rules = c.execute("SELECT COUNT(*) FROM kb_rules").fetchone()[0]

    # 用户曾使用过知识库后全部清空：不再自动写回默认错误码/规则
    if existing_codes == 0 and existing_rules == 0 and store.get_meta(KB_SEEDED_ONCE_KEY):
        return

    if not existing_codes:
        for ec in DEFAULT_ERROR_CODES:
            store.add_error_code(ec)

    if not existing_rules:
        for rule in DEFAULT_RULES:
            store.add_rule(rule)


# ── 种子数据（电商 / SaaS 客服场景）──────────────────────────────


SEED_ENTRIES_ECOMMERCE = [
    {
        "category": "订单查询",
        "title": "查询订单状态",
        "triggers": '["查单","订单状态","单号","查询订单","order","order status"]',
        "scenario": "用户想查询自己的订单当前进度",
        "steps": "1. 礼貌询问订单号\n2. 告知查询方式（系统/人工）\n3. 提供结果或预计时间",
        "principles": "响应迅速，明确告知处理进度，避免让用户等待超5分钟",
        "example_reply_zh": "您好！请提供您的订单号，我为您立刻查询 😊",
        "forbidden": "绝对不说「我不知道」「找别人问」",
    },
    {
        "category": "物流跟踪",
        "title": "包裹未到/延误",
        "triggers": '["包裹","没收到","快递","物流","延误","shipping","delivery"]',
        "scenario": "用户反映包裹超时未收到",
        "steps": "1. 表达歉意\n2. 查询物流单号\n3. 联系物流公司确认\n4. 给出补偿方案",
        "principles": "同理心优先，主动联系物流，48小时内给出结果",
        "example_reply_zh": "非常抱歉您还没收到包裹！我马上帮您核查物流情况，请稍等 🙏",
        "forbidden": "不要说「这是物流公司的问题」",
    },
    {
        "category": "退款退货",
        "title": "申请退款",
        "triggers": '["退款","退钱","refund","退货","return"]',
        "scenario": "用户要求退款或退货",
        "steps": "1. 确认退款原因\n2. 说明退款流程\n3. 告知到账时间（3-7个工作日）",
        "principles": "无理由退款7天内支持，态度友好不推诿",
        "example_reply_zh": "没问题，我们支持7天无理由退款！请告诉我您的订单号，我为您办理 ✅",
        "forbidden": "不要说「不能退」「过了时间」（7天内）",
    },
    {
        "category": "商品咨询",
        "title": "商品规格/尺寸询问",
        "triggers": '["尺寸","规格","型号","颜色","size","spec","color"]',
        "scenario": "用户询问商品具体参数",
        "steps": "1. 确认对方咨询的商品\n2. 提供详细规格\n3. 推荐合适选项",
        "principles": "准确、完整，结合用户需求推荐最适合的",
        "example_reply_zh": "这款商品有以下规格可选：[列出规格]，请问您的需求是？",
        "forbidden": "不要说「自己去看商品页」",
    },
    {
        "category": "支付问题",
        "title": "支付失败处理",
        "triggers": '["支付失败","付款失败","payment failed","无法支付","不能付款"]',
        "scenario": "用户支付时遇到错误",
        "steps": "1. 了解支付方式\n2. 常见原因说明（余额/限额/网络）\n3. 提供备用支付方式",
        "principles": "冷静引导，不要让用户感到沮丧，提供替代方案",
        "example_reply_zh": "抱歉支付遇到问题！常见原因是余额不足或银行限额，您可以试试 [备用方式]，或联系您的银行确认 📞",
        "forbidden": "不要说「我也不知道为什么」",
    },
    {
        "category": "投诉处理",
        "title": "服务态度投诉",
        "triggers": '["投诉","态度差","不满意","complaint","angry","upset"]',
        "scenario": "用户对服务表示不满",
        "steps": "1. 真诚道歉\n2. 倾听具体诉求\n3. 提出补偿方案\n4. 承诺改进",
        "principles": "绝不辩解，先认错后解决，主动提补偿",
        "example_reply_zh": "非常抱歉给您带来了不好的体验 😔 请告诉我具体情况，我会尽全力为您解决，并给予相应补偿。",
        "forbidden": "不要辩解、不要转移话题",
    },
    {
        "category": "账户安全",
        "title": "账户被盗/异常登录",
        "triggers": '["账户被盗","异常登录","密码泄露","account hacked","security"]',
        "scenario": "用户反映账户安全问题",
        "steps": "1. 立即冻结账户\n2. 核实身份\n3. 帮助重置密码\n4. 检查异常操作",
        "principles": "紧急优先，账户安全第一，立即行动",
        "example_reply_zh": "这很紧急！我立即帮您冻结账户防止损失。请确认您的注册邮箱/手机号，我帮您重置密码 🔒",
        "forbidden": "不要拖延，不要让用户等待超1分钟响应",
    },
    {
        "category": "优惠活动",
        "title": "优惠券/折扣咨询",
        "triggers": '["优惠","折扣","券","coupon","discount","promo","促销"]',
        "scenario": "用户询问当前优惠活动",
        "steps": "1. 告知当前活动\n2. 说明使用条件\n3. 主动发送优惠码（如有）",
        "principles": "主动营销，告知限时活动，制造紧迫感",
        "example_reply_zh": "现在有 [活动名称]！满 [金额] 减 [金额]，优惠码：[CODE]，有效期至 [日期] ⏰",
        "forbidden": "不要遗漏告知有效期和使用条件",
    },
]

SEED_ENTRIES_SAAS = [
    {
        "category": "账单计费",
        "title": "账单金额异常",
        "triggers": '["账单","账单错误","多扣","billing","invoice","charge"]',
        "scenario": "用户对账单金额有疑问",
        "steps": "1. 调出用户账单详情\n2. 逐项解释费用\n3. 如有误收立即退款",
        "principles": "透明、准确，误收款 24 小时内处理",
        "example_reply_zh": "我帮您调出账单明细：[明细]。如您认为有误，我立即升级处理 🔍",
        "forbidden": "不要说「账单都是对的」",
    },
    {
        "category": "功能使用",
        "title": "功能无法正常使用",
        "triggers": '["功能失效","不能用","bug","故障","feature not working"]',
        "scenario": "用户反映某功能不工作",
        "steps": "1. 确认具体功能和错误信息\n2. 尝试初步排查（刷新/重登）\n3. 提交技术工单\n4. 提供预计修复时间",
        "principles": "快速收集信息，优先安抚，告知处理时间",
        "example_reply_zh": "非常抱歉功能出现问题！我已为您提交技术工单（#[编号]），预计 [时间] 内修复，期间可以用 [替代方案] 🛠",
        "forbidden": "不要说「这不是我们的问题」",
    },
    {
        "category": "套餐升级",
        "title": "升级/降级套餐",
        "triggers": '["升级","降级","换套餐","upgrade","downgrade","plan change"]',
        "scenario": "用户想更改订阅套餐",
        "steps": "1. 了解当前套餐和目标套餐\n2. 说明差价/退款规则\n3. 帮助操作变更",
        "principles": "灵活处理，主动推荐最适合的套餐",
        "example_reply_zh": "当然可以！升级至 [套餐名] 只需补差价 [金额]，立即生效。我帮您办理吗？🚀",
        "forbidden": "不要阻止用户降级",
    },
]


def seed_kb_examples(store: "KnowledgeBaseStore", category: str = "all") -> dict:
    """导入内置电商/SaaS场景示例知识条目，已存在（同标题）则跳过"""
    from datetime import datetime, timezone

    datasets = []
    if category in ("all", "ecommerce"):
        datasets.extend(SEED_ENTRIES_ECOMMERCE)
    if category in ("all", "saas"):
        datasets.extend(SEED_ENTRIES_SAAS)

    if not datasets:
        return {"added": 0, "skipped": 0, "failed": 0}

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    result = {"added": 0, "skipped": 0, "failed": 0}

    with store._conn() as c:
        existing_titles = {
            row[0] for row in c.execute("SELECT title FROM kb_entries").fetchall()
        }

    for entry in datasets:
        if entry["title"] in existing_titles:
            result["skipped"] += 1
            continue
        try:
            store.add_entry(entry)
            existing_titles.add(entry["title"])
            result["added"] += 1
        except Exception:
            result["failed"] += 1

    return result


# ── E0: 系统话术种子数据 ─────────────────────────────────────

SYSTEM_REPLY_SEEDS: List[Dict[str, Any]] = [
    # ── 全局兜底 ──
    {
        "category": "系统话术", "title": "全局兜底话术",
        "template_key": "global_fallback", "reply_mode": "direct",
        "fallback_group": "global",
        "triggers": '[]',
        "example_reply_zh": (
            "在的，请您稍等一下～\n---\n"
            "收到，马上为您处理。\n---\n"
            "好的亲，稍等我看一下～\n---\n"
            "您好，请稍等片刻～\n---\n"
            "收到啦，这就帮您查看。"
        ),
    },
    # ── 按意图兜底 ──
    {
        "category": "系统话术", "title": "问候兜底",
        "template_key": "greeting_fallback", "reply_mode": "direct",
        "fallback_group": "greeting",
        "triggers": '[]',
        "example_reply_zh": (
            "在的～有啥可以帮您的？\n---\n"
            "亲，我们24小时竭诚为您服务，请把您的需求发给我。\n---\n"
            "宝，有什么可以为您效劳呀，请把需求告诉我。"
        ),
    },
    {
        "category": "系统话术", "title": "订单查询兜底（无单号）",
        "template_key": "order_query_fallback", "reply_mode": "direct",
        "fallback_group": "order_query",
        "triggers": '[]',
        "example_reply_zh": "暂未看到订单信息，请发订单号或清晰付款截图，我帮您查。",
    },
    {
        "category": "系统话术", "title": "订单查询兜底（有单号）",
        "template_key": "order_query_with_number_fallback", "reply_mode": "direct",
        "fallback_group": "order_query",
        "triggers": '[]',
        "template_vars": '["order_number"]',
        "example_reply_zh": (
            "已收到订单号 {order_number}，稍等我帮您核查。\n---\n"
            "订单 {order_number} 已收到，马上为您查询～"
        ),
    },
    {
        "category": "系统话术", "title": "价格咨询兜底",
        "template_key": "price_check_fallback", "reply_mode": "direct",
        "fallback_group": "price_check",
        "triggers": '[]',
        "example_reply_zh": (
            "手续费/费率的具体数值与比例由业务主管或人工客服对接说明，我这里不报价率数字。\n---\n"
            "需要了解通道额度或运行状态，我可以帮您说明。"
        ),
    },
    {
        "category": "系统话术", "title": "状态查询兜底",
        "template_key": "status_check_fallback", "reply_mode": "direct",
        "fallback_group": "status_check",
        "triggers": '[]',
        "example_reply_zh": (
            "请告诉我具体情况，我帮您查一下状态。\n---\n"
            "需要查询什么状态？订单状态还是通道状态？"
        ),
    },
    {
        "category": "系统话术", "title": "通道信息兜底",
        "template_key": "channel_info_fallback", "reply_mode": "direct",
        "fallback_group": "channel_info",
        "triggers": '[]',
        "example_reply_zh": (
            "当前可用通道可为您说明额度与运行状态；手续费/费率的具体数值与比例不在此展示，"
            "请咨询您的业务主管或人工客服。\n---\n"
            "需要了解哪个通道的额度或状态？我帮您说明。"
        ),
    },
    {
        "category": "系统话术", "title": "投诉处理兜底",
        "template_key": "complaint_fallback", "reply_mode": "direct",
        "fallback_group": "complaint",
        "triggers": '[]',
        "example_reply_zh": (
            "抱歉给您带来不便，请描述具体问题，我来帮您处理。\n---\n"
            "非常抱歉听到您的问题，请告诉我具体情况，我会尽快处理。"
        ),
    },
    {
        "category": "系统话术", "title": "闲聊兜底",
        "template_key": "small_talk_fallback", "reply_mode": "direct",
        "fallback_group": "small_talk",
        "triggers": '[]',
        "example_reply_zh": (
            "有什么可以帮您的吗？\n---\n"
            "在的～有什么需要咨询的吗？"
        ),
    },
    {
        "category": "系统话术", "title": "测试回复",
        "template_key": "test_reply", "reply_mode": "direct",
        "fallback_group": "test",
        "triggers": '[]',
        "example_reply_zh": (
            "✅ 系统运行正常！Camille AI已就绪。\n---\n"
            "测试收到，一切正常！"
        ),
    },
    # ── GXP 流程模板 ──
    {
        "category": "系统话术", "title": "GXP 意图确认",
        "template_key": "gxp_ask_intent", "reply_mode": "direct",
        "template_vars": '["order_no"]',
        "triggers": '[]',
        "example_reply_zh": (
            "收到单号 {order_no}。您需要我帮您：查询代收 / 回调代收 / 查询提现 / 回调提现？"
            "请直接说一项或发数字（1 查代收 2 回调代收 3 查提现 4 回调提现）。"
        ),
    },
    {
        "category": "系统话术", "title": "GXP 重复单号确认",
        "template_key": "gxp_ask_same_no", "reply_mode": "direct",
        "template_vars": '["order_no"]',
        "triggers": '[]',
        "example_reply_zh": "单号 {order_no} 已记录，您需要：查询代收 / 回调代收 / 查询提现 / 回调提现？",
    },
    {
        "category": "系统话术", "title": "GXP 请先发单号",
        "template_key": "gxp_need_order_no", "reply_mode": "direct",
        "triggers": '[]',
        "example_reply_zh": "请先发单号，再说需求；或直接说：查代收 单号。",
    },
    {
        "category": "系统话术", "title": "GXP 单号已过期",
        "template_key": "gxp_expired", "reply_mode": "direct",
        "triggers": '[]',
        "example_reply_zh": "单号已过期，请重新发单号后再选需求。",
    },
    {
        "category": "系统话术", "title": "GXP 功能菜单",
        "template_key": "gxp_ask_what", "reply_mode": "direct",
        "triggers": '[]',
        "example_reply_zh": (
            "您要查的是：1 汇率 2 余额 3 代收订单状态 4 提现订单状态 5 代收成功率？"
            "请直接说一项或发数字。"
        ),
    },
    {
        "category": "系统话术", "title": "GXP 带单号功能菜单",
        "template_key": "gxp_ask_what_with_order", "reply_mode": "direct",
        "template_vars": '["order_no"]',
        "triggers": '[]',
        "example_reply_zh": (
            "收到单号 {order_no}。您要查的是：1 汇率 2 余额 3 代收订单状态 "
            "4 提现订单状态 5 代收成功率？请直接说一项或发数字。"
        ),
    },
    # ── GXP 引导硬编码 → KB ──
    {
        "category": "系统话术", "title": "GXP 查代收提示",
        "template_key": "gxp_hint_query_deposit", "reply_mode": "direct",
        "triggers": '[]',
        "example_reply_zh": "查代收订单请发单号，或说：查代收 单号。",
    },
    {
        "category": "系统话术", "title": "GXP 查提现提示",
        "template_key": "gxp_hint_query_withdraw", "reply_mode": "direct",
        "triggers": '[]',
        "example_reply_zh": "查提现订单请发单号，或说：查提现 单号；也可直接回复「查提现」看列表。",
    },
    {
        "category": "系统话术", "title": "GXP 回调代收提示",
        "template_key": "gxp_hint_callback_deposit", "reply_mode": "direct",
        "triggers": '[]',
        "example_reply_zh": "请提供单号，例如：回调代收 12345678",
    },
    {
        "category": "系统话术", "title": "GXP 回调提现提示",
        "template_key": "gxp_hint_callback_withdraw", "reply_mode": "direct",
        "triggers": '[]',
        "example_reply_zh": "请提供单号，例如：回调提现 12345678",
    },
    {
        "category": "系统话术", "title": "GXP 模拟回调提示",
        "template_key": "gxp_hint_mock_callback", "reply_mode": "direct",
        "triggers": '[]',
        "example_reply_zh": "请提供商户单号，例如：代收模拟回调 12345678",
    },
    {
        "category": "系统话术", "title": "GXP UTR查询提示",
        "template_key": "gxp_hint_utr_query", "reply_mode": "direct",
        "triggers": '[]',
        "example_reply_zh": "请提供 UTR（12位数字）或单号，例如：utr查询 123456789012 或 补单 12345678",
    },
    {
        "category": "系统话术", "title": "GXP 请求已发起",
        "template_key": "gxp_request_sent", "reply_mode": "direct",
        "template_vars": '["hint"]',
        "triggers": '[]',
        "example_reply_zh": "已帮您发起请求，{hint}",
    },
    {
        "category": "系统话术", "title": "GXP 处理中兜底",
        "template_key": "gxp_processing_fallback", "reply_mode": "direct",
        "triggers": '[]',
        "example_reply_zh": "好的亲，系统正在处理中，请稍等片刻～",
    },
]


def seed_system_replies(store: "KnowledgeBaseStore") -> dict:
    """E0: 将所有硬编码话术 + 模板迁移为 KB 条目（已存在则跳过）"""
    result = {"added": 0, "skipped": 0, "failed": 0}
    ensure_kb_seeded_once_meta(store)
    with store._conn() as c:
        total_entries = c.execute("SELECT COUNT(*) FROM kb_entries").fetchone()[0]
        existing_keys = {
            r[0] for r in c.execute(
                "SELECT template_key FROM kb_entries WHERE template_key != ''"
            ).fetchall()
        }
    if total_entries == 0 and store.get_meta(KB_SEEDED_ONCE_KEY):
        return {
            **result,
            "skipped": len(SYSTEM_REPLY_SEEDS),
            "suppressed_empty_after_user_clear": True,
        }
    for entry in SYSTEM_REPLY_SEEDS:
        key = entry.get("template_key", "")
        if key in existing_keys:
            result["skipped"] += 1
            continue
        try:
            store.add_entry(entry)
            existing_keys.add(key)
            result["added"] += 1
        except Exception:
            result["failed"] += 1
    ensure_kb_seeded_once_meta(store)
    return result
