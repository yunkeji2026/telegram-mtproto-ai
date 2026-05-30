"""跨平台 RPA 共享工具函数。"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── P6-A / P7-C: 意图关键词分类表（三平台复用）──────────────────────────────

# P14-B: YAML-loadable 意图字典（缺文件时回退到内置默认）
_INTENT_TAGS_DEFAULT: Dict[str, List[str]] = {
    "purchase": ["买", "购买", "下单", "价格", "多少钱", "怎么买", "付款", "订单",
                 "发货", "运费", "优惠", "打折", "包邮", "费用", "多少",
                 "buy", "order", "price", "cost", "purchase", "pay", "checkout",
                 "discount", "promo", "ราคา", "ซื้อ", "giá", "mua"],
    "support":  ["问题", "不行", "故障", "退款", "投诉", "不好用", "坏了", "维修",
                 "售后", "退货", "补偿", "赔偿", "客服", "帮我", "解决",
                 "problem", "issue", "broken", "not working", "refund", "complaint",
                 "fix", "help", "support", "wrong", "damage", "เสีย", "hư", "lỗi"],
    "inquiry":  ["请问", "咨询", "了解", "想知道", "能不能", "可以吗", "有没有",
                 "介绍", "说明", "怎样", "如何", "什么是", "是否",
                 "how", "what", "when", "where", "why", "can i", "do you",
                 "tell me", "info", "information", "ขอ", "เกี่ยว", "hỏi"],
    "greeting": ["你好", "hi", "hello", "在吗", "嗨", "哈喽", "打扰", "您好", "hey",
                 "good morning", "good afternoon", "good evening", "สวัสดี",
                 "xin chào", "halo", "salam", "안녕"],
}


def _intent_tags_yaml_path() -> Path:
    """允许通过环境变量覆写（测试 / 多环境部署）。"""
    override = os.environ.get("INTENT_TAGS_PATH")
    if override:
        return Path(override)
    # repo-root/config/intent_tags.yaml — 从本文件 (.../src/integrations/) 推算
    return Path(__file__).resolve().parents[2] / "config" / "intent_tags.yaml"


def _load_intent_tags() -> Dict[str, List[str]]:
    """P15-D: 启动期/热更期加载意图字典 — 任何错误都回退默认值（永不抛）。

    校验规则：
      - 顶层必须是 mapping
      - 每个 tag 必须是 str；value 必须是 list
      - 每个 keyword 转 str 并 strip；空 keyword 跳过
      - 任何不合规项 → warning 日志（含 tag/yaml 路径）但继续加载其他项
    """
    p = _intent_tags_yaml_path()
    if not p.exists():
        logger.debug("intent_tags.yaml not found at %s — using built-in defaults", p)
        return dict(_INTENT_TAGS_DEFAULT)
    try:
        import yaml  # 仅在加载时按需 import，避免硬依赖
    except ImportError:
        logger.warning("PyYAML not available; using built-in intent tags")
        return dict(_INTENT_TAGS_DEFAULT)
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        # yaml.YAMLError 通常自带 problem_mark with line/col
        line_info = ""
        mark = getattr(exc, "problem_mark", None)
        if mark is not None:
            line_info = f" (line {mark.line + 1}, col {mark.column + 1})"
        logger.warning("intent_tags.yaml parse failed%s: %s — using defaults",
                       line_info, exc)
        return dict(_INTENT_TAGS_DEFAULT)
    if not isinstance(data, dict):
        logger.warning("intent_tags.yaml top-level is %s, not a mapping — using defaults",
                       type(data).__name__)
        return dict(_INTENT_TAGS_DEFAULT)

    out: Dict[str, List[str]] = {}
    skipped: List[str] = []
    # P16-D: 保留键（schema_version 等）静默跳过，不视为错误
    _reserved = {"schema_version", "_meta", "_comment"}
    for tag, kws in data.items():
        if not isinstance(tag, str):
            skipped.append(f"non-str tag {tag!r}")
            continue
        if tag in _reserved:
            continue
        if not isinstance(kws, list):
            skipped.append(f"{tag} (value is {type(kws).__name__}, expected list)")
            continue
        cleaned: List[str] = []
        for kw in kws:
            try:
                s = str(kw).strip()
            except Exception:
                continue
            if s:
                cleaned.append(s)
        if cleaned:
            out[tag] = cleaned
        else:
            skipped.append(f"{tag} (empty after cleaning)")

    if skipped:
        logger.warning("intent_tags.yaml: skipped entries: %s", "; ".join(skipped))
    if not out:
        logger.warning("intent_tags.yaml produced 0 valid tags — using built-in defaults")
        return dict(_INTENT_TAGS_DEFAULT)
    return out


# P16-D: schema 版本 — 写入文件时记录；加载时不当成 tag
INTENT_TAGS_SCHEMA_VERSION = 1
_RESERVED_KEYS = {"schema_version", "_meta", "_comment"}


_INTENT_TAGS: Dict[str, List[str]] = _load_intent_tags()

# P18-B: 编辑活动指标（写次数 / reload 次数 / restore 次数 / 最近编辑时间）
_INTENT_TAGS_EDIT_STATS: Dict[str, Any] = {
    "writes": 0,
    "reloads": 0,
    "restores": 0,
    "last_edit_ts": 0.0,
}

# P19-C: 滑窗 — 记录最近 1h 内写入时间戳（用于 edits_1h 指标）
from collections import deque as _deque
import threading as _threading
_INTENT_TAGS_EDIT_WINDOW: "_deque[float]" = _deque(maxlen=1024)
_EDIT_WINDOW_SEC = 3600.0
# P20-A: 单锁覆盖 counter + window mutation；FastAPI 多 worker 不共享内存但
# 同进程 async + 多线程仍需要原子保证。
_INTENT_TAGS_STATS_LOCK = _threading.Lock()
# P20-A: 单独的 write 锁 — 串行化 yaml 文件写入（防 Windows 同名 .tmp 竞争）
# 不复用 stats lock，避免阻塞 stats 读取
_INTENT_TAGS_WRITE_LOCK = _threading.Lock()


def _record_edit(ts: float) -> None:
    """P19-C / P20-A: 加入滑窗 + 清理 > 1h 的旧 ts（写锁内调用）。"""
    with _INTENT_TAGS_STATS_LOCK:
        _INTENT_TAGS_EDIT_WINDOW.append(ts)
        cutoff = ts - _EDIT_WINDOW_SEC
        while _INTENT_TAGS_EDIT_WINDOW and _INTENT_TAGS_EDIT_WINDOW[0] < cutoff:
            _INTENT_TAGS_EDIT_WINDOW.popleft()


def _bump_stat(key: str, delta: int = 1) -> None:
    """P20-A: 原子自增 counter（已在锁内时不要再调用）。"""
    with _INTENT_TAGS_STATS_LOCK:
        _INTENT_TAGS_EDIT_STATS[key] = _INTENT_TAGS_EDIT_STATS.get(key, 0) + delta


def _set_stat(key: str, value: Any) -> None:
    """P20-A: 原子赋值。"""
    with _INTENT_TAGS_STATS_LOCK:
        _INTENT_TAGS_EDIT_STATS[key] = value


def get_intent_tags_edit_stats() -> Dict[str, Any]:
    """P18-B / P19-C / P20-A / P22-C: 返回编辑活动指标（锁内拍快照）。"""
    now = time.time()
    cutoff = now - _EDIT_WINDOW_SEC
    with _INTENT_TAGS_STATS_LOCK:
        # 读取时再清一次过期 ts
        while _INTENT_TAGS_EDIT_WINDOW and _INTENT_TAGS_EDIT_WINDOW[0] < cutoff:
            _INTENT_TAGS_EDIT_WINDOW.popleft()
        out = dict(_INTENT_TAGS_EDIT_STATS)
        out["edits_1h"] = len(_INTENT_TAGS_EDIT_WINDOW)
        # P22-C: 持久化失败指标
        out["save_failures_total"] = _stats_save_failures.get("total", 0)
        out["save_failures_consecutive"] = _stats_save_failures.get("consecutive", 0)
        out["save_last_failure_ts"] = _stats_save_failures.get("last_failure_ts", 0.0)
    return out


def reset_intent_tags_edit_window() -> None:
    """P20-D: 测试用 — 清空滑窗（不重置 counter）。"""
    with _INTENT_TAGS_STATS_LOCK:
        _INTENT_TAGS_EDIT_WINDOW.clear()


# ────────────────────────────────────────────────────────────────────────
# P20-C: Counter 持久化到 intent_tags.yaml.stats.json sidecar
# ────────────────────────────────────────────────────────────────────────


def _intent_tags_stats_path() -> Path:
    """Stats sidecar lives next to intent_tags.yaml."""
    p = _intent_tags_yaml_path()
    return p.parent / (p.name + ".stats.json")


# P21-D: 持久化防抖 — 最小写间隔（秒）+ 测试加速钩子
_STATS_SAVE_MIN_INTERVAL_SEC = 1.0
_stats_last_save_ts: float = 0.0
# 测试可设为 True 完全跳过持久化（如 test_concurrent_writes）
_DISABLE_STATS_PERSISTENCE: bool = False

# P22-C: 持久化失败追踪
_stats_save_failures: Dict[str, Any] = {
    "consecutive": 0,    # 连续失败次数（成功重置为 0）
    "total": 0,          # 累计失败次数
    "last_error": "",    # 最近一次失败信息
    "last_failure_ts": 0.0,
}
_STATS_FAILURE_ALERT_THRESHOLD = 3  # 连续失败达此阈值升级为 ERROR


def _save_stats_persistent(*, force: bool = False) -> None:
    """P20-C: 把 counter + window 写到 sidecar JSON（best-effort，原子写）。

    P21-D: 默认 1s 防抖；测试可通过 _DISABLE_STATS_PERSISTENCE 完全跳过。
    异常吞掉 — 持久化失败不应阻塞业务路径。
    """
    global _stats_last_save_ts
    if _DISABLE_STATS_PERSISTENCE:
        return
    now = time.time()
    if not force and (now - _stats_last_save_ts) < _STATS_SAVE_MIN_INTERVAL_SEC:
        return  # 防抖：跳过本次写
    try:
        sp = _intent_tags_stats_path()
        sp.parent.mkdir(parents=True, exist_ok=True)
        with _INTENT_TAGS_STATS_LOCK:
            payload = {
                "writes": _INTENT_TAGS_EDIT_STATS.get("writes", 0),
                "reloads": _INTENT_TAGS_EDIT_STATS.get("reloads", 0),
                "restores": _INTENT_TAGS_EDIT_STATS.get("restores", 0),
                "last_edit_ts": _INTENT_TAGS_EDIT_STATS.get("last_edit_ts", 0.0),
                # P19-C: 持久化滑窗（重启后还能算出 edits_1h）
                "edit_window": list(_INTENT_TAGS_EDIT_WINDOW),
                "saved_at": time.time(),
            }
        tmp = sp.with_suffix(sp.suffix + ".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, sp)
        _stats_last_save_ts = now  # P21-D: only update on success
        # P22-C / P23-C: clear consecutive failure counter on success（线程安全）
        with _INTENT_TAGS_STATS_LOCK:
            prev = _stats_save_failures["consecutive"]
            _stats_save_failures["consecutive"] = 0
        if prev > 0:
            logger.info("intent_tags stats persistence recovered after %d failures", prev)
    except Exception as exc:
        # P22-C / P23-C: 锁内累计失败 + 指数退避日志（防 1000x ERROR 刷屏）
        with _INTENT_TAGS_STATS_LOCK:
            _stats_save_failures["consecutive"] += 1
            _stats_save_failures["total"] += 1
            _stats_save_failures["last_error"] = str(exc)[:200]
            _stats_save_failures["last_failure_ts"] = time.time()
            consecutive = _stats_save_failures["consecutive"]
        # P23-C: 退避日志 — 1, 3, 10, 30, 100, 300, 1000, ... 才升级 ERROR
        if consecutive < _STATS_FAILURE_ALERT_THRESHOLD:
            logger.warning("intent_tags stats persistence failed (%dx): %s",
                           consecutive, exc)
        elif _should_log_failure(consecutive):
            logger.error("intent_tags stats persistence failing (%dx consecutive): %s",
                         consecutive, exc)
        # else: 静默吞掉，等下一个退避阈值


def _should_log_failure(n: int) -> bool:
    """P23-C: 指数退避 — 仅在 3, 10, 30, 100, 300, 1000, 3000, 10000, ... 时 ERROR。

    序列规则：x, 3*10**k, 1*10**(k+1) for k=0,1,2,...
    起点 3 (=_STATS_FAILURE_ALERT_THRESHOLD)。
    """
    if n < _STATS_FAILURE_ALERT_THRESHOLD:
        return False
    if n == 3:
        return True
    # 提取数字的首字符判断是否为 1 或 3，且后续都是 0
    s = str(n)
    head = s[0]
    return head in ("1", "3") and set(s[1:]) <= {"0"}


def _load_stats_persistent() -> None:
    """P20-C: 启动时从 sidecar 恢复 counter（best-effort）。"""
    try:
        sp = _intent_tags_stats_path()
        if not sp.exists():
            return
        data = json.loads(sp.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return
        with _INTENT_TAGS_STATS_LOCK:
            for k in ("writes", "reloads", "restores"):
                v = data.get(k)
                if isinstance(v, int) and v >= 0:
                    _INTENT_TAGS_EDIT_STATS[k] = v
            lts = data.get("last_edit_ts")
            if isinstance(lts, (int, float)) and lts >= 0:
                _INTENT_TAGS_EDIT_STATS["last_edit_ts"] = float(lts)
            # 恢复滑窗（但要丢弃 > 1h 的过期项）
            ew = data.get("edit_window") or []
            if isinstance(ew, list):
                now = time.time()
                cutoff = now - _EDIT_WINDOW_SEC
                _INTENT_TAGS_EDIT_WINDOW.clear()
                for t in ew:
                    if isinstance(t, (int, float)) and t >= cutoff:
                        _INTENT_TAGS_EDIT_WINDOW.append(float(t))
        logger.info("intent_tags stats restored from %s (writes=%d edits_1h=%d)",
                    sp, _INTENT_TAGS_EDIT_STATS.get("writes", 0),
                    len(_INTENT_TAGS_EDIT_WINDOW))
    except Exception as exc:
        logger.warning("intent_tags stats load failed (using defaults): %s", exc)


# Restore counter state from previous run at module init
_load_stats_persistent()


def reload_intent_tags() -> Dict[str, List[str]]:
    """P14-B: 运营改 yaml 后调用以热更（无需重启进程）。返回当前字典。"""
    global _INTENT_TAGS
    _INTENT_TAGS = _load_intent_tags()
    _bump_stat("reloads")  # P20-A: thread-safe
    return _INTENT_TAGS


def _validate_intent_tags_yaml(content: str) -> Dict[str, Any]:
    """共享校验逻辑（P16-A 写入 / P17-B diff 都用）。

    Returns dict {parsed, category_count, keyword_count, tags_norm}.
    Raises ValueError on any structural issue.
    """
    import yaml as _yaml
    if not isinstance(content, str) or len(content) > 200_000:
        raise ValueError("content must be string under 200KB")
    try:
        parsed = _yaml.safe_load(content) or {}
    except _yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        loc = f" at line {mark.line + 1}" if mark else ""
        raise ValueError(f"yaml parse error{loc}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"top-level must be a mapping, got {type(parsed).__name__}")
    tags_norm: Dict[str, List[str]] = {}
    keyword_count = 0
    for tag, kws in parsed.items():
        if tag in _RESERVED_KEYS or not isinstance(tag, str):
            continue
        if not isinstance(kws, list):
            raise ValueError(f"tag {tag!r}: value must be a list, got {type(kws).__name__}")
        cleaned = [str(k).strip() for k in kws if str(k).strip()]
        if cleaned:
            tags_norm[tag] = cleaned
            keyword_count += len(cleaned)
    if not tags_norm:
        raise ValueError("at least one non-empty tag list is required")
    return {
        "parsed": parsed,
        "tags_norm": tags_norm,
        "category_count": len(tags_norm),
        "keyword_count": keyword_count,
    }


# P17-C: 备份保留份数
_INTENT_TAGS_BACKUP_KEEP = 5


def _rotate_backups(p: Path) -> str:
    """P17-C: 写带时间戳的 .bakYYYYMMDD_HHMMSS_mmm；保留最近 N 份。

    P19-A: 精度从 1s 升到 ms（避免快速连续保存覆盖同秒备份）。
    Returns the new backup path string (empty if source missing).
    """
    if not p.exists():
        return ""
    # P19-A: 3-digit millisecond suffix + collision disambiguation
    now_ns = time.time_ns()
    ts = time.strftime("%Y%m%d_%H%M%S", time.localtime(now_ns / 1e9))
    ms = (now_ns // 1_000_000) % 1000
    bak_new = p.with_suffix(p.suffix + f".bak{ts}_{ms:03d}")
    # Windows clock resolution (~16ms) can cause exact ts+ms collisions on rapid writes;
    # append _N if the path already exists.
    if bak_new.exists():
        for n in range(1, 1000):
            cand = p.with_suffix(p.suffix + f".bak{ts}_{ms:03d}_{n}")
            if not cand.exists():
                bak_new = cand
                break
    try:
        bak_new.write_bytes(p.read_bytes())
    except Exception as exc:
        logger.warning("intent_tags.yaml backup failed: %s", exc)
        return ""
    # 同名旧 .bak（没有时间戳）也兼容 — 改名补时间戳
    plain_bak = p.with_suffix(p.suffix + ".bak")
    if plain_bak.exists():
        try:
            plain_bak.rename(p.with_suffix(p.suffix + ".bak_legacy"))
        except Exception:
            pass
    # 清理旧备份
    backups = sorted(
        [b for b in p.parent.iterdir() if b.name.startswith(p.name + ".bak") and b != bak_new],
        key=lambda x: x.stat().st_mtime,
        reverse=True,
    )
    for old in backups[_INTENT_TAGS_BACKUP_KEEP - 1:]:
        try:
            old.unlink()
        except Exception as exc:
            logger.debug("backup cleanup failed for %s: %s", old, exc)
    return str(bak_new)


def diff_intent_tags(content: str) -> Dict[str, Any]:
    """P17-B: 计算提交内容相对当前运行时字典的差异（保存前预览）。

    Returns dict with:
      - ok: True
      - added_tags / removed_tags: list[str]
      - changed_tags: { tag: {added: [...], removed: [...]} }
      - summary: human-readable counts
    Raises ValueError on parse/structure errors.
    """
    info = _validate_intent_tags_yaml(content)
    new_tags = info["tags_norm"]
    old_tags = _INTENT_TAGS  # 当前运行时
    new_keys = set(new_tags)
    old_keys = set(old_tags)
    added_tags = sorted(new_keys - old_keys)
    removed_tags = sorted(old_keys - new_keys)
    changed: Dict[str, Dict[str, Any]] = {}
    reordered: List[str] = []  # P18-D: tags where overlapping keywords reordered
    for k in sorted(new_keys & old_keys):
        old_list = old_tags[k]
        new_list = new_tags[k]
        old_set = set(old_list)
        new_set = set(new_list)
        a = sorted(new_set - old_set)
        r = sorted(old_set - new_set)
        # P18-D: 相交部分的相对顺序是否变化（优先级 = 字典顺序）
        common = old_set & new_set
        old_seq = [x for x in old_list if x in common]
        new_seq = [x for x in new_list if x in common]
        order_changed = len(common) >= 2 and old_seq != new_seq
        if a or r:
            entry: Dict[str, Any] = {"added": a, "removed": r}
            if order_changed:
                entry["reordered"] = True
            changed[k] = entry
        elif order_changed:
            reordered.append(k)
    total_added = len(added_tags) + sum(len(v["added"]) for v in changed.values())
    total_removed = len(removed_tags) + sum(len(v["removed"]) for v in changed.values())
    summary_parts = [f"+{total_added} kw", f"-{total_removed} kw"]
    summary_parts.append(f"{len(added_tags)} new tags")
    summary_parts.append(f"{len(removed_tags)} removed tags")
    summary_parts.append(f"{len(changed)} changed")
    if reordered:
        summary_parts.append(f"{len(reordered)} reordered")
    return {
        "ok": True,
        "added_tags": added_tags,
        "removed_tags": removed_tags,
        "changed_tags": changed,
        "reordered_tags": reordered,  # P18-D
        "category_count": info["category_count"],
        "keyword_count": info["keyword_count"],
        "summary": " / ".join(summary_parts),
    }


def list_intent_tags_backups() -> List[Dict[str, Any]]:
    """P17-D: 列出可恢复的备份（按 mtime 倒序）。"""
    p = _intent_tags_yaml_path()
    if not p.parent.exists():
        return []
    out: List[Dict[str, Any]] = []
    for f in sorted(
        [b for b in p.parent.iterdir() if b.name.startswith(p.name + ".bak")],
        key=lambda x: x.stat().st_mtime, reverse=True,
    )[:_INTENT_TAGS_BACKUP_KEEP * 2]:  # 最多扫描 keep*2 份
        try:
            stat = f.stat()
            out.append({
                "filename": f.name,
                "size": stat.st_size,
                "mtime": stat.st_mtime,
            })
        except Exception:
            continue
    return out


def _safe_backup_path(filename: str) -> Path:
    """P18-D: 双层防路径穿越（字符过滤 + Path.resolve 二次校验）。

    1. 字符过滤：拒绝 /, \\, .., 空字符串
    2. 前缀校验：必须以目标文件名 + ".bak" 开头
    3. 解析后路径必须在 yaml 同目录下（resolve + is_relative_to）
    """
    if not isinstance(filename, str) or not filename:
        raise ValueError("filename required")
    if "/" in filename or "\\" in filename or ".." in filename or "\x00" in filename:
        raise ValueError("invalid backup filename")
    p = _intent_tags_yaml_path()
    # P19-D: case-insensitive prefix check (normcase handles Windows uppercase)
    if not os.path.normcase(filename).startswith(os.path.normcase(p.name + ".bak")):
        raise ValueError("filename must start with target file basename + '.bak'")
    # P19-D: normalize case (Windows: 'Intent_tags.yaml.bak' == 'intent_tags.yaml.bak')
    parent_resolved_n = os.path.normcase(str(p.parent.resolve()))
    candidate = (p.parent / filename).resolve()
    candidate_n = os.path.normcase(str(candidate))
    # parent containment check via normcased prefix (handles symlinks and case)
    inside = (
        candidate_n == parent_resolved_n
        or candidate_n.startswith(parent_resolved_n + os.sep)
        or (os.altsep is not None and candidate_n.startswith(parent_resolved_n + os.altsep))
    )
    if not inside:
        raise ValueError("resolved path escapes backup directory")
    # P19-D: candidate basename (after symlink resolve) must still match the bak prefix
    # — guards against symlinks pointing to a same-dir file that isn't a real backup.
    target_basename_n = os.path.normcase(candidate.name)
    expected_prefix_n = os.path.normcase(p.name + ".bak")
    if not target_basename_n.startswith(expected_prefix_n):
        raise ValueError("resolved file is not a backup of target")
    return candidate


def read_intent_tags_backup(filename: str) -> str:
    """P18-A: 读取指定备份的原文（供 dry-run 预览复用）。"""
    src = _safe_backup_path(filename)
    if not src.exists() or not src.is_file():
        raise ValueError("backup not found")
    try:
        return src.read_text(encoding="utf-8")
    except Exception as exc:
        raise ValueError(f"backup unreadable: {exc}") from exc


def restore_intent_tags_backup(filename: str) -> Dict[str, Any]:
    """P17-D: 从指定备份恢复（同样走原子写 + 校验 + reload）。

    P18-D: 用 _safe_backup_path 做双层防御（字符过滤 + Path.resolve 二次校验）。
    """
    content = read_intent_tags_backup(filename)
    # 走标准 write 路径（会再做校验、生成新备份）
    result = write_intent_tags_yaml(content)
    _bump_stat("restores")  # P20-A
    _save_stats_persistent()  # P20-C
    return result


def write_intent_tags_yaml(content: str) -> Dict[str, Any]:
    """P16-A: 把运营从 UI 提交的 yaml 文本写入文件 + reload。

    流程：
      1. _validate_intent_tags_yaml() 全量校验（含语法 / 结构）
      2. P17-C: 旋转备份（带时间戳，保留最近 5 份）
      3. 原子写：写 .tmp → os.replace
      4. reload_intent_tags() 立即生效
    """
    info = _validate_intent_tags_yaml(content)
    p = _intent_tags_yaml_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    # P20-A: 串行化文件 IO — 防止并发线程在同一个 .tmp 文件上互踩
    with _INTENT_TAGS_WRITE_LOCK:
        backup_path = _rotate_backups(p)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, p)
        reload_intent_tags()
        # P18-B / P20-A: 编辑统计（线程安全 — _bump_stat/_record_edit 各自加锁）
        now = time.time()
        _bump_stat("writes")
        _set_stat("last_edit_ts", now)
        _record_edit(now)  # P19-C: sliding window
        _save_stats_persistent()  # P20-C: 持久化（best-effort）
    return {
        "ok": True,
        "category_count": info["category_count"],
        "keyword_count": info["keyword_count"],
        "backup_path": backup_path,
    }


def read_intent_tags_yaml() -> str:
    """P16-A: 读取当前 yaml 文件原文（UI 编辑器 textarea 初始值用）。"""
    p = _intent_tags_yaml_path()
    if not p.exists():
        return ""
    try:
        return p.read_text(encoding="utf-8")
    except Exception as exc:
        logger.warning("intent_tags.yaml read failed: %s", exc)
        return ""


def _kw_matches(kw: str, text_lower: str) -> bool:
    """P11-C: 短 ASCII 关键词用 word-boundary，避免 'hi' 匹配 'this'。

    CJK/Thai/越南语等非 ASCII 直接子串匹配（这些语言无空格分词）。
    """
    if kw.isascii() and len(kw) <= 5:
        # 短英文词：要求两侧是非字母/数字（或边界）
        return re.search(r"(?<![A-Za-z0-9])" + re.escape(kw) + r"(?![A-Za-z0-9])", text_lower) is not None
    return kw in text_lower


def compute_intent_tag(text: str) -> str:
    """基于关键词的轻量级意图分类（purchase / support / inquiry / greeting / general）。

    P11-C: 短英文关键词改用 word-boundary 匹配，避免 'hi' / 'buy' 等误判。
    """
    if not text:
        return "general"
    tl = text.lower()
    for tag, kws in _INTENT_TAGS.items():
        if any(_kw_matches(k, tl) for k in kws):
            return tag
    return "general"


def sessions_from_rows(
    rows: List[Any],
    peer_col: str = "peer_text",
    reply_col: str = "reply_text",
    ok_col: str = "ok",
    tag_col: str = "intent_tag",
    ts_col: str = "ts",
    gap_sec: float = 14400,
) -> List[Dict[str, Any]]:
    """通用会话分组：将按 ts ASC 排好的 run 行列表按 4h 间隔划分为会话。

    兼容 dict/sqlite3.Row 两种行格式。
    """
    if not rows:
        return []

    sessions: List[Dict[str, Any]] = []
    cur: Optional[Dict[str, Any]] = None
    prev_ts: Optional[float] = None

    for row in rows:
        ts = float(row[ts_col] or 0)
        peer = str(row[peer_col] or "")
        tag = str(row[tag_col] or "general")
        is_ok = bool(row[ok_col])

        if prev_ts is None or (ts - prev_ts) > gap_sec:
            if cur:
                sessions.append(cur)
            cur = {
                "session_num": len(sessions) + 1,
                "start_ts": ts,
                "end_ts": ts,
                "turn_count": 0,
                "ok_count": 0,
                "intent_counts": {},
                "first_msg": peer[:120],
                "last_msg": peer[:120],
                "first_reply": str(row[reply_col] or "")[:120],
            }
        cur["end_ts"] = ts  # type: ignore[index]
        cur["turn_count"] += 1  # type: ignore[operator]
        if is_ok:
            cur["ok_count"] += 1  # type: ignore[operator]
        cur["intent_counts"][tag] = cur["intent_counts"].get(tag, 0) + 1  # type: ignore
        cur["last_msg"] = peer[:120]
        prev_ts = ts

    if cur:
        sessions.append(cur)

    for s in sessions:
        counts = s.pop("intent_counts", {})
        s["dominant_intent"] = max(counts, key=counts.get) if counts else "general"
        s["intent_distribution"] = counts

    return list(reversed(sessions))


def extract_chat_name(chat_key: str) -> str:
    """P12-A: 从 chat_key 提取显示名（用于跨平台身份匹配）。

    各平台格式：
      WA:        wa:{account_id}:{name}            → name
      Messenger: messenger_rpa:{name} / acc_X:{name} → name
      LINE RPA:  {topbar_text} 或 line_rpa:default  → 最后一段
      LINE webhook: line:user:{uid}                → 无人类可读名，返回空
    规则：取最后一个 `:` 之后的部分；若纯 ID/UUID/数字 → 视为无名
    """
    if not chat_key:
        return ""
    s = chat_key.rsplit(":", 1)[-1].strip()
    if not s:
        return ""
    # 全数字/UUID/LINE user-id（U + 32hex） → 视为无人类可读名
    if s.isdigit() or (len(s) >= 16 and all(c in "0123456789abcdefABCDEFUu-_" for c in s)):
        return ""
    # 通用占位/无意义名
    if s.lower() in {"default", "unknown", "anonymous", ""}:
        return ""
    return s


def count_runs_for_chat_name(
    conn: Any,
    table: str,
    name: str,
    *,
    peer_col: str = "peer_text",
    chat_key_col: str = "chat_key",
    ts_col: str = "ts",
) -> Dict[str, Any]:
    """P12-A: 统计某 chat_name 的轮次/最近时间（用 chat_key 后缀匹配）。

    返回 {total_turns, last_ts, sample_chat_key}。
    name 为空时返回零值；表名/列名做白名单校验。
    """
    out: Dict[str, Any] = {"total_turns": 0, "last_ts": 0.0, "sample_chat_key": ""}
    if not name:
        return out
    for ident in (table, peer_col, chat_key_col, ts_col):
        if not ident.replace("_", "").isalnum():
            raise ValueError(f"invalid identifier: {ident}")
    # 后缀匹配：chat_key LIKE %:name 或 = name
    pat1 = f"%:{name}"
    row = conn.execute(
        f"SELECT COUNT(*) as n, MAX({ts_col}) as last_ts, MAX({chat_key_col}) as ck"
        f" FROM {table} WHERE {peer_col}!='' AND ({chat_key_col} LIKE ? OR {chat_key_col}=?)",
        (pat1, name),
    ).fetchone()
    if not row:
        return out
    out["total_turns"] = int(row["n"] or 0)
    out["last_ts"] = float(row["last_ts"] or 0)
    out["sample_chat_key"] = str(row["ck"] or "")
    return out


def compute_intent_stats(
    conn: Any,
    table: str,
    *,
    window_hours: float = 168.0,
    ts_col: str = "ts",
    peer_col: str = "peer_text",
    tag_col: str = "intent_tag",
) -> Dict[str, Any]:
    """P10-C: 跨平台意图分布统计。

    给定一个 sqlite 连接 + runs 表名，返回 {window_hours, total_turns, distribution}。
    LINE/Messenger/WhatsApp 的 runs 表 schema 在 ts/peer_text/intent_tag 三列上一致，可共用。
    白名单 table/column 名，防 SQL 注入。
    """
    # 表名/列名白名单（防注入：仅允许 ASCII 字母/数字/下划线）
    for ident in (table, ts_col, peer_col, tag_col):
        if not ident.replace("_", "").isalnum():
            raise ValueError(f"invalid identifier: {ident}")
    since = time.time() - max(0.0, float(window_hours)) * 3600.0
    rows = conn.execute(
        f"SELECT COALESCE({tag_col},'general') as tag, COUNT(*) as cnt"
        f" FROM {table} WHERE {ts_col} >= ? AND {peer_col}!=''"
        f" GROUP BY tag ORDER BY cnt DESC",
        (since,),
    ).fetchall()
    total_row = conn.execute(
        f"SELECT COUNT(*) as n FROM {table} WHERE {ts_col} >= ? AND {peer_col}!=''",
        (since,),
    ).fetchone()
    total = int((total_row["n"] if total_row else 0) or 0)
    return {
        "window_hours": float(window_hours),
        "total_turns": total,
        "distribution": {r["tag"]: int(r["cnt"]) for r in rows},
    }
