# -*- coding: utf-8 -*-
from pathlib import Path
from scripts import i18n_htmlconv as hc
from scripts import i18n_mh as mh

TPL = "src/web/templates/golive_checklist.html"

hc.TPL = Path(TPL)
hc.KEY_PREFIX = "gl_s"
hc.apply({})  # 4 edits, all reused keys

mh.TPL = TPL
mh.insert_keys(
    {
        "gl_tf_sum": '<b style="color:var(--tk-ok-ink);">{ok}</b> 通过\u3000<b style="color:var(--tk-warn-ink);">{warn}</b> 建议\u3000<b style="color:var(--tk-danger-ink);">{fail}</b> 待修',
        "gl_js_action": "处理",
    },
    {
        "gl_tf_sum": '<b style="color:var(--tk-ok-ink);">{ok}</b> passed\u3000<b style="color:var(--tk-warn-ink);">{warn}</b> suggested\u3000<b style="color:var(--tk-danger-ink);">{fail}</b> to fix',
        "gl_js_action": "Fix",
    },
)
print("golive html + manual keys done")
