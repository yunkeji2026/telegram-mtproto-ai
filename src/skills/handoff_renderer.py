"""HandoffRenderer — 从 yaml 话术池里选一条并渲染最终文本。

设计原则：
  1. 选择和渲染分开：`pick()` 决定用哪条，`render()` 做字符串替换
  2. slot 必须保留：如果 AI 改写层将来介入，render 后要能检测 slot 存在
  3. 语言/时机/人设风格三维过滤 + 排除最近用过的模板防重复
  4. yaml 热重载：文件 mtime 变了自动 reload
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore

logger = logging.getLogger(__name__)


# 上下文常量
CONTEXT_GOODBYE = "goodbye"
CONTEXT_IDENTITY_ASKED = "identity_asked"
CONTEXT_ANY = "any"


@dataclass
class HandoffScript:
    id: str
    language: str
    triggers: List[str]
    shell: Dict[str, str]
    persona_tone: str = ""
    # yaml 里没有但可扩展
    extra: Dict[str, Any] = field(default_factory=dict)

    def assemble_text(self) -> str:
        """把 shell 的三段拼成一条消息（保留 slot）。"""
        parts = [
            (self.shell.get("greeting") or "").strip(),
            (self.shell.get("reason") or "").strip(),
            (self.shell.get("cta") or "").strip(),
        ]
        return "，".join(p for p in parts if p) if self.language == "zh" else " ".join(p for p in parts if p)


@dataclass
class RenderedHandoff:
    script_id: str
    language: str
    text: str
    slots_used: Dict[str, str]
    warning: str = ""


class HandoffRendererError(Exception):
    pass


class HandoffRenderer:
    """话术池的加载/选择/渲染。"""

    def __init__(self, scripts_path: Path) -> None:
        self._scripts_path = Path(scripts_path)
        self._lock = threading.Lock()
        self._scripts: List[HandoffScript] = []
        self._mtime: float = 0.0
        self._load()

    # ── 加载（首次 + 热重载） ──────────────────────────
    def _load(self) -> None:
        if yaml is None:
            raise HandoffRendererError("PyYAML not installed")
        if not self._scripts_path.exists():
            raise HandoffRendererError(f"scripts file not found: {self._scripts_path}")
        data = yaml.safe_load(self._scripts_path.read_text(encoding="utf-8")) or {}
        raw_scripts = data.get("scripts") or []
        parsed: List[HandoffScript] = []
        for r in raw_scripts:
            if not isinstance(r, dict):
                continue
            sid = str(r.get("id") or "").strip()
            lang = str(r.get("language") or "").strip()
            triggers = list(r.get("triggers") or [])
            shell = r.get("shell") or {}
            if not sid or not lang or not triggers or not isinstance(shell, dict):
                logger.warning("handoff_scripts: skipping malformed entry %r", r)
                continue
            parsed.append(HandoffScript(
                id=sid,
                language=lang,
                triggers=[str(t) for t in triggers],
                shell={str(k): str(v) for k, v in shell.items()},
                persona_tone=str(r.get("persona_tone") or ""),
            ))
        if not parsed:
            raise HandoffRendererError(
                f"no valid scripts in {self._scripts_path}")
        with self._lock:
            self._scripts = parsed
            try:
                self._mtime = self._scripts_path.stat().st_mtime
            except OSError:
                self._mtime = time.time()
        logger.info("handoff_scripts loaded: %d scripts", len(parsed))

    def maybe_reload(self) -> bool:
        """mtime 变了就重载。返回 True 表示实际重载。"""
        try:
            new_mtime = self._scripts_path.stat().st_mtime
        except OSError:
            return False
        if new_mtime > self._mtime:
            try:
                self._load()
                return True
            except Exception as e:
                logger.warning("handoff_scripts hot-reload failed: %s", e)
                return False
        return False

    # ── 选择 ───────────────────────────────────────────
    def pick(
        self,
        *,
        language: str,
        context: str = CONTEXT_ANY,
        tone: str = "",
        exclude_ids: Optional[Iterable[str]] = None,
    ) -> Optional[HandoffScript]:
        """挑一条话术。过滤优先级：
          1. 语言必须匹配
          2. triggers 包含 context 或 CONTEXT_ANY
          3. tone 若指定必须匹配
          4. 排除 exclude_ids
        剩下的里随机选一条（secrets.choice）。
        """
        with self._lock:
            candidates = list(self._scripts)
        excl: Set[str] = set(exclude_ids or [])
        language = (language or "").strip().lower()
        tone = (tone or "").strip().lower()

        def _match(s: HandoffScript) -> bool:
            if s.id in excl:
                return False
            if s.language.lower() != language:
                return False
            triggers_lower = {t.lower() for t in s.triggers}
            if context.lower() not in triggers_lower and CONTEXT_ANY not in triggers_lower:
                return False
            if tone and s.persona_tone.lower() != tone:
                return False
            return True

        pool = [s for s in candidates if _match(s)]
        # 放宽：若 tone 过滤后空，丢弃 tone 再试
        if not pool and tone:
            pool = [s for s in candidates
                    if s.id not in excl
                    and s.language.lower() == language
                    and (context.lower() in {t.lower() for t in s.triggers}
                         or CONTEXT_ANY in {t.lower() for t in s.triggers})]
        # 再放宽：没 context 命中就 fallback 到 any
        if not pool and context != CONTEXT_ANY:
            return self.pick(language=language, context=CONTEXT_ANY,
                              exclude_ids=exclude_ids)
        if not pool:
            return None
        return secrets.choice(pool)

    # ── 渲染 ───────────────────────────────────────────
    @staticmethod
    def render(
        script: HandoffScript,
        *,
        line_id: str,
        token: str,
        persona_name: str = "",
    ) -> RenderedHandoff:
        """把 {LINE_ID} {TOKEN} {PERSONA_NAME} 填进去。不含 slot 的话术会输出警告。"""
        raw = script.assemble_text()
        slots_used: Dict[str, str] = {}
        text = raw
        for marker, val in [
            ("{LINE_ID}", line_id),
            ("{TOKEN}", token),
            ("{PERSONA_NAME}", persona_name),
        ]:
            if marker in text:
                text = text.replace(marker, val or "")
                slots_used[marker] = val or ""
        warning = ""
        # 必须嵌入 LINE_ID 和 TOKEN——否则话术无法完成合并
        if line_id and "{LINE_ID}" not in raw:
            warning = "script_missing_LINE_ID_slot"
        elif token and "{TOKEN}" not in raw:
            warning = "script_missing_TOKEN_slot"
        return RenderedHandoff(
            script_id=script.id,
            language=script.language,
            text=text,
            slots_used=slots_used,
            warning=warning,
        )

    # ── 便利查询 ──────────────────────────────────────
    def count(self) -> int:
        with self._lock:
            return len(self._scripts)

    def list_ids(self) -> List[str]:
        with self._lock:
            return [s.id for s in self._scripts]

    def by_id(self, sid: str) -> Optional[HandoffScript]:
        with self._lock:
            for s in self._scripts:
                if s.id == sid:
                    return s
        return None
