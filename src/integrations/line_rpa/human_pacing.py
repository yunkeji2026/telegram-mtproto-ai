"""LINE RPA 拟人节奏：读停顿、消息分条、字符抖动、分条间隔。

所有参数均可通过 config/line_rpa.human_pacing 配置：

  line_rpa:
    human_pacing:
      enabled: true
      read_pause_ms: [800, 2000]       # 看完对方消息后的"思考"停顿
      per_char_ms:   [40, 80]          # 拟人打字间隔（仅在 slow_type 模式下生效）
      slow_type:     false             # true 则逐字输入；false 则整段输入
      split_mode:    sentence          # none | sentence | length
      split_max_chars: 80              # length 模式阈值
      split_max_parts: 3
      inter_msg_ms:  [700, 1800]       # 分条之间的间隔
"""

from __future__ import annotations

import logging
import random
import re
from dataclasses import dataclass
from typing import List, Tuple

logger = logging.getLogger(__name__)

_SENT_SPLIT_RE = re.compile(
    r"(?<=[。！？!?\.\n])\s+|(?<=[。！？!?])(?=[^\s])",
    flags=re.UNICODE,
)


@dataclass
class PacingConfig:
    enabled: bool = True
    read_pause_ms_lo: int = 800
    read_pause_ms_hi: int = 2000
    per_char_ms_lo: int = 40
    per_char_ms_hi: int = 80
    slow_type: bool = False
    split_mode: str = "sentence"  # none | sentence | length
    split_max_chars: int = 80
    split_max_parts: int = 3
    inter_msg_ms_lo: int = 700
    inter_msg_ms_hi: int = 1800

    @classmethod
    def from_dict(cls, cfg: dict | None) -> "PacingConfig":
        c = cfg or {}
        if not isinstance(c, dict):
            return cls()

        def _pair(key: str, default_lo: int, default_hi: int) -> Tuple[int, int]:
            v = c.get(key)
            if isinstance(v, (list, tuple)) and len(v) >= 2:
                try:
                    lo = int(v[0])
                    hi = int(v[1])
                    if lo > hi:
                        lo, hi = hi, lo
                    return max(0, lo), max(0, hi)
                except (TypeError, ValueError):
                    pass
            if isinstance(v, (int, float)):
                return int(v), int(v)
            return default_lo, default_hi

        rp_lo, rp_hi = _pair("read_pause_ms", 800, 2000)
        pc_lo, pc_hi = _pair("per_char_ms", 40, 80)
        im_lo, im_hi = _pair("inter_msg_ms", 700, 1800)
        return cls(
            enabled=bool(c.get("enabled", True)),
            read_pause_ms_lo=rp_lo,
            read_pause_ms_hi=rp_hi,
            per_char_ms_lo=pc_lo,
            per_char_ms_hi=pc_hi,
            slow_type=bool(c.get("slow_type", False)),
            split_mode=str(c.get("split_mode", "sentence") or "sentence").lower(),
            split_max_chars=max(20, int(c.get("split_max_chars", 80) or 80)),
            split_max_parts=max(1, int(c.get("split_max_parts", 3) or 3)),
            inter_msg_ms_lo=im_lo,
            inter_msg_ms_hi=im_hi,
        )


def jitter_ms(lo: int, hi: int) -> float:
    lo = max(0, int(lo))
    hi = max(lo, int(hi))
    if lo == hi:
        return lo / 1000.0
    return random.uniform(lo, hi) / 1000.0


def sleep_seconds_for_reading() -> float:
    """保留给老代码的便捷入口。"""
    return jitter_ms(800, 2000)


def _split_by_sentence(text: str, max_parts: int, max_chars: int) -> List[str]:
    raw = [s.strip() for s in _SENT_SPLIT_RE.split(text or "") if s and s.strip()]
    if not raw:
        return [text] if text else []
    # 将过长的句子进一步按 max_chars 拆；过短的相邻句子合并
    pieces: List[str] = []
    cur = ""
    for s in raw:
        if len(s) >= max_chars:
            if cur:
                pieces.append(cur)
                cur = ""
            # 按 max_chars 长度硬拆
            for i in range(0, len(s), max_chars):
                pieces.append(s[i : i + max_chars])
            continue
        cand = (cur + " " + s).strip() if cur else s
        if len(cand) <= max_chars:
            cur = cand
        else:
            if cur:
                pieces.append(cur)
            cur = s
    if cur:
        pieces.append(cur)
    # 控制总条数
    if len(pieces) > max_parts:
        # 把尾部多余的并到最后一条
        head = pieces[: max_parts - 1]
        tail = " ".join(pieces[max_parts - 1 :])
        pieces = head + [tail]
    return pieces


def _split_by_length(text: str, max_parts: int, max_chars: int) -> List[str]:
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]
    pieces: List[str] = []
    i = 0
    while i < len(text) and len(pieces) < max_parts - 1:
        pieces.append(text[i : i + max_chars])
        i += max_chars
    if i < len(text):
        pieces.append(text[i:])
    return pieces


def split_message(text: str, cfg: PacingConfig) -> List[str]:
    """按配置把整段回复切成多条；总是至少返回一条。"""
    text = (text or "").strip()
    if not text:
        return []
    if not cfg.enabled or cfg.split_mode == "none" or cfg.split_max_parts <= 1:
        return [text]
    if cfg.split_mode == "length":
        parts = _split_by_length(text, cfg.split_max_parts, cfg.split_max_chars)
    else:  # sentence
        parts = _split_by_sentence(text, cfg.split_max_parts, cfg.split_max_chars)
    # 兜底：若未切出任何段，退化为整段
    return parts if parts else [text]


def typing_duration_sec(text: str, cfg: PacingConfig) -> float:
    """整段"模拟打字"要花的秒数（整段输入模式下用作发送前的停顿）。"""
    if not text:
        return 0.0
    n = len(text)
    lo = cfg.per_char_ms_lo
    hi = cfg.per_char_ms_hi
    # 抖动：每个字符独立分布 → 用均值 + 5% 噪声近似
    mean = (lo + hi) / 2.0 / 1000.0
    base = n * mean
    noise = random.uniform(-0.08, 0.12) * base
    return max(0.2, base + noise)
