"""HandoffCompliance — 引流话术内容合规巡检。

三层：
  1. blocked_keywords  → 直接拒发
  2. warn_keywords     → 允许但打标（运营后续看审计）
  3. 长度检查           → 过长或过短拒发

注：与"法律合规/国家策略"无关（按用户要求已从设计中删除）；这里是
**业务层面**的话术内容自律——情感陪聊引流别带金融/交易/客服味。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore

logger = logging.getLogger(__name__)


@dataclass
class ComplianceResult:
    allowed: bool
    blocked_hits: List[str] = field(default_factory=list)
    warn_hits: List[str] = field(default_factory=list)
    length_issue: str = ""          # ""/"too_short"/"too_long"
    reason: str = ""                # 便于日志/审计

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "blocked_hits": self.blocked_hits,
            "warn_hits": self.warn_hits,
            "length_issue": self.length_issue,
            "reason": self.reason,
        }


class HandoffComplianceChecker:
    def __init__(
        self,
        *,
        config_path: Optional[Path] = None,
        blocked_keywords: Optional[List[str]] = None,
        warn_keywords: Optional[List[str]] = None,
        max_length: int = 240,
        min_length: int = 10,
    ) -> None:
        if config_path is not None:
            data = self._load_yaml(Path(config_path))
            self._blocked = _norm_kw_list(data.get("blocked_keywords") or [])
            self._warn = _norm_kw_list(data.get("warn_keywords") or [])
            self._max_length = int(data.get("max_length_chars") or max_length)
            self._min_length = int(data.get("min_length_chars") or min_length)
        else:
            self._blocked = _norm_kw_list(blocked_keywords or [])
            self._warn = _norm_kw_list(warn_keywords or [])
            self._max_length = max_length
            self._min_length = min_length

    @staticmethod
    def _load_yaml(path: Path) -> Dict[str, Any]:
        if yaml is None:
            raise RuntimeError("PyYAML not installed")
        if not path.exists():
            raise FileNotFoundError(path)
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    # ── 主判定 ────────────────────────────────────────
    def check(self, text: str) -> ComplianceResult:
        t = (text or "").strip()
        if not t:
            return ComplianceResult(allowed=False, length_issue="too_short",
                                     reason="empty_text")
        # 长度
        length_issue = ""
        if len(t) < self._min_length:
            length_issue = "too_short"
        elif len(t) > self._max_length:
            length_issue = "too_long"

        lower = t.lower()
        blocked_hits = [kw for kw in self._blocked if kw in lower]
        warn_hits = [kw for kw in self._warn if kw in lower]

        if blocked_hits or length_issue in ("too_short", "too_long"):
            return ComplianceResult(
                allowed=False,
                blocked_hits=blocked_hits,
                warn_hits=warn_hits,
                length_issue=length_issue,
                reason=("length_" + length_issue) if length_issue else "blocked_keyword_hit",
            )
        return ComplianceResult(
            allowed=True,
            warn_hits=warn_hits,
            reason="ok" if not warn_hits else "passed_with_warnings",
        )


def _norm_kw_list(kws: List[str]) -> List[str]:
    out: List[str] = []
    for k in kws:
        s = str(k or "").strip().lower()
        if s:
            out.append(s)
    return out
