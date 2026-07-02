# -*- coding: utf-8 -*-
"""Parameterized i18n migration harness (template-agnostic).

Reusable across workspace templates. Set the target template before use, then:
  - ``insert_keys(zh, en)``  — splice new keys into src/web/web_i18n.py (both packs)
  - ``apply_edits(edits)``   — line-anchored in-place edits on the target template
  - ``report(node=True)``    — untagged/own-script CJK + missing-key + node --check status

Usage::

    from scripts import i18n_mh
    i18n_mh.TPL = "src/web/templates/whatsapp_rpa.html"
    i18n_mh.insert_keys({"wa_s001": "中文"}, {"wa_s001": "English"})
    i18n_mh.apply_edits([(120, 'old', 'new')])
    i18n_mh.report(node=True)
"""
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

W = Path("src/web/web_i18n.py")
TPL = None  # set by caller: path (str|Path) to the template being migrated
CJK = re.compile(r"[\u4e00-\u9fff]")


def _tpl() -> Path:
    if not TPL:
        raise RuntimeError("i18n_mh.TPL not set — assign the target template path first")
    return Path(TPL)


def insert_keys(zh, en):
    """Insert ``zh``/``en`` key dicts into the ``zh``/``en`` blocks of web_i18n.py.

    Aborts if any key already exists (guards against double-apply on re-run)."""
    text = W.read_text(encoding="utf-8")
    existing = set(re.findall(r'^\s{8}"([^"]+)"\s*:', text, flags=re.M))
    dup = [k for k in zh if k in existing]
    assert not dup, "keys already present: %s" % dup
    m_en_open = text.index('\n    "en": {')
    zh_close = text.rindex("\n    },", 0, m_en_open) + 1
    en_close = text.index("\n    },", m_en_open) + 1

    def block(d):
        return "".join(
            '        %s: %s,\n' % (json.dumps(k, ensure_ascii=False), json.dumps(v, ensure_ascii=False))
            for k, v in d.items()
        )

    text = text[:en_close] + block(en) + text[en_close:]
    text = text[:zh_close] + block(zh) + text[zh_close:]
    W.write_text(text, encoding="utf-8")
    print("INSERTED %d keys" % len(zh))


def apply_edits(edits):
    """Apply ``(line_1based, old_substr, new_substr)`` edits; abort if any anchor missing."""
    p = _tpl()
    lines = p.read_text(encoding="utf-8").split("\n")
    fails = []
    for (ln, old, new) in edits:
        if old not in lines[ln - 1]:
            fails.append((ln, old))
    if fails:
        for ln, old in fails:
            print('EDIT FAIL L%d: missing %r' % (ln, old))
            print('   actual: %r' % lines[ln - 1])
        raise SystemExit("aborting: %d edit anchors not found" % len(fails))
    for (ln, old, new) in edits:
        lines[ln - 1] = lines[ln - 1].replace(old, new, 1)
    p.write_text("\n".join(lines), encoding="utf-8")
    print("APPLIED %d edits" % len(edits))


def report(node=True):
    """Print i18n status for the target: whole-file untagged CJK, own-<script> CJK,
    missing zh/en keys, and (optionally) ``node --check`` on every masked <script> body."""
    src = _tpl().read_text(encoding="utf-8")
    from scripts.i18n_scan import _strip_comments, scan_workspace_i18n
    leaks = 0
    for body in re.findall(r"<script\b[^>]*>(.*?)</script>", src, flags=re.S | re.I):
        leaks += len(CJK.findall(_strip_comments(body)))
    name = _tpl().name
    rep = scan_workspace_i18n([name])
    info = rep["per_template"].get(name, {})
    print("untagged CJK (whole file):", info.get("untagged_cjk"), " | own-script CJK:", leaks,
          " | missing_zh:", rep["missing_zh"][:8], " missing_en:", rep["missing_en"][:8])
    if node:
        ok = True
        for body in re.findall(r"<script\b[^>]*>(.*?)</script>", src, flags=re.S | re.I):
            masked = re.sub(r"\{\{.*?\}\}", "0", body)
            masked = re.sub(r"\{%.*?%\}", "", masked)
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
                f.write(masked)
                fn = f.name
            try:
                r = subprocess.run(["node", "--check", fn], capture_output=True, text=True)
                if r.returncode != 0:
                    ok = False
                    print("node --check FAIL:", r.stderr.strip()[:400])
            finally:
                os.unlink(fn)
        print("node --check:", "all blocks OK" if ok else "FAILED")
