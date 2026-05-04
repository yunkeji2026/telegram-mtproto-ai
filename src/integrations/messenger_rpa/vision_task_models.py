"""Vision 任务的模型/超时/解释——中央显式表。

**为什么需要这个**：

P0/P1 给 ``title_verify`` 加 vision 兜底时实测 flash 准确（5s），就把"读
顶栏文字 → flash"作为经验。P3 加 ``input_verify`` 时延续这个经验默认
flash——结果实测 **5/5 次 false negative**（明明输入框有"测试一下"却返
空）。换 plus 立刻准确。

教训：**不同 vision 任务的最佳模型不同**。flash 适合"单字段、清晰、大字
体"，plus 才能"在多 UI 元素里定位 + OCR"。这个经验靠口口相传会丢，沉淀
进显式表才不会让下个写 vision 模块的人再踩同坑。

接入方式::

    from src.integrations.messenger_rpa.vision_task_models import cfg_for_task

    def read_xxx_via_vision(..., task_name: str = "xxx"):
        title_cfg = cfg_for_task(task_name, base_cfg=vision_cfg)
        vc = VisionClient(title_cfg)
        ...

新增任务：在 ``VISION_TASKS`` 加一行 + 写 ``notes`` 说为啥选这个模型。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VisionTaskConfig:
    """单个 vision 任务的推荐配置。

    ``model`` / ``timeout`` 是关键字段；``notes`` 必填——用来把实测数据
    沉淀进代码（"为啥选这个模型？"），下次有人改也有据可查。
    """
    model: str
    timeout: float = 30.0
    max_tokens: int = 1024
    notes: str = ""


# ── 显式任务表（按字典序排列）──────────────────────────────
VISION_TASKS: Dict[str, VisionTaskConfig] = {
    "inbox_combined": VisionTaskConfig(
        model="glm-4v-plus",
        timeout=30.0,
        max_tokens=2048,
        notes=(
            "Inbox 行扫描——多元素 UI（头像、未读小红点、preview、时间），"
            "flash 空间感不够。plus 是默认（runner 直接用全局 vision_cfg）。"
        ),
    ),
    "input_verify": VisionTaskConfig(
        model="glm-4v-plus",
        timeout=30.0,
        notes=(
            "P3 实测（2026-04-28, 720x480 input strip, 中文样本）："
            "flash 5/5 次 false negative（明明有'测试一下'返空 + 5-30s "
            "首次冷调甚至 60s+）。plus 100% 准确，5-13s。"
            "本任务必须 plus。"
        ),
    ),
    "peer_image": VisionTaskConfig(
        model="glm-4v-plus",
        timeout=30.0,
        max_tokens=2048,
        notes="对方发的图——可能含 OCR / 场景理解需求，plus 必备。",
    ),
    "peer_typing": VisionTaskConfig(
        model="glm-4v-flash",
        timeout=15.0,
        notes=(
            "检测对方'正在输入'气泡——单一固定 UI 元素 + 二值判断，"
            "flash 够用且更快。"
        ),
    ),
    "title_verify": VisionTaskConfig(
        model="glm-4v-flash",
        timeout=15.0,
        notes=(
            "P0+P1 实测（2026-04, 720x208 top strip, 中英日文样本）："
            "flash 准确读出 peer name + 4-7s。plus 9-10s 但精度无提升。"
            "本任务 flash 即可。"
        ),
    ),
}


def cfg_for_task(
    task_name: str,
    *,
    base_cfg: Optional[Dict[str, Any]] = None,
    overrides: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """生成可传给 ``VisionClient(...)`` 的 cfg dict。

    优先级：``overrides`` > 任务表 > ``base_cfg``。

    - ``base_cfg``：调用方持有的 vision 配置（含 api_key / provider 等）
    - 任务表：覆盖 model / timeout / max_tokens
    - ``overrides``：临时覆盖（极少用，主要给 tests）

    若 ``base_cfg.provider`` 不是 zhipu（如 ollama），任务表的 model 不会
    硬覆盖——避免"任务表说 glm-4v-plus，但你跑 ollama"的悖论。
    """
    cfg: Dict[str, Any] = dict(base_cfg or {})
    task = VISION_TASKS.get(task_name)
    if task is None:
        logger.debug(
            "[vision_task_models] 未知 task_name=%r 沿用 base_cfg。可在"
            " VISION_TASKS 显式登记。", task_name,
        )
        if overrides:
            cfg.update(overrides)
        return cfg

    provider = (cfg.get("provider") or "zhipu").strip().lower()
    if provider == "zhipu":
        cfg["model"] = task.model
        # timeout/max_tokens 也带上——某些任务（plus + 大图）需要更长 timeout
        cfg["timeout"] = task.timeout
        cfg["max_tokens"] = task.max_tokens

    if overrides:
        cfg.update(overrides)
    return cfg


def list_tasks() -> Dict[str, VisionTaskConfig]:
    """诊断用——一键列出所有登记任务及其 notes。"""
    return dict(VISION_TASKS)


__all__ = [
    "VisionTaskConfig",
    "VISION_TASKS",
    "cfg_for_task",
    "list_tasks",
]
