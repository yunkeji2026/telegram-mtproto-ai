# -*- coding: utf-8 -*-
from pathlib import Path
from scripts import i18n_jsconv as jc
from scripts import i18n_htmlconv as hc
from scripts import i18n_mh as mh

TPL = "src/web/templates/draft_audit_page.html"

jc.TPL = Path(TPL)
jc.KEY_PREFIX = "da_js_"
jc.apply({})  # 0 new keys, all reused; splices 8 edits

hc.TPL = Path(TPL)
hc.KEY_PREFIX = "da_s"
hc.apply({
    "da_s001": "Draft audit log",
    "da_s002": "Back to draft workspace",
    "da_s003": "Draft ID (optional)",
    "da_s004": "Agent ID (optional)",
    "da_s005": "Action",
    "da_s006": "Reason",
})

mh.TPL = TPL
mh.insert_keys(
    {
        "da_o_all": "全部动作", "da_o_blocked": "blocked（拦截）",
        "da_o_force": "force_override（强制放行）", "da_o_autosend": "autosend（自动发送）",
        "da_o_approved": "approved（批准）", "da_o_rejected": "rejected（拒绝）",
        "da_tf_count": "共 {n} 条记录",
    },
    {
        "da_o_all": "All actions", "da_o_blocked": "Blocked",
        "da_o_force": "Force-passed", "da_o_autosend": "Auto-sent",
        "da_o_approved": "Approved", "da_o_rejected": "Rejected",
        "da_tf_count": "{n} records",
    },
)
print("da manual keys inserted")
