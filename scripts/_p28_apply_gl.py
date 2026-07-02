# -*- coding: utf-8 -*-
from pathlib import Path
from scripts import i18n_jsconv as jc
from scripts import i18n_htmlconv as hc
from scripts import i18n_mh as mh

TPL = "src/web/templates/golive_checklist.html"

jc.TPL = Path(TPL)
jc.KEY_PREFIX = "gl_js_"
jc.apply({
    "gl_js_001": "Ready to go live",
    "gl_js_002": "All hard requirements are met — you're good to launch!",
    "gl_js_003": "Can go live, with suggestions",
    "gl_js_004": "Hard requirements are met; address yellow items soon.",
    "gl_js_005": "Not ready to go live",
    "gl_js_006": "Must-fix issues remain — see red items below.",
})

hc.TPL = Path(TPL)
hc.KEY_PREFIX = "gl_s"
hc.apply({})  # 4 edits, all reused keys

mh.TPL = TPL
mh.insert_keys(
    {
        "gl_tf_sum": '<b style="color:var(--tk-ok-ink);">{ok}</b> 通过　<b style="color:var(--tk-warn-ink);">{warn}</b> 建议　<b style="color:var(--tk-danger-ink);">{fail}</b> 待修',
        "gl_js_action": "处理",
    },
    {
        "gl_tf_sum": '<b style="color:var(--tk-ok-ink);">{ok}</b> passed　<b style="color:var(--tk-warn-ink);">{warn}</b> suggested　<b style="color:var(--tk-danger-ink);">{fail}</b> to fix',
        "gl_js_action": "Fix",
    },
)
print("golive_checklist applied")
