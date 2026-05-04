"""W2-D2.4：自然节奏引擎（pacing）。

陪护产品的差异化体验之一：AI 不秒回。
- 短回应（"嗯""好"）：5-15 秒
- 中等回复（30-60 字）：15-45 秒
- 长回复（100 字+）：30-90 秒
- 长篇情绪倾诉响应：60-180 秒（让对方有"被认真听"的感觉）

设计成纯函数（无副作用，无 IO），方便单测。
runner 调 calc_pacing_delay → 拿到 PacingResult →
  - delay_sec <= short_send_threshold（默认 3s）：直接 await
  - delay_sec > short_send_threshold：enqueue_deferred(reason="pacing:")，
    由独立 drain loop 异步发
"""
from __future__ import annotations

import datetime as _dt
import random
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class PacingResult:
    delay_sec: float          # 建议延迟（>=0）
    reason: str               # 决策理由（log 用）
    typing_indicator: bool    # 等待期内是否值得显示"在输入"
    # delay 分解（debug）
    base_sec: float = 0.0
    peer_factor: float = 1.0
    stage_factor: float = 1.0
    hour_factor: float = 1.0   # W2-D3.3
    jitter: float = 1.0


# ── 默认配置（可被 messenger_rpa.pacing 覆盖）────────────────
_DEFAULTS = {
    "enabled": True,
    "min_sec": 3.0,                       # 最低不秒回
    "max_sec": 180.0,                     # 单条不超过 3 分钟
    "thinking_base_sec": 4.0,             # AI"看到消息→开始打字"基础时间
    "per_char_typing_sec": 0.06,          # 每字打字时间（约 1000 字/分钟）
    "long_msg_threshold_chars": 80,       # peer 超过此字数视为长篇
    "long_msg_extra_sec": 25.0,           # 长篇倾诉额外"消化时间"
    "very_long_threshold_chars": 200,     # 超长篇
    "very_long_extra_sec": 60.0,          # 超长篇额外秒数
    "stage_multiplier": {
        "initial": 1.3,                   # 不熟悉 → 更慢，不显得太热情
        "warming": 1.0,
        "intimate": 0.75,                 # 熟悉 → 更快回
        "steady": 0.85,
    },
    # ★ W2-D3.3：hour_of_day 因子（设备本地时间）
    # 7-9 早上慢一点（"刚醒"）；19-22 高互动快一点；22-1 晚上"快睡"慢一点
    # 周末整体节奏放慢 1.15x（在度假感）
    "hour_multiplier": {
        # 给个默认每小时倍率；未列出的小时 = 1.0
        7: 1.4, 8: 1.3, 9: 1.15,         # 早上"刚醒"
        19: 0.85, 20: 0.85, 21: 0.85,    # 黄金互动时段
        22: 1.1, 23: 1.2, 0: 1.3,        # 晚上"快睡"
        # 1-6 点 → 走 quiet_hours gate，不进 pacing
    },
    "weekend_multiplier": 1.15,           # 周六日整体放慢
    "jitter_range": (0.7, 1.3),           # 随机抖动 ±30%
    "typing_indicator_min_sec": 8.0,      # 延迟 >= 此值显示"在输入"
    "short_send_threshold_sec": 3.0,      # 低于此延迟直接 await，不走 defer
}


def _cfg(config: Optional[dict], key: str):
    if config and key in config:
        return config[key]
    return _DEFAULTS[key]


def calc_pacing_delay(
    *,
    reply_text: str,
    peer_text: str,
    now_dt: Optional[_dt.datetime] = None,
    relationship_stage: str = "warming",
    config: Optional[dict] = None,
) -> PacingResult:
    """计算 AI 回复的自然节奏延迟。

    参数
    ------
    reply_text : 即将发出的 AI 回复
    peer_text : 触发本次回复的对方原消息
    now_dt : 当前时间（None = 现在）；保留参数便于测试
    relationship_stage : initial/warming/intimate/steady（来自 companion_relationship）
    config : 覆盖默认参数（messenger_rpa.pacing 段）
    """
    if not bool(_cfg(config, "enabled")):
        return PacingResult(0.0, "disabled", False)

    reply_len = len(reply_text or "")
    peer_len = len(peer_text or "")

    # 1) 基础打字时间
    base = (
        float(_cfg(config, "thinking_base_sec"))
        + reply_len * float(_cfg(config, "per_char_typing_sec"))
    )

    # 2) peer 长度因子（对方说越多 AI"消化"越久）
    peer_factor = 1.0
    long_th = int(_cfg(config, "long_msg_threshold_chars"))
    very_long_th = int(_cfg(config, "very_long_threshold_chars"))
    extra = 0.0
    if peer_len >= very_long_th:
        extra = float(_cfg(config, "very_long_extra_sec"))
        peer_factor = 1.5
    elif peer_len >= long_th:
        extra = float(_cfg(config, "long_msg_extra_sec"))
        peer_factor = 1.2

    # 3) 关系阶段因子
    stage_map = _cfg(config, "stage_multiplier") or {}
    stage_factor = float(stage_map.get(relationship_stage, 1.0))

    # ★ 4) hour_of_day + 周末 因子
    if now_dt is None:
        now_dt = _dt.datetime.now()
    hour_map = _cfg(config, "hour_multiplier") or {}
    # hour_map 可能是 dict[int|str, float]，统一成 int key
    hour_factor = 1.0
    try:
        h = int(now_dt.hour)
        if h in hour_map:
            hour_factor = float(hour_map[h])
        elif str(h) in hour_map:
            hour_factor = float(hour_map[str(h)])
    except Exception:
        hour_factor = 1.0
    weekend_mul = float(_cfg(config, "weekend_multiplier") or 1.0)
    if now_dt.weekday() >= 5:  # Sat=5 Sun=6
        hour_factor *= weekend_mul

    # 5) 随机抖动（人不会精确）
    jr = _cfg(config, "jitter_range") or (0.7, 1.3)
    jitter = random.uniform(float(jr[0]), float(jr[1]))

    # 合成
    delay = (base + extra) * stage_factor * hour_factor * jitter

    # 限幅
    min_sec = float(_cfg(config, "min_sec"))
    max_sec = float(_cfg(config, "max_sec"))
    delay = max(min_sec, min(max_sec, delay))

    typing_min = float(_cfg(config, "typing_indicator_min_sec"))
    typing_indicator = delay >= typing_min

    reason = (
        f"len={reply_len} peer={peer_len} "
        f"base={base:.1f} extra={extra:.1f} "
        f"stage={relationship_stage}({stage_factor:.2f}) "
        f"hour({now_dt.hour})={hour_factor:.2f} "
        f"jitter={jitter:.2f} → {delay:.1f}s"
    )
    return PacingResult(
        delay_sec=delay,
        reason=reason,
        typing_indicator=typing_indicator,
        base_sec=base,
        peer_factor=peer_factor,
        stage_factor=stage_factor,
        hour_factor=hour_factor,
        jitter=jitter,
    )


def short_send_threshold_sec(config: Optional[dict] = None) -> float:
    """暴露给 runner 用的阈值：小于此值直接 await，否则走 defer。"""
    return float(_cfg(config, "short_send_threshold_sec"))
