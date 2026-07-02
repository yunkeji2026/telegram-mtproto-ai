"""P38：后端路由响应文案批量本地化施工器（map-driven，确定性 + 可复审 + 幂等）。

**为什么不做全自动 AST 改写**：f-string 的 ``{expr}`` → 具名 ``tr(..., fmt=expr)``、多条近义中文
归并到同一 key、以及 EN 译文，都是需要人判的语义活；对几十处 raise 点做魔法自动改写风险高。
故本工具只做机械且安全的部分：

- :func:`apply_map` —— 对源码套用 ``{old_snippet: new_snippet}`` 精确替换（命中即全替，
  0 命中会被记入 report 供调用方断言，绝不静默漏改）。
- :func:`ensure_import` —— 幂等保证 ``from src.web.web_i18n import tr`` 存在。

键名/译文/占位符映射由调用方（人/agent）curate 后传入；每收口一个路由族复用同一函数，
避免几十处手工替换出错。配套只读扫描见 ``scripts.i18n_scan.scan_routes_response_cjk``。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Mapping, Tuple

_DEFAULT_IMPORT = "from src.web.web_i18n import tr"

# 含 CJK 的字符串字面量（可选 f 前缀）：body 至少含一个中日韩表意字；用数字反向引用 \2 匹配同类引号。
_RE_CJK_STR_LITERAL = re.compile(
    r"""(f?)(['"])((?:\\.|(?!\2).)*?[\u4e00-\u9fff](?:\\.|(?!\2).)*?)\2"""
)
_RE_FSTR_PLACEHOLDER = re.compile(r"\{([^}]*)\}")

# 作用域安全的响应片段（P40）：只认「HTTPException(状态码, "中文…")」与
# 「"detail"/"error": "中文…"」两种上下文——绝不产出裸字面量 map（防同一中文出现在
# 非响应位置被全局误替）。f-string 片段仅登记、不自动组 new（需人工具名 fmt）。
_RE_HTTPEXC_CJK = re.compile(
    r"""HTTPException\(\s*(\d+)\s*,\s*(f?)(['"])((?:\\.|(?!\3).)*?[\u4e00-\u9fff](?:\\.|(?!\3).)*?)\3\s*\)"""
)
# 响应字段白名单（P41）：默认 detail/error（与棘轮账本 scan_routes_response_cjk 同口径）；
# 可扩到 msg/reason 等——但**扩这里也要同步扩账本 scanner**再一起提门禁，否则口径漂移。
_DEFAULT_RESP_FIELDS = ("detail", "error")


def _dict_kwarg_res(fields):
    alt = "|".join(re.escape(f) for f in fields)
    dict_re = re.compile(
        r"""(['"])(""" + alt + r""")\1\s*:\s*(f?)(['"])((?:\\.|(?!\4).)*?[\u4e00-\u9fff](?:\\.|(?!\4).)*?)\4"""
    )
    # 关键字实参形式：HTTPException(status_code=…, detail="中文") / detail=f"…"
    kwarg_re = re.compile(
        r"""\b(""" + alt + r""")\s*=\s*(f?)(['"])((?:\\.|(?!\3).)*?[\u4e00-\u9fff](?:\\.|(?!\3).)*?)\3"""
    )
    return dict_re, kwarg_re


_RE_DICT_CJK, _RE_KWARG_CJK = _dict_kwarg_res(_DEFAULT_RESP_FIELDS)


def apply_map(text: str, mapping: Mapping[str, str]) -> Tuple[str, Dict[str, int]]:
    """对 ``text`` 套用 ``{old: new}`` 精确替换。

    返回 ``(new_text, report)``，``report[old]`` 为该 old 的命中次数（0 表示未命中——
    调用方应据此校验 curate 的 map 是否与源码脱节）。命中即替换全部出现。
    """
    report: Dict[str, int] = {}
    for old, new in mapping.items():
        cnt = text.count(old)
        report[old] = cnt
        if cnt:
            text = text.replace(old, new)
    return text, report


def ensure_import(text: str, import_line: str = _DEFAULT_IMPORT) -> Tuple[str, bool]:
    """幂等确保某 import 行存在；缺失则插到最后一处 **top-level** import 之后。

    返回 ``(new_text, added)``。已存在→原样返回、``added=False``。
    """
    if import_line in text:
        return text, False
    lines = text.splitlines(keepends=True)
    idx = 0
    depth = 0            # 括号续行深度：多行 `from x import ( … )` 视为一条语句
    cont = False         # 反斜杠续行
    in_stmt = False      # 当前是否在 import 语句内
    for i, ln in enumerate(lines):
        s = ln.lstrip()
        top_level = ln[:1] not in (" ", "\t")
        if depth == 0 and not cont and top_level and (s.startswith("from ") or s.startswith("import ")):
            in_stmt = True
        if in_stmt:
            depth += ln.count("(") - ln.count(")")
            cont = ln.rstrip().endswith("\\")
            if depth <= 0 and not cont:
                idx = i + 1          # 语句在本行收尾 → 插到其后
                in_stmt = False
                depth = 0
    lines.insert(idx, import_line + ("\n" if not import_line.endswith("\n") else ""))
    return "".join(lines), True


def suggest_map(text: str, zh_dict: Mapping[str, str]) -> List[dict]:
    """P39：为一个路由文件的 CJK 响应文案生成**键匹配建议**（半自动，加速 curate map）。

    对每条硬编码中文字符串字面量（经 :func:`~scripts.i18n_scan.iter_route_cjk_prose` 圈定
    的响应文案行）：
    - 若其中文**精确命中**现有字典某 key 的 zh 值 → 建议 ``reuse`` 该 key（跨族复用，
      如 base.shell.pwd_min_len），把「大海捞现成键」自动化；
    - 否则建议 ``new``（调用方起新键名）；
    - f-string 标 ``is_fstring`` + 提取 ``{占位符}``，提示需具名 fmt（不自动改，防误判语义）。

    返回按首次出现排序的建议列表；键匹配存在多候选时取字典序最小 key（稳定可复现）。
    """
    from scripts.i18n_scan import iter_route_cjk_prose

    reverse: Dict[str, List[str]] = {}
    for k, v in zh_dict.items():
        if isinstance(v, str):
            reverse.setdefault(v, []).append(k)

    seen: Dict[str, dict] = {}
    for lineno, line in iter_route_cjk_prose(text):
        for m in _RE_CJK_STR_LITERAL.finditer(line):
            is_f = bool(m.group(1))
            body = m.group(3)
            entry = seen.get(body)
            if entry is None:
                cands = reverse.get(body, [])
                entry = seen[body] = {
                    "literal": m.group(0),
                    "is_fstring": is_f,
                    "placeholders": _RE_FSTR_PLACEHOLDER.findall(body) if is_f else [],
                    "match_key": (min(cands) if cands else None),
                    "action": ("reuse" if cands else "new"),
                    "count": 0,
                    "lines": [],
                }
            entry["count"] += 1
            entry["lines"].append(lineno)
    return list(seen.values())


def draft_map(text: str, zh_dict: Mapping[str, str], *, fields=None) -> List[dict]:
    """P40：为响应文案生成**作用域安全**的可施工条目（比 suggest 更进一步）。

    只圈定两种上下文（不产出裸字面量，杜绝同一中文出现在非响应位置被全局误替）：
    ``HTTPException(<status>, "中文…")`` 与 ``"detail"/"error": "中文…"``。每条给出：
    - ``old``：源码里可精确 :func:`str.replace` 的整段片段；
    - ``kind``/``status``/``field``：供 :func:`build_draft_map` 组装 ``tr(...)`` 调用；
    - ``match_key``：中文精确命中现有键时的可复用键；
    - ``is_fstring``：True 则仅登记不自动组 new（需人工具名 fmt）。

    按 ``old`` 去重（同一中文在 httpexc / dict 两种上下文各成一条）。
    """
    from scripts.i18n_scan import iter_route_cjk_prose

    dict_re, kwarg_re = (_RE_DICT_CJK, _RE_KWARG_CJK) if not fields else _dict_kwarg_res(fields)

    reverse: Dict[str, List[str]] = {}
    for k, v in zh_dict.items():
        if isinstance(v, str):
            reverse.setdefault(v, []).append(k)

    seen: Dict[str, dict] = {}

    def _add(body, old, is_f, kind, *, status=None, field=None):
        e = seen.get(old)
        if e is None:
            cands = reverse.get(body, [])
            e = seen[old] = {
                "body": body, "old": old, "is_fstring": is_f, "kind": kind,
                "status": status, "field": field,
                "match_key": (min(cands) if cands else None), "count": 0,
            }
        e["count"] += 1

    for _lineno, line in iter_route_cjk_prose(text):
        for m in _RE_HTTPEXC_CJK.finditer(line):
            _add(m.group(4), m.group(0), bool(m.group(2)), "httpexc", status=m.group(1))
        for m in dict_re.finditer(line):
            _add(m.group(5), m.group(0), bool(m.group(3)), "dict", field=m.group(2))
        for m in kwarg_re.finditer(line):
            _add(m.group(4), m.group(0), bool(m.group(2)), "kwarg", field=m.group(1))
    return list(seen.values())


def draft_coverage(text: str, zh_dict: Mapping[str, str], *, fields=None) -> dict:
    """P41：诚实的施工器**覆盖率体检**——量化「账本命中数 vs draft 能安全施工的 site 数」，
    并**点名 draft 圈不进的账本行**（如字符串拼接 ``"…" + var``、多参 HTTPException 等需人工）。

    返回 ``{ledger, covered, ratio, uncovered:[(lineno, line)]}``。选靶时用 ratio 分流：
    1.0=可一把过；<1.0 的差集就是必须人工处理的少数。
    """
    from scripts.i18n_scan import count_route_cjk_prose, iter_route_cjk_prose

    ents = draft_map(text, zh_dict, fields=fields)
    bodies = [e["body"] for e in ents]
    ledger = count_route_cjk_prose(text)
    covered = sum(e["count"] for e in ents)
    uncovered = [(ln, line.strip()) for ln, line in iter_route_cjk_prose(text)
                 if not any(b in line for b in bodies)]
    return {
        "ledger": ledger, "covered": covered,
        "ratio": (covered / ledger if ledger else 1.0),
        "uncovered": uncovered,
    }


def build_draft_map(entries, key_for=None) -> Tuple[Dict[str, str], List[dict], List[dict]]:
    """把 :func:`draft_map` 条目组装成可施工的 ``{old: new}``。

    ``key_for(entry)`` 返回下列之一决定每条用哪个 i18n 键 + 传参：
    - ``"err.x.y"``：无 fmt 的 ``tr(request, "err.x.y")``；
    - ``("err.x.y", {"platform": '"LINE"', "err": "e"})``：带具名 fmt（值为**源码表达式串**，
      故字面量要自带引号，如 ``'"LINE"'``；变量直接写 ``"e"``）——这条使 f-string 与
      ``{platform}`` 参数化也能走同一施工器；
    - ``None``：跳过（非 f→``pending``，f→``fstrings``），交人工。

    ``key_for`` 缺省时对非 f 条目取 ``entry['match_key']``（精确复用命中）。
    返回 ``(mapping, pending, fstrings)``。
    """
    def _default(e):
        return None if e["is_fstring"] else e["match_key"]

    key_for = key_for or _default
    mapping: Dict[str, str] = {}
    pending: List[dict] = []
    fstrings: List[dict] = []
    for e in entries:
        spec = key_for(e)
        if spec is None:
            (fstrings if e["is_fstring"] else pending).append(e)
            continue
        if isinstance(spec, tuple):
            key, fmt = spec
        else:
            key, fmt = spec, None
        call = f'tr(request, "{key}"'
        if fmt:
            call += "".join(f", {k}={v}" for k, v in fmt.items())
        call += ")"
        if e["kind"] == "httpexc":
            new = f'HTTPException({e["status"]}, {call})'
        elif e["kind"] == "kwarg":
            new = f'{e["field"]}={call}'
        else:
            new = f'"{e["field"]}": {call}'
        mapping[e["old"]] = new
    return mapping, pending, fstrings


def convert_file(
    path,
    mapping: Mapping[str, str],
    *,
    import_line: str = _DEFAULT_IMPORT,
    write: bool = True,
) -> dict:
    """把 ``mapping`` 套用到单个路由文件，并确保 tr import 存在。

    返回报告 dict：``report``（逐条命中次数）、``unmatched``（0 命中的 old）、
    ``total_replaced``、``import_added``、``changed``。
    """
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    new, report = apply_map(text, mapping)
    new, added = ensure_import(new, import_line)
    changed = new != text
    if write and changed:
        p.write_text(new, encoding="utf-8")
    return {
        "report": report,
        "unmatched": sorted(k for k, v in report.items() if v == 0),
        "total_replaced": sum(report.values()),
        "import_added": added,
        "changed": changed,
    }


def coverage_report(zh_dict: Mapping[str, str], *, fields=None) -> List[dict]:
    """P42：对**所有**尚有硬编码中文响应文案的 routes 文件跑 :func:`draft_coverage`，
    按 ratio 升序（最需人工的排前）返回报表。用于选靶分流：
    ratio=1.0 → draft 可一把过（批量档）；ratio<1.0 → 差集是必须人工的少数硬骨头。
    """
    from scripts.i18n_scan import _ROUTES_DIR, scan_routes_response_cjk

    rows: List[dict] = []
    for fname in scan_routes_response_cjk():
        text = (_ROUTES_DIR / fname).read_text(encoding="utf-8")
        cov = draft_coverage(text, zh_dict, fields=fields)
        rows.append({"file": fname, **cov})
    rows.sort(key=lambda r: (r["ratio"], -r["ledger"]))
    return rows


def _main(argv=None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Route i18n conversion helper (P38/P39)")
    ap.add_argument("--suggest", metavar="ROUTE_FILE",
                    help="打印该路由文件 CJK 响应文案的键匹配建议（reuse 现有键 / new 新键）")
    ap.add_argument("--coverage", metavar="ROUTE_FILE",
                    help="打印 draft 施工器覆盖率（ratio<1 时点名圈不进的账本行）")
    ap.add_argument("--coverage-all", action="store_true",
                    help="全库覆盖率报表：对所有待清 routes 文件按 ratio 升序列出，选靶分流用")
    args = ap.parse_args(argv)
    if not (args.suggest or args.coverage or args.coverage_all):
        ap.error("需要 --suggest / --coverage / --coverage-all")
    from src.web.web_i18n import get_translations

    if args.coverage_all:
        rows = coverage_report(get_translations("zh"))
        full = [r for r in rows if r["ratio"] >= 1.0]
        print(f"[routeconv] coverage-all: {len(rows)} files, "
              f"{len(full)} at ratio 1.0 (batchable), {len(rows) - len(full)} need manual")
        for r in rows:
            flag = "OK " if r["ratio"] >= 1.0 else "!! "
            print(f"  {flag}{r['ratio']:.2f}  ledger={r['ledger']:>3} covered={r['covered']:>3}  {r['file']}")
        return 0

    if args.coverage:
        text = Path(args.coverage).read_text(encoding="utf-8")
        cov = draft_coverage(text, get_translations("zh"))
        print(f"[routeconv] {args.coverage}: ledger={cov['ledger']} covered={cov['covered']} "
              f"ratio={cov['ratio']:.2f}")
        for ln, line in cov["uncovered"]:
            print(f"  UNCOVERED L{ln}: {line}")
        return 0

    text = Path(args.suggest).read_text(encoding="utf-8")
    sugg = suggest_map(text, get_translations("zh"))
    reuse = [s for s in sugg if s["action"] == "reuse"]
    new = [s for s in sugg if s["action"] == "new"]
    print(f"[routeconv] {args.suggest}: {len(sugg)} distinct CJK strings "
          f"({len(reuse)} reuse existing key, {len(new)} new)")
    for s in sugg:
        tag = f"REUSE {s['match_key']}" if s["action"] == "reuse" else "NEW"
        fstr = f" [f-string {s['placeholders']}]" if s["is_fstring"] else ""
        print(f"  ×{s['count']} {tag}{fstr}: {s['literal']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
