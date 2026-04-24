"""失败留痕：封装"按步命中 → 截图 → 写盘 → FIFO 清理"的纯逻辑。

从 Runner 中抽出来，保持 Runner 的 orchestrator 职责清晰。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional

logger = logging.getLogger(__name__)


_DEFAULT_STEPS: List[str] = [
    "open_fail",
    "no_peer_text",
    "skill_error",
    "send_failed",
    "no_xml_in_room",
]


@dataclass
class FailureShotsConfig:
    enabled: bool = False
    dir: str = "logs/line_rpa/failures"
    max_files: int = 200
    on_steps: List[str] = field(default_factory=lambda: list(_DEFAULT_STEPS))

    @classmethod
    def from_dict(cls, raw: Any) -> "FailureShotsConfig":
        if not isinstance(raw, dict):
            return cls()
        steps_raw = raw.get("on_steps")
        if isinstance(steps_raw, list) and steps_raw:
            steps = [str(s) for s in steps_raw if str(s).strip()]
        else:
            steps = list(_DEFAULT_STEPS)
        return cls(
            enabled=bool(raw.get("enabled", False)),
            dir=str(raw.get("dir") or "logs/line_rpa/failures"),
            max_files=max(10, int(raw.get("max_files", 200) or 200)),
            on_steps=steps,
        )


def _safe_chat_slug(chat_key: str) -> str:
    slug = "".join(
        c if (c.isalnum() or c in ("_", "-")) else "_"
        for c in (chat_key or "anon")[:24]
    )
    return slug or "anon"


def save_failure_shot(
    *,
    cfg: FailureShotsConfig,
    step: str,
    chat_key: str,
    png: Optional[bytes],
) -> Optional[str]:
    """把 PNG 以 `<ts_ms>_<step>_<chat>.png` 写盘；返回纯文件名（不含目录）。

    - 只在 `cfg.enabled=True` 且 `step in cfg.on_steps` 时写盘
    - 自动 FIFO 清理最旧文件到 `cfg.max_files`
    - 返回 None 表示跳过 / 未写盘 / 写失败
    """
    if not cfg.enabled:
        return None
    if step not in cfg.on_steps:
        return None
    if not png:
        return None

    try:
        shots_dir = Path(cfg.dir)
        shots_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:  # noqa: BLE001
        logger.debug("创建失败截图目录失败: %s", e)
        return None

    fname = f"{int(time.time() * 1000)}_{step}_{_safe_chat_slug(chat_key)}.png"
    try:
        (shots_dir / fname).write_bytes(png)
    except Exception as e:  # noqa: BLE001
        logger.debug("写失败截图失败: %s", e)
        return None

    try:
        files = sorted(shots_dir.glob("*.png"), key=lambda p: p.stat().st_mtime)
        if len(files) > cfg.max_files:
            for f in files[: len(files) - cfg.max_files]:
                try:
                    f.unlink()
                except Exception:
                    pass
    except Exception:
        pass

    return fname
