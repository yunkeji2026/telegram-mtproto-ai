"""P59：术语库可编辑覆盖层（供后台控制台增删改）。

全局 config + 域包 terminology.yaml 是「基线」（工程改配置），本模块提供一个
**可在运行时编辑的覆盖层**（`config/glossary_overrides.yaml`），优先级最高，
让主管无需改代码/配置即可增删术语与品牌保护词，并即时生效。

文件结构：
    terms:
      size: 尺码
    protect:
      - LINE

线程安全；每次写入留一个 ``.bak`` 备份。隐私无关（只是术语字典）。
"""

from __future__ import annotations

import shutil
import threading
from pathlib import Path
from typing import Any, Dict, List


class GlossaryStore:
    def __init__(self, path: Any) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    def load(self) -> Dict[str, Any]:
        """返回 {"terms": {..}, "protect": [..]}（缺失/损坏 → 空结构）。"""
        with self._lock:
            if not self.path.exists():
                return {"terms": {}, "protect": []}
            try:
                import yaml
                with self.path.open("r", encoding="utf-8") as fh:
                    data = yaml.safe_load(fh) or {}
            except Exception:
                return {"terms": {}, "protect": []}
            terms = data.get("terms") if isinstance(data.get("terms"), dict) else {}
            protect = data.get("protect") if isinstance(data.get("protect"), list) else []
            clean_terms = {str(k): str(v) for k, v in terms.items() if k and isinstance(v, (str, int, float))}
            clean_protect: List[str] = []
            for t in protect:
                s = str(t).strip()
                if s and s not in clean_protect:
                    clean_protect.append(s)
            return {"terms": clean_terms, "protect": clean_protect}

    def _save(self, data: Dict[str, Any]) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists():
                try:
                    shutil.copy2(self.path, self.path.with_suffix(self.path.suffix + ".bak"))
                except Exception:
                    pass
            import yaml
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                yaml.safe_dump(data, fh, allow_unicode=True, sort_keys=True)
            tmp.replace(self.path)

    def upsert_term(self, term: str, translation: str) -> Dict[str, Any]:
        term = str(term or "").strip()
        translation = str(translation or "").strip()
        if not term or not translation:
            raise ValueError("term 与 translation 均不能为空")
        with self._lock:
            data = self.load()
            data["terms"][term] = translation
            self._save(data)
            return data

    def remove_term(self, term: str) -> Dict[str, Any]:
        term = str(term or "").strip()
        with self._lock:
            data = self.load()
            data["terms"].pop(term, None)
            self._save(data)
            return data

    def add_protect(self, word: str) -> Dict[str, Any]:
        word = str(word or "").strip()
        if not word:
            raise ValueError("protect 词不能为空")
        with self._lock:
            data = self.load()
            if word not in data["protect"]:
                data["protect"].append(word)
            self._save(data)
            return data

    def remove_protect(self, word: str) -> Dict[str, Any]:
        word = str(word or "").strip()
        with self._lock:
            data = self.load()
            data["protect"] = [w for w in data["protect"] if w != word]
            self._save(data)
            return data


__all__ = ["GlossaryStore"]
