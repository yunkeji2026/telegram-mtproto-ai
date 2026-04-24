"""Vision 模型返回的 JSON 字符串修复与失败样本落盘（combined / inbox 等共用）。"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def strip_vision_code_fence(raw: str) -> str:
    s = (raw or "").strip()
    if s.startswith("```"):
        lines = [ln for ln in s.splitlines() if not ln.strip().startswith("```")]
        s = "\n".join(lines).strip()
    return s


def json_close_unfinished_string(s: str) -> str:
    """模型在 preview 等字段里截断，导致未闭合的 ""。补一个结尾引号再解析。"""
    in_str, esc, i, n = False, False, 0, len(s)
    while i < n:
        c = s[i]
        if esc:
            esc = False
        elif c == "\\":
            esc = True
        elif in_str:
            if c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
        i += 1
    if in_str:
        return s + '"'
    return s


def json_balance_outside_strings(s: str) -> str:
    """在字符串外为未闭合的 [ 与 { 补全 ]}。"""
    in_str, esc, stack = False, False, []
    for c in s:
        if esc:
            esc = False
            continue
        if in_str:
            if c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
            continue
        if c == "[":
            stack.append("]")
        elif c == "{":
            stack.append("}")
        elif c in "]}":
            if stack and c == stack[-1]:
                stack.pop()
    return s + "".join(reversed(stack))


def json_extract_first_object(s: str) -> str:
    """部分响应在 JSON 后带解释文字，截取第一个 { ... } 块（大括号配平）。"""
    start = s.find("{")
    if start < 0:
        return s
    depth, in_str, esc, i, n = 0, False, False, start, len(s)
    end = n
    while i < n:
        c = s[i]
        if esc:
            esc = False
            i += 1
            continue
        if in_str:
            if c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            i += 1
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
        i += 1
    return s[start:end]


def _fail_dir() -> Path:
    d = os.environ.get("MESSENGER_VISION_JSON_FAIL_DIR", "").strip()
    if d:
        return Path(d).resolve()
    return Path("tmp_messenger_rpa").resolve()


def dump_failed_vision_json(raw: str, *, label: str = "json") -> None:
    """多策略仍失败时把原始响应写入磁盘（默认 tmp_messenger_rpa）。"""
    s = (raw or "").strip()
    if not s:
        return
    try:
        base = _fail_dir()
        base.mkdir(parents=True, exist_ok=True)
        fn = base / (
            f"vision_json_fail_{int(time.time())}_"
            f"{label.replace('/', '_')[:40]}_{uuid.uuid4().hex[:6]}.txt"
        )
        fn.write_text(s[:12_000], encoding="utf-8", errors="replace")
        logger.warning("[vision_json] 解析失败样本已落盘: %s", fn)
    except OSError as ex:
        logger.debug("[vision_json] 落盘失败: %s", ex)


def parse_vision_json_loose(
    raw: str,
    *,
    dump_label: str = "loose",
    write_dump: bool = True,
) -> Optional[Dict[str, Any]]:
    """对 Vision 返回的 JSON 做多候选修复后 ``json.loads`` 为 dict。

    - 用于 combined inbox/thread、inbox_scanner 等，避免各写一套失败逻辑。
    - 全失败时可选落盘，便于对模型输出调参。
    """
    s0 = strip_vision_code_fence(raw)
    if not s0:
        return None
    p = s0.find("{")
    s_from_brace = s0[p:] if p >= 0 else s0
    candidates: List[str] = []
    for a in (s0, s_from_brace, json_extract_first_object(s0), json_extract_first_object(s_from_brace)):
        if a and a not in candidates:
            candidates.append(a)
    for base in list(candidates):
        x = json_close_unfinished_string(base)
        if x != base and x not in candidates:
            candidates.append(x)
        y = json_balance_outside_strings(json_close_unfinished_string(base))
        if y and y not in candidates:
            candidates.append(y)
    for s in candidates:
        try:
            return json.loads(s)
        except Exception:
            continue
    s_dbg = s0[:240].replace("\n", "\\n")
    logger.warning(
        "vision JSON 解析失败（多策略仍失败） label=%r raw~=%r", dump_label, s_dbg,
    )
    if write_dump:
        dump_failed_vision_json(s0, label=dump_label)
    return None
