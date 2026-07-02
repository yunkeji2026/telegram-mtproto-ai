# -*- coding: utf-8 -*-
"""Context-aware static-HTML i18n converter (template-agnostic).

Wraps bare CJK in a template's **static** (Jinja/HTML) layer into the server-side hook
``{{ (i18n or {}).get('key', "中文") }}`` — the SSR convention used by rpa_overview /
messenger_rpa (no flash of untranslated content). CJK inside ``<script>`` / ``<style>`` /
HTML comments / Jinja ``{% %}{{ }}{# #}`` is masked out (offset-preserving) so only
user-visible text nodes + attribute values are touched.

Reuses existing keys by exact zh match (any namespace); mints ``<KEY_PREFIX>NNN`` otherwise.
Attribute values get the opposite fallback-quote of their enclosing quote (no clash).

Usage::

    from scripts import i18n_htmlconv as hc
    hc.TPL = Path("src/web/templates/whatsapp_rpa.html")
    hc.KEY_PREFIX = "wa_s"
    hc.build_plan(3, 500)      # dry-run -> _html_plan.json / _html_new.json
    # ...review, author EN...
    hc.apply(EN_dict)

CLI dry-run: ``python -m scripts.i18n_htmlconv LO HI [--tpl PATH] [--prefix wa_s]``
"""
import json
import re
import sys
from pathlib import Path

from src.web.web_i18n import get_translations

TPL = Path("src/web/templates/whatsapp_rpa.html")
KEY_PREFIX = "wa_s"
WORK = Path(".")

PURE = re.compile(r"[\u4e00-\u9fff]")
# A CJK "run" = a user-visible phrase. Body may weave in latin/digits/spaces/CJK-punct;
# it must START with a Han char but may END with a Han char *or* trailing CJK punctuation
# (closers 」』】）/ …。！？，、：；) so matched brackets & ellipses stay inside the key
# (else EN render orphans a stray full-width ``）`` / ``…`` next to the translated text).
_CJK = r"\u4e00-\u9fff"
_TAILP = r"）】」』…。！？，、：；·"
RUN = re.compile(
    r"[%s][%s A-Za-z0-9·…、，。！？：；（）()/%%\.\-]*[%s%s]|[%s]"
    % (_CJK, _CJK, _CJK, _TAILP, _CJK)
)

# masked regions: script/style bodies (incl tags), all comment kinds, all Jinja constructs
_MASK = re.compile(
    r"<script\b[^>]*>.*?</script>"
    r"|<style\b[^>]*>.*?</style>"
    r"|<!--.*?-->"
    r"|\{#.*?#\}"
    r"|\{%.*?%\}"
    r"|\{\{.*?\}\}",
    flags=re.S | re.I,
)

zh_dict = None
REV = None


def _rank(k):
    if k.startswith(("rpa_", "msg_s", "wa_s", "ov_")):
        return 1
    if k.startswith(("dash.", "nav.")):
        return 2
    return 6


def _load():
    global zh_dict, REV
    zh_dict = get_translations("zh")
    REV = {}
    for k, v in zh_dict.items():
        if isinstance(v, str) and (v not in REV or _rank(k) < _rank(REV[v])):
            REV[v] = k


def _senses_map():
    """zh-value -> {key: en} across the whole dict; powers the ambiguous-reuse warning
    (a homograph zh with ≥2 distinct EN senses is flagged so the wrong sense isn't shipped)."""
    en = get_translations("en")
    m = {}
    for k, v in zh_dict.items():
        if isinstance(v, str):
            m.setdefault(v, {})[k] = en.get(k)
    return m


def _mask(text):
    """Replace masked regions with a sentinel ``\\x00`` (offsets + lines preserved).

    The sentinel is deliberately *not* a space: space is in the RUN body class, so masking
    with spaces let a RUN bridge across a masked ``{% else %}`` / ``{{ var }}`` and then
    ``run = text[s:e]`` restored the template syntax into the key (e.g. a title
    ``{% if %}中文A{% else %}中文B{% endif %}`` collapsed into one polluted key). ``\\x00``
    is outside the RUN char classes → masked regions become hard boundaries, so ``CJK {{v}} CJK``
    correctly splits into two clean keys with the interpolation left untouched between them."""
    def repl(m):
        return "".join("\n" if ch == "\n" else "\x00" for ch in m.group(0))
    return _MASK.sub(repl, text)


def _attr_quote(masked, pos):
    """If offset ``pos`` sits inside a quoted attribute value, return that quote char, else ''.
    Looks at the current tag (nearest unmasked '<' up to pos) for a trailing ``=["']...`` open."""
    lt = masked.rfind("<", 0, pos)
    gt = masked.rfind(">", 0, pos)
    if lt < 0 or lt < gt:
        return ""  # not inside a tag → text node
    seg = masked[lt:pos]
    m = re.search(r"=\s*([\"'])[^\"']*$", seg)
    return m.group(1) if m else ""


def _plan_path():
    return WORK / "_html_plan.json"


def _new_path():
    return WORK / "_html_new.json"


def build_plan(lo, hi):
    _load()
    text = TPL.read_text(encoding="utf-8")
    masked = _mask(text)
    nl = [m.start() for m in re.finditer("\n", text)]

    def line_of(off):
        import bisect
        return bisect.bisect_right(nl, off) + 1

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

    edits = []
    for m in RUN.finditer(masked):
        s, e = m.start(), m.end()
        run = text[s:e]
        aq = _attr_quote(masked, s)
        fq = "'" if aq == '"' else '"'          # opposite of enclosing attr quote
        if fq in run:                            # extremely rare: CJK text w/ ascii quote
            fq = "'" if fq == '"' else '"'
        key = keyfor(run)
        new = "{{ (i18n or {}).get('%s', %s%s%s) }}" % (key, fq, run, fq)
        edits.append({"s": s, "e": e, "zh": run, "key": key, "attr": aq, "new": new})

    edits = [ed for ed in edits if lo <= line_of(ed["s"]) <= hi]
    edits.sort(key=lambda ed: ed["s"])
    pruned = []
    last = -1
    for ed in edits:
        if ed["s"] < last:
            continue
        pruned.append(ed)
        last = ed["e"]
    for ed in pruned:
        ed["line"] = line_of(ed["s"])
        ed["old"] = text[ed["s"]:ed["e"]]

    minted_rev = {v: k for k, v in minted.items()}
    new = {}
    for ed in pruned:
        k = ed["key"]
        if k in minted_rev and k not in new:
            new[k] = minted_rev[k]

    senses = _senses_map()
    ambig = {}
    for k in sorted({ed["key"] for ed in pruned} - set(new)):
        zh = zh_dict.get(k)
        s = senses.get(zh, {})
        if len({v for v in s.values() if v}) >= 2:
            ambig[k] = {"zh": zh, "chosen_en": s.get(k), "candidates": s}
    (WORK / "_html_ambig.json").write_text(json.dumps(ambig, ensure_ascii=False, indent=1), encoding="utf-8")

    _plan_path().write_text(json.dumps(pruned, ensure_ascii=False, indent=1), encoding="utf-8")
    _new_path().write_text(json.dumps(new, ensure_ascii=False, indent=1), encoding="utf-8")
    print("range %d-%d | edits: %d | new keys: %d | reused: %d" % (
        lo, hi, len(pruned), len(new), len(pruned) - len(new)))
    if ambig:
        print("[warn] ambiguous reuse: %d key(s) whose zh has multiple EN senses — review _html_ambig.json" % len(ambig))
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
    for ed in sorted(plan, key=lambda e: e["s"], reverse=True):
        assert text[ed["s"]:ed["e"]] == ed["old"], "offset drift at %d" % ed["s"]
        text = text[:ed["s"]] + ed["new"] + text[ed["e"]:]
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
