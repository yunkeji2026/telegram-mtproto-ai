# -*- coding: utf-8 -*-
from pathlib import Path
from scripts import i18n_jsconv as jc
from scripts import i18n_htmlconv as hc
from scripts import i18n_mh as mh

TPL = "src/web/templates/tasks.html"

jc.TPL = Path(TPL)
jc.KEY_PREFIX = "tk_js_"
jc.apply({
    "tk_js_001": "No due date",
    "tk_js_002": "No tasks — you're all caught up",
    "tk_js_003": "Overdue",
    "tk_js_004": "Contacts subsystem not enabled",
})

hc.TPL = Path(TPL)
hc.KEY_PREFIX = "tk_s"
hc.apply({
    "tk_s001": "My tasks",
    "tk_s002": "Workspace",
    "tk_s003": "Follow-up tasks",
    "tk_s004": "All agents",
    "tk_s005": "Today & overdue",
    "tk_s006": "Overdue only",
    "tk_s007": "All open",
})

mh.TPL = TPL
mh.insert_keys(
    {
        "tk_tf_count": "共 {n} 条 · 到期(我的/全部) {mine}/{all}",
        "tk_tf_snooze1": "+1天",
        "tk_tf_snooze3": "+3天",
        "tk_tf_snooze7": "+1周",
    },
    {
        "tk_tf_count": "{n} tasks · due (mine/all) {mine}/{all}",
        "tk_tf_snooze1": "+1 day",
        "tk_tf_snooze3": "+3 days",
        "tk_tf_snooze7": "+1 week",
    },
)
print("tasks applied")
