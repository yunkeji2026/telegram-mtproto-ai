"""评测数据集加载（YAML / JSONL）+ 内置种子。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class IntentSample:
    text: str
    intent: str          # 人工标注的 ground-truth 意图
    note: str = ""


@dataclass
class FaqSample:
    question: str        # 应可被 KB 自动解决的 FAQ 问题
    note: str = ""


def load_intent_samples(path: Optional[str] = None) -> List[IntentSample]:
    """从 YAML 或 JSONL 加载意图样本；path 为空则返回内置种子集。

    YAML 形如：``- {text: "在吗", intent: "打招呼"}``
    JSONL 形如：每行 ``{"text": "...", "intent": "..."}``
    """
    if not path:
        return list(_SEED_INTENT_SAMPLES)
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    if path.endswith(".jsonl"):
        out: List[IntentSample] = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                out.append(IntentSample(text=str(d.get("text", "")),
                                        intent=str(d.get("intent", "")),
                                        note=str(d.get("note", ""))))
        return out
    # 默认按 YAML 解析
    import yaml
    with open(path, "r", encoding="utf-8") as f:
        rows = yaml.safe_load(f) or []
    return [IntentSample(text=str(r.get("text", "")),
                         intent=str(r.get("intent", "")),
                         note=str(r.get("note", "")))
            for r in rows if isinstance(r, dict)]


# 内置种子：覆盖规则版意图标签空间（打招呼/停止联系/需要安抚/不满投诉/
# 短句接话/提问/继续聊天/空消息）。人工标注，作为可复现基线。
_SEED_INTENT_SAMPLES: List[IntentSample] = [
    IntentSample("在吗", "打招呼"),
    IntentSample("你好", "打招呼"),
    IntentSample("hello", "打招呼"),
    IntentSample("嗨", "打招呼"),
    IntentSample("别再联系我了", "停止联系"),
    IntentSample("stop contacting me", "停止联系"),
    IntentSample("unsubscribe please", "停止联系"),
    IntentSample("我最近好难过，压力好大", "需要安抚"),
    IntentSample("我好焦虑睡不着", "需要安抚"),
    IntentSample("感觉好孤独", "需要安抚"),
    IntentSample("你们这什么破服务，太气人了", "不满/投诉"),
    IntentSample("我真的很生气，烦死了", "不满/投诉"),
    IntentSample("好的", "短句接话"),
    IntentSample("嗯嗯", "短句接话"),
    IntentSample("哈哈哈", "短句接话"),
    IntentSample("收到", "短句接话"),
    IntentSample("这个产品的尺码怎么选？", "提问"),
    IntentSample("请问发货要多久？", "提问"),
    IntentSample("How long does shipping take?", "提问"),
    IntentSample("能便宜点吗？", "提问"),
    IntentSample("今天天气不错我们出去走走吧聊聊近况", "继续聊天"),
    IntentSample("我刚看完那部电影觉得还挺好看的推荐你也看看", "继续聊天"),
    IntentSample("", "空消息"),
    IntentSample("   ", "空消息"),
]


def load_faq_samples(path: Optional[str] = None) -> List["FaqSample"]:
    """加载 FAQ 样本（YAML/JSONL）；path 为空则返回内置种子集。

    YAML/JSONL 每条：``{question: "...", note: "..."}``
    """
    if not path:
        return list(_SEED_FAQ_SAMPLES)
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    if path.endswith(".jsonl"):
        out: List[FaqSample] = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                out.append(FaqSample(question=str(d.get("question", "")),
                                     note=str(d.get("note", ""))))
        return out
    import yaml
    with open(path, "r", encoding="utf-8") as f:
        rows = yaml.safe_load(f) or []
    return [FaqSample(question=str(r.get("question", "")),
                      note=str(r.get("note", "")))
            for r in rows if isinstance(r, dict)]


# 内置 FAQ 种子（跨境电商常见问题），作为解决率评测的默认输入
_SEED_FAQ_SAMPLES: List["FaqSample"] = [
    FaqSample("怎么退货"),
    FaqSample("发货要多久"),
    FaqSample("支持货到付款吗"),
    FaqSample("尺码怎么选"),
    FaqSample("支持哪些支付方式"),
    FaqSample("可以退款吗"),
    FaqSample("물류 어떻게 확인하나요", "韩文物流查询"),
    FaqSample("How do I track my order"),
]
