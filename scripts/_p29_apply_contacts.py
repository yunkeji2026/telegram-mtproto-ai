# -*- coding: utf-8 -*-
from pathlib import Path
from scripts import i18n_jsconv as jc
from scripts import i18n_htmlconv as hc
from scripts import i18n_mh as mh

TPL = "src/web/templates/contacts_list.html"

jc.TPL = Path(TPL)
jc.KEY_PREFIX = "cl_js_"
jc.apply({
    "cl_js_001": "No matching contacts",
    "cl_js_002": "Lead captured",
})

hc.TPL = Path(TPL)
hc.KEY_PREFIX = "cl_s"
hc.apply({
    "cl_s001": "Contacts",
    "cl_s002": "Search: name / ID / channel number…",
    "cl_s003": "All contacts",
    "cl_s004": "No lead yet",
    "cl_s005": "All follow-ups",
    "cl_s006": "Has follow-up plan",
    "cl_s007": "Previous",
    "cl_s008": "Next",
})

mh.TPL = TPL
mh.insert_keys(
    {
        "cl_o_due": "待跟进(到期)",
        "cl_tf_all_n": "全部 {n}",
        "cl_tf_pageinfo": "{pg} / {pages} 页 · 共 {total}",
    },
    {
        "cl_o_due": "Follow-up due",
        "cl_tf_all_n": "All {n}",
        "cl_tf_pageinfo": "{pg} / {pages} · {total} total",
    },
)
print("contacts_list applied")
