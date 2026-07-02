# -*- coding: utf-8 -*-
"""i18n reconnaissance for a workspace template (reusable, template-agnostic).

Prints the numbers you need before migrating a page:
  - total lines; <script>/<style> block spans; Jinja block markers (title/content/…)
  - seal metric ``_count_untagged_cjk`` + own-<script> CJK (the two seal locks)
  - keys already used + missing zh/en
  - CJK run stats: total / unique / exact-zh-reusable(%) — how much the shared key bank covers
  - script-start line + static-vs-JS untagged split (how to size the two migration passes)

Shares the CJK ``RUN`` regex with :mod:`scripts.i18n_jsconv` (single source of truth,
incl. the trailing full-width-punctuation fix) so recon counts match what the converters do.

CLI::  python -m scripts.i18n_recon <template.html> [<template2.html> ...]
"""
import re
import sys
from pathlib import Path

from scripts.i18n_jsconv import RUN
from scripts.i18n_scan import _count_untagged_cjk, _strip_comments, scan_workspace_i18n
from src.web.web_i18n import get_translations

TPL_DIR = Path("src/web/templates")
CJK = re.compile(r"[\u4e00-\u9fff]")

# ── data-default CJK leak candidates ─────────────────────────────────────────
# A Chinese literal sitting in a *data* var's fallback — ``he.get('k', '中文')`` /
# ``foo|default('中文')`` — renders that Chinese in an EN locale whenever the field is unset
# (settings.html shipped exactly this: ``he.get('escalation_line', '已多次…')``). Source-level
# cap-0 can't see it (it lives *outside* an i18n hook), only the EN seal render exposes it — so
# recon lists these up front to wrap before migrating (not discover them at seal time).
# ``(i18n or {}).get('k','中文')`` and ``i18n.get(...)`` are the i18n hook itself → excluded:
#   - the (i18n or {}) form has no ``\w`` before ``.get`` so _DATA_GET never matches it;
#   - the bare i18n form is filtered by receiver-name.
_DATA_GET = re.compile(
    r"\b(\w+)\.get\(\s*['\"][^'\"]+['\"]\s*,\s*['\"]([^'\"]*[\u4e00-\u9fff][^'\"]*)['\"]")
_DATA_DEFAULT = re.compile(r"\|\s*default\(\s*['\"]([^'\"]*[\u4e00-\u9fff][^'\"]*)['\"]\s*\)")


def _data_default_cjk(text):
    """Return ``[(line, kind, cjk_default)]`` for data-var fallbacks carrying a Chinese literal."""
    hits = []
    for i, l in enumerate(text.split("\n")):
        for m in _DATA_GET.finditer(l):
            if m.group(1) != "i18n":
                hits.append((i + 1, m.group(1) + ".get", m.group(2)))
        for m in _DATA_DEFAULT.finditer(l):
            hits.append((i + 1, "|default", m.group(1)))
    return hits


# ── split-phrase candidates: a value splits a CJK phrase in two ──────────────────────
# Two forms, same disease — a count/var sits between CJK fragments, so naive per-run conversion
# ships ``used5runs``: the number breaks the phrase and English needs word-order + spacing the split
# can't provide. Plan these as window.Tf('{n} …') phrases (or leading-space measure-word keys).
#   ① template literal: ``用${n}次`` / ``共 ${d.total} 条`` (knowledge.html: 23 of these)
#      ``}CJK`` = measure word after count; ``CJK${`` = phrase head before it.
#   ② string concatenation: ``'累计 '+n+' 次'`` / ``'高风险 '+h+' 条'`` (agent_perf.html: 21 — this
#      page was all concat, zero template-literals, so ① alone missed every one). ``CJK'+`` = literal
#      ends in CJK then joins a value; ``+'…CJK`` = value then a CJK-bearing literal.
_SPLIT_PHRASE = re.compile(
    r"\}\s*[\u4e00-\u9fff]|[\u4e00-\u9fff]\s*\$\{"          # ① ${…} template literal
    r"|[\u4e00-\u9fff][^'\"]*['\"]\s*\+|\+\s*['\"][^'\"]*[\u4e00-\u9fff]"  # ② '…'+v / v+'…' concat
)


def _split_phrase_inline(text, script_spans):
    """Return ``[(line, kind, snippet)]`` for a value splitting a CJK phrase, inside <script> only.

    kind = 'interp' (``${…}`` template literal) or 'concat' (``'…'+v+'…'`` string concatenation)."""
    def _in_script(ln):
        return any(a <= ln <= b for a, b in script_spans)

    hits = []
    for i, l in enumerate(text.split("\n")):
        if not _in_script(i + 1):
            continue
        if _SPLIT_PHRASE.search(l):
            kind = "interp" if ("${" in l or "}" in l) else "concat"
            hits.append((i + 1, kind, l.strip()[:96]))
    return hits


# ── attribute CJK leak candidates: title/placeholder/aria-label/alt with CJK but no data-i18n-* ──
# A tooltip/placeholder attr carrying Chinese but missing its ``data-i18n-*`` hook renders that
# Chinese in an EN locale — yet ``_count_untagged_cjk`` misses it: its line often *also* carries a
# ``data-i18n`` (for the element's text) so the whole line reads "tagged", or the attr sits in a
# multi-line opening tag. Only the EN seal render exposes it (unified_inbox shipped exactly two:
# an App-toggle ``title`` and an iframe ``title``). recon lists these up front to hook before seal.
_ATTR_HOOK = {"title": "data-i18n-title", "placeholder": "data-i18n-placeholder",
              "aria-label": "data-i18n-aria-label", "alt": "data-i18n-alt"}


def _attr_leak_cjk(text):
    """Return ``[(line, attr, value)]`` for a markup attr carrying CJK but lacking its data-i18n hook.

    Scans opening tags (may span lines) outside <script>/<style>/comments — mirrors the EN render
    seal's visible surface, so a hit here == a hit the seal would catch, but flagged pre-migration."""
    body = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", "", text, flags=re.S | re.I)
    body = re.sub(r"<!--.*?-->", "", body, flags=re.S)
    hits = []
    for m in re.finditer(r"<\w+\b[^>]*?>", body, flags=re.S):
        tag = m.group(0)
        ln = body[: m.start()].count("\n") + 1
        for attr, hook in _ATTR_HOOK.items():
            am = re.search(r'\b%s="([^"]*)"' % re.escape(attr), tag)
            if am and CJK.search(am.group(1)) and hook not in tag:
                hits.append((ln, attr, am.group(1)))
    return hits


# ── fullwidth-punctuation-only JS string literals: the recon blind spot ──────────────
# A JS string that builds visible text/attr but carries *only* fullwidth punctuation and no Han
# — e.g. ``title="'+day+'：'+rate+'%（'+a+'/'+t+'）"`` (ai_quality spark tooltip) — slips past BOTH
# seal metrics AND the split-phrase probe: ``_count_untagged_cjk`` counts Han runs only, and
# ``_SPLIT_PHRASE`` requires a ``[\u4e00-\u9fff]`` neighbour, so a Han-free ``：（）`` glue is invisible
# until it renders those fullwidth glyphs in an EN locale. Flag such literals pre-migration so the
# fullwidth ``：（）、；`` gets ASCII-normalised (or the whole phrase moves to window.Tf) before seal.
_FW_PUNCT = re.compile(r"[\u3000-\u303f\uff00-\uffef]")
_STR_LIT = re.compile(r"([\x27\x60\x22])(?:\\.|[^\\])*?\1")


def _fw_punct_only_js(text, script_spans):
    """Return ``[(line, literal)]`` for <script> string literals with fullwidth punct but no Han
    (the class of glue the Han-based probes structurally cannot see)."""
    def _in_script(ln):
        return any(a <= ln <= b for a, b in script_spans)

    hits = []
    for i, l in enumerate(text.split("\n")):
        if not _in_script(i + 1):
            continue
        st = l.strip()
        if st.startswith(("//", "/*", "*")):
            continue
        for m in _STR_LIT.finditer(l):
            seg = m.group(0)
            if _FW_PUNCT.search(seg) and not CJK.search(seg):
                hits.append((i + 1, seg[:60]))
                break
    return hits


def _spans(text, tag):
    out = []
    for m in re.finditer(r"<%s\b[^>]*>(.*?)</%s>" % (tag, tag), text, flags=re.S | re.I):
        out.append((text[: m.start()].count("\n") + 1, text[: m.end()].count("\n") + 1))
    return out


def recon(name):
    text = (TPL_DIR / name).read_text(encoding="utf-8")
    lines = text.split("\n")
    print("=== %s ===" % name)
    print("total_lines=%d" % len(lines))
    print("script blocks:", _spans(text, "script"))
    print("style blocks:", _spans(text, "style"))
    print("jinja blocks:", [(i + 1, l.strip()[:64]) for i, l in enumerate(lines)
                            if re.search(r"\{%\s*block\b", l)])

    print("untagged_cjk (seal metric)=%d" % _count_untagged_cjk(text))
    own = sum(len(CJK.findall(_strip_comments(b)))
              for b in re.findall(r"<script\b[^>]*>(.*?)</script>", text, flags=re.S | re.I))
    print("own_script_cjk=%d" % own)

    rep = scan_workspace_i18n([name])
    pt = rep["per_template"][name]
    print("keys_already_used=%d | missing_en=%d | missing_zh=%d"
          % (pt["keys"], len(rep["missing_en"]), len(rep["missing_zh"])))

    zh = get_translations("zh")
    rev = {v for v in zh.values() if isinstance(v, str)}
    stripped = _strip_comments(text)
    runs = RUN.findall(stripped)
    uniq = set(runs)
    reusable = [r for r in uniq if r in rev]
    print("cjk_runs_total=%d | unique=%d | exact-zh-reusable=%d (%.0f%%)"
          % (len(runs), len(uniq), len(reusable), 100.0 * len(reusable) / max(1, len(uniq))))

    # span-aware split: a line is "JS" iff it falls inside ANY <script> span (handles pages
    # with multiple scattered scripts + static HTML woven between them). htmlconv converts the
    # static bucket (masks every script), jsconv the JS bucket — this sizes the two passes.
    # NB: split on the RAW line grid (real line numbers) and reuse ``_count_untagged_cjk`` per
    # segment — an earlier version compared a *stripped-text* line index against *raw-text* script
    # spans, so a multi-line comment collapsing earlier lines shifted the big <script>'s body into
    # the "static" bucket (knowledge.html: 22 phantom static vs 4 real). Concatenating each bucket
    # then counting keeps comment-stripping self-consistent within the bucket.
    scripts = _spans(text, "script")

    def _in_script(ln):
        return any(a <= ln <= b for a, b in scripts)

    raw = text.split("\n")
    static_seg = [l for i, l in enumerate(raw) if not _in_script(i + 1)]
    js_seg = [l for i, l in enumerate(raw) if _in_script(i + 1)]
    static_u = _count_untagged_cjk("\n".join(static_seg))
    js_u = _count_untagged_cjk("\n".join(js_seg))
    print("script_spans=%d | static_untagged=%d | js_untagged=%d" % (len(scripts), static_u, js_u))

    dd = _data_default_cjk(text)
    print("data-default CJK (leak candidates → wrap before seal): %d" % len(dd))
    for ln, kind, s in dd[:40]:
        print("  L%d %s -> %r" % (ln, kind, s))

    sp = _split_phrase_inline(text, scripts)
    print("split-phrase inline (value splits a CJK phrase → plan as window.Tf): %d" % len(sp))
    for ln, kind, s in sp[:40]:
        print("  L%d [%s]: %s" % (ln, kind, s))

    al = _attr_leak_cjk(text)
    print("attr CJK leak (title/placeholder/aria/alt w/o data-i18n-* → hook before seal): %d" % len(al))
    for ln, attr, s in al[:40]:
        print("  L%d %s= %r" % (ln, attr, s[:70]))

    fw = _fw_punct_only_js(text, scripts)
    print("fullwidth-punct-only JS literal (Han-free ：（）；、 glue → ASCII/Tf before seal): %d" % len(fw))
    for ln, s in fw[:40]:
        print("  L%d %s" % (ln, s))


if __name__ == "__main__":
    names = sys.argv[1:] or ["dashboard.html"]
    for nm in names:
        recon(Path(nm).name)
        print()
