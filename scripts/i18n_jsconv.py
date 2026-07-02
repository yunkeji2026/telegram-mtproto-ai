# -*- coding: utf-8 -*-
"""Context-aware JS i18n converter (template-agnostic).

Scans a template's <script> bodies with a real string/template state machine
(no blind regex — respects comments, regex literals, nested ``${}``), classifies
CJK literals, and emits edits:

  - ``qpure``   : ``'中文'`` / ``"中文"``            -> ``window.T('key')``
  - ``qmix``    : ``'<b>中文</b>'`` (mixed HTML/text) -> ``'<b>'+window.T('key')+'</b>'``
  - ``tmpltext``: bare CJK run in an HTML template literal -> ``${window.T('key')}``
  - ``phrase``  : pure-text template literal with ``${}`` -> FLAGGED for manual Tf (grammar)

Reuses existing keys by exact zh match (any namespace); mints ``<KEY_PREFIX>NNN`` otherwise.

Usage::

    from scripts import i18n_jsconv as jc
    jc.TPL = Path("src/web/templates/whatsapp_rpa.html")
    jc.KEY_PREFIX = "wa_js_"          # minted-key namespace for this page
    jc.build_plan(1448, 1799)         # dry-run -> writes _js_plan/_js_new/_js_phrases.json
    # ...review JSON, author EN dict for the new keys...
    jc.apply(EN_dict)                 # insert keys + splice edits by offset

CLI dry-run: ``python -m scripts.i18n_jsconv LO HI [--tpl PATH] [--prefix wa_js_]``
"""
import json
import re
import sys
from pathlib import Path

from src.web.web_i18n import get_translations

TPL = Path("src/web/templates/messenger_rpa.html")
KEY_PREFIX = "msg_js_"
WORK = Path(".")  # dir holding transient _js_plan.json / _js_new.json / _js_phrases.json

PURE = re.compile(r"[\u4e00-\u9fff]")
# A CJK "run" starts with a Han char and may END with a Han char *or* trailing CJK
# punctuation/closers ( 」』】） …。！？，、：；· ) so matched brackets & sentence-final
# marks stay inside the key (else EN render orphans a stray full-width ``）``/``。`` next to
# the translated text, and qpure strings ending in ``。`` fall through to a lossy qmix split).
_CJK = r"\u4e00-\u9fff"
_TAILP = r"）】」』…。！？，、：；·"
RUN = re.compile(
    r"[%s][%s A-Za-z0-9·…、，。！？：；（）()/%%\.\-]*[%s%s]|[%s]"
    % (_CJK, _CJK, _CJK, _TAILP, _CJK)
)

zh_dict = None  # populated by _load()
REV = None      # zh-value -> best reuse key


def _rank(k):
    if k in ("refresh", "rpa_loading"):
        return 0
    if k.startswith("rpa_"):
        return 1
    if k.startswith("msg_s") or k.startswith("wa_s"):  # prefer static page keys already defined
        return 2
    if k.startswith("ov_"):
        return 3
    if k.startswith("dash."):
        return 4
    return 6


def _load():
    """(Re)load the zh dict + reverse reuse map. Call at the start of build_plan so a
    long-lived process picks up keys inserted by prior batches."""
    global zh_dict, REV
    zh_dict = get_translations("zh")
    REV = {}
    for k, v in zh_dict.items():
        if isinstance(v, str) and (v not in REV or _rank(k) < _rank(REV[v])):
            REV[v] = k


def _senses_map():
    """zh-value -> {key: en} across the whole dict. Powers the ambiguous-reuse warning:
    exact-zh reuse silently picks ONE key per zh (by _rank), but a homograph like ``中``
    maps to both ``inbox.xls.zh``='ZH' and a 'Medium' key — reusing the wrong sense ships a
    mistranslation. Flagging any reused key whose zh carries ≥2 distinct EN senses turns that
    blind spot into a machine-surfaced review candidate."""
    en = get_translations("en")
    m = {}
    for k, v in zh_dict.items():
        if isinstance(v, str):
            m.setdefault(v, {})[k] = en.get(k)
    return m


def _plan_path():
    return WORK / "_js_plan.json"


def _new_path():
    return WORK / "_js_new.json"


def _phrases_path():
    return WORK / "_js_phrases.json"


def _script_spans(text):
    """yield (start_off, end_off) of each <script>...</script> body."""
    for m in re.finditer(r"<script\b[^>]*>(.*?)</script>", text, flags=re.S | re.I):
        yield m.start(1), m.end(1)


def _scan(S, base):
    """Scan one script body S (absolute base offset). Return (qstrings, tmpls).
    qstrings: list of dict {s,e,body}
    tmpls:    list of dict {s,e, text_chunks:[(cs,ce)], html:bool}
    Offsets are ABSOLUTE (base + local)."""
    n = len(S)
    i = 0
    qstrings = []
    tmpls = []
    tstack = []
    expr_depth = []
    prev = ""
    _RX_PREV = set("=([{,;:!&|?+-*%^~<>")

    def in_tmpl():
        return bool(tstack) and (len(expr_depth) < len(tstack))

    while i < n:
        c = S[i]
        if in_tmpl():
            if c == "\\":
                i += 2
                continue
            if c == "`":
                fr = tstack.pop()
                fr["chunks"].append((fr["text_start"], base + i))
                fr["end"] = base + i + 1
                tmpls.append(fr)
                prev = "`"
                i += 1
                continue
            if c == "$" and i + 1 < n and S[i + 1] == "{":
                fr = tstack[-1]
                fr["chunks"].append((fr["text_start"], base + i))
                expr_depth.append(0)
                i += 2
                continue
            i += 1
            continue
        # CODE / EXPR mode
        if c == "/" and i + 1 < n and S[i + 1] == "/":
            j = S.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "/" and i + 1 < n and S[i + 1] == "*":
            j = S.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if c == "/" and (prev == "" or prev in _RX_PREV):
            i += 1
            in_cls = False
            while i < n:
                ch = S[i]
                if ch == "\\":
                    i += 2
                    continue
                if ch == "[":
                    in_cls = True
                elif ch == "]":
                    in_cls = False
                elif ch == "/" and not in_cls:
                    break
                elif ch == "\n":
                    break
                i += 1
            i += 1
            while i < n and S[i].isalpha():
                i += 1
            prev = "/"
            continue
        if c == "'" or c == '"':
            q = c
            st = i
            i += 1
            while i < n:
                if S[i] == "\\":
                    i += 2
                    continue
                if S[i] == q:
                    break
                i += 1
            body = S[st + 1:i]
            if PURE.search(body):
                qstrings.append({"s": base + st, "e": base + i + 1, "body": body})
            prev = q
            i += 1
            continue
        if c == "`":
            tstack.append({"start": base + i, "chunks": [], "text_start": base + i + 1})
            i += 1
            continue
        if c == "{":
            if expr_depth:
                expr_depth[-1] += 1
            prev = "{"
            i += 1
            continue
        if c == "}":
            if expr_depth:
                if expr_depth[-1] == 0:
                    expr_depth.pop()
                    tstack[-1]["text_start"] = base + i + 1
                else:
                    expr_depth[-1] -= 1
            prev = "}"
            i += 1
            continue
        if not c.isspace():
            prev = c
        i += 1
    for t in tmpls:
        span = S[t["start"] - base:t["end"] - base]
        t["html"] = ("<" in span or ">" in span)
    return qstrings, tmpls


def build_plan(lo, hi):
    _load()
    text = TPL.read_text(encoding="utf-8")
    nl = [m.start() for m in re.finditer("\n", text)]

    def line_of(off):
        import bisect
        return bisect.bisect_right(nl, off) + 1

    edits = []
    phrases = []
    pat = re.compile(r"^%s(\d+)$" % re.escape(KEY_PREFIX))
    seq = [max([int(m.group(1)) for k in zh_dict for m in [pat.match(k)] if m] or [0])]
    minted = {}

    def keyfor(zh):
        if zh in REV:
            return REV[zh]
        if zh in minted:
            return minted[zh]
        seq[0] += 1
        k = "%s%03d" % (KEY_PREFIX, seq[0])
        minted[zh] = k
        return k

    phrase_spans = []
    for bs, be in _script_spans(text):
        S = text[bs:be]
        qstrings, tmpls = _scan(S, bs)
        for t in tmpls:
            span_txt = text[t["start"]:t["end"]]
            if not t["html"] and len(t["chunks"]) > 1 and PURE.search(span_txt):
                phrase_spans.append((t["start"], t["end"]))
                phrases.append({"line": line_of(t["start"]), "span": span_txt})

        def in_phrase(off):
            return any(a <= off < b for a, b in phrase_spans)

        for q in qstrings:
            if in_phrase(q["s"]):
                continue
            body = q["body"]
            runs = list(RUN.finditer(body))
            if not runs:
                continue
            stripped = body.strip()
            if RUN.fullmatch(stripped) and stripped == body:
                key = keyfor(body)
                edits.append({"s": q["s"], "e": q["e"], "kind": "qpure", "zh": body, "key": key,
                              "new": "window.T('%s')" % key})
            else:
                qc = text[q["s"]]
                parts = []
                last = 0

                def lit(seg):
                    if seg:
                        parts.append(qc + seg + qc)

                for rm in runs:
                    lit(body[last:rm.start()])
                    parts.append("window.T('%s')" % keyfor(rm.group(0)))
                    last = rm.end()
                lit(body[last:])
                edits.append({"s": q["s"], "e": q["e"], "kind": "qmix",
                              "zh": "|".join(r.group(0) for r in runs),
                              "key": "", "new": "+".join(parts)})
        for t in tmpls:
            if not t["html"]:
                continue
            for (cs, ce) in t["chunks"]:
                chunk = text[cs:ce]
                for rm in RUN.finditer(chunk):
                    run = rm.group(0)
                    a = cs + rm.start()
                    b = cs + rm.end()
                    if in_phrase(a):
                        continue
                    key = keyfor(run)
                    edits.append({"s": a, "e": b, "kind": "tmpltext", "zh": run, "key": key,
                                  "new": "${window.T('%s')}" % key})

    edits = [e for e in edits if lo <= line_of(e["s"]) <= hi]
    edits.sort(key=lambda e: e["s"])
    pruned = []
    last_end = -1
    for e in edits:
        if e["s"] < last_end:
            continue
        pruned.append(e)
        last_end = e["e"]
    for e in pruned:
        e["line"] = line_of(e["s"])
        e["old"] = text[e["s"]:e["e"]]

    minted_rev = {v: k for k, v in minted.items()}
    harvest = re.compile(r"window\.T\('(%s\d+)'\)" % re.escape(KEY_PREFIX))
    new = {}
    for e in pruned:
        for k in harvest.findall(e["new"]):
            if k in minted_rev and k not in new:
                new[k] = minted_rev[k]
    # ── ambiguous-reuse warning: any reused key whose zh has ≥2 distinct EN senses ──
    senses = _senses_map()
    any_key = re.compile(r"window\.T\('([^']+)'\)")
    used = set()
    for e in pruned:
        used.update(any_key.findall(e["new"]))
    ambig = {}
    for k in sorted(used - set(new)):
        zh = zh_dict.get(k)
        s = senses.get(zh, {})
        if len({v for v in s.values() if v}) >= 2:
            ambig[k] = {"zh": zh, "chosen_en": s.get(k), "candidates": s}
    (WORK / "_js_ambig.json").write_text(json.dumps(ambig, ensure_ascii=False, indent=1), encoding="utf-8")

    _plan_path().write_text(json.dumps(pruned, ensure_ascii=False, indent=1), encoding="utf-8")
    _new_path().write_text(json.dumps(new, ensure_ascii=False, indent=1), encoding="utf-8")
    _phrases_path().write_text(
        json.dumps([p for p in phrases if lo <= p["line"] <= hi], ensure_ascii=False, indent=1),
        encoding="utf-8")
    print("range %d-%d | edits: %d | new keys: %d | reused: %d | phrase-flags(manual Tf): %d" % (
        lo, hi, len(pruned), len(new), len(pruned) - len(new),
        len([p for p in phrases if lo <= p['line'] <= hi])))
    if ambig:
        print("[warn] ambiguous reuse: %d key(s) whose zh has multiple EN senses — review _js_ambig.json" % len(ambig))
        for k, info in ambig.items():
            print("  %s -> %r (zh %r); senses: %s" % (k, info["chosen_en"], info["zh"], info["candidates"]))


def apply(EN):
    plan = json.loads(_plan_path().read_text(encoding="utf-8"))
    new = json.loads(_new_path().read_text(encoding="utf-8"))
    assert set(EN) == set(new), "EN/new mismatch: %s | %s" % (set(new) - set(EN), set(EN) - set(new))
    bad = [k for k, v in EN.items() if PURE.search(v)]
    assert not bad, "EN has CJK: %s" % bad
    from scripts import i18n_mh as _mh
    _mh.TPL = str(TPL)
    _mh.insert_keys({k: new[k] for k in new}, EN)
    text = TPL.read_text(encoding="utf-8")
    for e in sorted(plan, key=lambda e: e["s"], reverse=True):
        assert text[e["s"]:e["e"]] == e["old"], "offset drift at %d" % e["s"]
        text = text[:e["s"]] + e["new"] + text[e["e"]:]
    TPL.write_text(text, encoding="utf-8")
    print("APPLIED %d edits" % len(plan))


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--tpl" in args:
        j = args.index("--tpl")
        TPL = Path(args[j + 1])
        del args[j:j + 2]
    if "--prefix" in args:
        j = args.index("--prefix")
        KEY_PREFIX = args[j + 1]
        del args[j:j + 2]
    build_plan(int(args[0]), int(args[1]))
