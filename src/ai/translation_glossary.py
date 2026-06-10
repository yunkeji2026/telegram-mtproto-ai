"""P56：术语库加载/合并（全局 + 域包 terminology.yaml）。

术语库两类条目：
- terms（source_term -> 偏好译法）：注入 LLM prompt 提示，统一译法。
- protect（不译保护词，品牌/产品名）：mask→翻译→restore，保证逐字保留。

version() = 内容 hash；术语库变更 → 翻译记忆 cache_key 变 → 自动失效旧译。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class Glossary:
    terms: Dict[str, str] = field(default_factory=dict)
    protect: List[str] = field(default_factory=list)
    version: str = ""

    def empty(self) -> bool:
        return not self.terms and not self.protect


def _hash(terms: Dict[str, str], protect: List[str]) -> str:
    raw = repr(sorted(terms.items())) + "|" + repr(sorted(protect))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _load_yaml(path: Path) -> Dict[str, Any]:
    try:
        import yaml
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def build_glossary(
    config: Optional[Dict[str, Any]],
    *,
    domain_files: Optional[List[Path]] = None,
    overrides: Optional[Dict[str, Any]] = None,
) -> Glossary:
    """合并 域包 terminology.yaml + 全局术语（config.translation.glossary）+ 覆盖层。

    config.translation.glossary:
      enabled: true
      extra_terms: {size: 尺码}     # 全局 terms
      protect: ["LINE", "WhatsApp"] # 全局不译保护词
    域包文件（domains/<d>/prompts/terminology.yaml）顶层 `glossary:` 段同结构，
    或直接 `glossary: {SKU: ...}` 的纯 dict（向后兼容现有文件）。

    优先级（低→高）：域包 < 全局 < overrides（后台控制台可编辑层，P59）。
    overrides = {"terms": {..}, "protect": [..]}。
    """
    tr = ((config or {}).get("translation") or {})
    gl = tr.get("glossary") or {}
    if not gl.get("enabled", True):
        return Glossary({}, [], "")

    terms: Dict[str, str] = {}
    protect: List[str] = []

    # 1) 域包（低优先）
    for path in (domain_files or []):
        try:
            p = Path(path)
        except Exception:
            continue
        if not p.exists():
            continue
        data = _load_yaml(p)
        dg = data.get("glossary", data)  # 兼容：有 glossary 段则取它，否则整 dict
        if isinstance(dg, dict):
            for k, v in dg.items():
                if isinstance(v, str):
                    terms[str(k)] = v
            dp = dg.get("protect") if isinstance(dg.get("protect"), list) else None
            for t in (dp or []):
                if t:
                    protect.append(str(t))

    # 2) 全局（高优先，覆盖域包）
    for k, v in (gl.get("extra_terms") or {}).items():
        if isinstance(v, str):
            terms[str(k)] = v
    for t in (gl.get("protect") or []):
        if t and str(t) not in protect:
            protect.append(str(t))

    # 3) 覆盖层（最高优先，后台控制台可编辑）
    ov = overrides or {}
    ov_terms = ov.get("terms") if isinstance(ov.get("terms"), dict) else {}
    for k, v in ov_terms.items():
        if isinstance(v, (str, int, float)) and str(k):
            terms[str(k)] = str(v)
    ov_protect = ov.get("protect") if isinstance(ov.get("protect"), list) else []
    for t in ov_protect:
        if t and str(t) not in protect:
            protect.append(str(t))

    # 去重 protect 同时保持顺序
    seen = set()
    protect = [t for t in protect if not (t in seen or seen.add(t))]

    return Glossary(terms=terms, protect=protect, version=_hash(terms, protect))
