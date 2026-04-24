"""
按 config 目录下 knowledge_base.db 路径缓存单例，供 SkillManager 与各 Skill 共用，
避免重复打开 SQLite 与重复初始化；首次创建时执行 seed_system_replies。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

_logger = logging.getLogger(__name__)

_STORES: Dict[str, Any] = {}


def reset_kb_singleton_cache() -> None:
    """清空进程内 KB 单例缓存（在直接操作 SQLite 或 purge_all_data 后调用，避免旧索引）。"""
    _STORES.clear()


def _db_path(config) -> Optional[Path]:
    if not hasattr(config, "config_path"):
        return None
    return Path(config.config_path).parent / "knowledge_base.db"


def get_kb_store(config, *, require_exists: bool) -> Optional[Any]:
    """
    require_exists=True：仅当 knowledge_base.db 已存在时打开（与历史 Skill._get_kb_store 一致）。
    require_exists=False：若无文件则创建（与历史 SkillManager._get_kb_store / 兜底一致）。
    同一 resolved 路径全局只初始化一次，并只做一次 seed_system_replies。
    """
    from src.utils.kb_store import KnowledgeBaseStore, seed_system_replies

    p = _db_path(config)
    if not p:
        return None
    if require_exists and not p.exists():
        return None

    key = str(p.resolve())
    if key in _STORES:
        return _STORES[key]

    try:
        kb = KnowledgeBaseStore(p)
        r = seed_system_replies(kb)
        if isinstance(r, dict) and r.get("added", 0) > 0:
            _logger.info("KB 系统话术种子数据已迁移: %s", r)
        _STORES[key] = kb
        return kb
    except Exception as e:
        _logger.warning("KB 初始化失败: %s", e)
        return None
