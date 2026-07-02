# -*- coding: utf-8 -*-
"""后端 UI-facing 中文字面量扫描器（P10-A）。

模板层的裸 CJK 已由 test_i18n_coverage 密封；但**后端注入 UI 的中文**（API 响应里的 label/msg/
detail、序列化给前端的标签字典）尚无门禁。后端 .py 的 CJK 绝大多数是**内部噪声**（日志/注释/
docstring/AI 提示词/审计文案），不该动；本扫描器用 AST 精确挑出「大概率会到浏览器」的少数，按
置信度分档，供人工三选一收口，杜绝对内部串误伤。

分类（每个 CJK 字符串字面量按最近上下文归档）：
  HIGH   —— HTTPException(detail=…) / 返回 dict 里 UI 键(msg/message/detail/label/title/text/
            error/reason/hint/name/desc/description/tip/placeholder) 的值。这些直接进 API 响应。
  MED    —— 模块级「标签字典」常量值（形如 _X_LABELS = {..: '中文'}），常被序列化给前端。
  SKIP   —— logger/logging/print/warnings 调用实参、注释、docstring、raise 非 HTTPException。

用法：
  python -m scripts.i18n_pyscan [<dir-or-file> ...]        # 默认扫 src/web
  python -m scripts.i18n_pyscan --all                      # 扫 src 全量（噪声多，仅供摸底）
  python -m scripts.i18n_pyscan src/web/routes/contacts_routes.py --show-skip
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

CJK = re.compile(r"[\u4e00-\u9fff]")

_UI_KEYS = {"msg", "message", "detail", "label", "title", "text", "error", "reason",
            "hint", "name", "desc", "description", "tip", "placeholder", "note",
            "summary", "status_label", "stage_label"}
_LOG_ATTRS = {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
_LOG_FUNCS = {"print", "logger", "logging"}


def _is_log_call(node: ast.Call) -> bool:
    f = node.func
    if isinstance(f, ast.Attribute):
        if f.attr in _LOG_ATTRS:
            return True
        # logger.bind(...).info(...) 等链式：底端 attr 已覆盖；再看根名
        root = f
        while isinstance(root, ast.Attribute):
            root = root.value
        if isinstance(root, ast.Name) and root.id in _LOG_FUNCS:
            return True
    if isinstance(f, ast.Name) and f.id in _LOG_FUNCS:
        return True
    return False


def _label_dict_name(assign_targets) -> str | None:
    for t in assign_targets:
        if isinstance(t, ast.Name) and ("LABEL" in t.id.upper() or t.id.upper().endswith("_ZH")
                                        or "TEXT" in t.id.upper() or "MSG" in t.id.upper()):
            return t.id
    return None


class _Scanner(ast.NodeVisitor):
    def __init__(self, src: str):
        self.src = src
        self.hits = []  # (lineno, tier, ctx, text)
        self._parents = {}
        self._docstrings = set()

    def _record_docstrings(self, tree):
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                doc = ast.get_docstring(node, clean=False)
                if doc:
                    body0 = node.body[0]
                    if isinstance(body0, ast.Expr) and isinstance(body0.value, ast.Constant):
                        self._docstrings.add(id(body0.value))

    def run(self, tree):
        self._record_docstrings(tree)
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                self._parents[id(child)] = parent
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and CJK.search(node.value):
                if id(node) in self._docstrings:
                    continue
                self._classify(node)

    def _classify(self, node: ast.Constant):
        # 向上找上下文
        cur = node
        in_log = False
        ui_key = None
        http_exc = False
        depth = 0
        while id(cur) in self._parents and depth < 8:
            p = self._parents[id(cur)]
            depth += 1
            if isinstance(p, ast.Call):
                if _is_log_call(p):
                    in_log = True
                    break
                if isinstance(p.func, ast.Name) and p.func.id == "HTTPException":
                    http_exc = True
                if isinstance(p.func, ast.Attribute) and p.func.attr == "HTTPException":
                    http_exc = True
                # detail= kwarg?
                for kw in p.keywords:
                    if kw.arg == "detail" and kw.value is cur:
                        http_exc = http_exc or True
            if isinstance(p, ast.Dict):
                for k, v in zip(p.keys, p.values):
                    if v is cur and isinstance(k, ast.Constant) and isinstance(k.value, str):
                        if k.value in _UI_KEYS:
                            ui_key = k.value
            cur = p
        text = node.value.replace("\n", " ")[:70]
        if in_log:
            self.hits.append((node.lineno, "SKIP", "log/print", text))
        elif http_exc or ui_key:
            self.hits.append((node.lineno, "HIGH", ui_key or "HTTPException.detail", text))
        else:
            # 模块级标签字典？
            lbl = self._enclosing_label_dict(node)
            if lbl:
                self.hits.append((node.lineno, "MED", f"dict {lbl}", text))
            else:
                self.hits.append((node.lineno, "SKIP", "other", text))

    def _enclosing_label_dict(self, node) -> str | None:
        cur = node
        depth = 0
        while id(cur) in self._parents and depth < 10:
            p = self._parents[id(cur)]
            depth += 1
            if isinstance(p, ast.Assign):
                return _label_dict_name(p.targets)
            cur = p
        return None


def scan_file(path: Path):
    try:
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
    except (SyntaxError, UnicodeDecodeError):
        return []
    sc = _Scanner(src)
    sc.run(tree)
    return sc.hits


def main(argv):
    show_skip = "--show-skip" in argv
    argv = [a for a in argv if a != "--show-skip"]
    if "--all" in argv:
        roots = [Path("src")]
        argv = [a for a in argv if a != "--all"]
    elif argv:
        roots = [Path(a) for a in argv]
    else:
        roots = [Path("src/web")]

    files = []
    for r in roots:
        if r.is_file():
            files.append(r)
        else:
            files.extend(sorted(r.rglob("*.py")))

    tally = {"HIGH": 0, "MED": 0, "SKIP": 0}
    per_file = {}
    for f in files:
        hits = scan_file(f)
        keep = [h for h in hits if show_skip or h[1] != "SKIP"]
        for _, tier, _, _ in hits:
            tally[tier] += 1
        if keep:
            per_file[f] = keep

    for f, hits in per_file.items():
        shown = [h for h in hits if h[1] in ("HIGH", "MED") or show_skip]
        if not shown:
            continue
        print(f"\n=== {f} ===")
        for ln, tier, ctx, text in shown:
            print(f"  [{tier}] L{ln} ({ctx}): {text}")
    print("\n---- TALLY ----")
    print(f"HIGH(API/UI 直出)={tally['HIGH']}  MED(标签字典)={tally['MED']}  SKIP(日志/内部/其他)={tally['SKIP']}")


if __name__ == "__main__":
    main(sys.argv[1:])
