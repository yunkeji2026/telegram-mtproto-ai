# -*- coding: utf-8 -*-
from pathlib import Path
from scripts import i18n_jsconv as jc
from scripts import i18n_htmlconv as hc
from scripts import i18n_mh as mh

TPL = "src/web/templates/diff.html"

jc.TPL = Path(TPL)
jc.KEY_PREFIX = "diff_js_"
jc.apply({
    "diff_js_001": "Quota",
    "diff_js_002": "Load failed, please refresh",
    "diff_js_003": "No snapshots yet",
    "diff_js_004": "Quick compare: ",
    "diff_js_005": "This config has fewer than two snapshots",
    "diff_js_006": "No snapshots for this config yet",
    "diff_js_007": "Confirm rollback",
})

hc.TPL = Path(TPL)
hc.KEY_PREFIX = "diff_s"
hc.apply({
    "diff_s001": "Version diff lets admins trace config changes.",
    "diff_s002": "Snapshot timeline",
    "diff_s003": "Old version",
    "diff_s004": "New version",
    "diff_s005": "Click to select",
    "diff_s006": "Quick compare: ",
    "diff_s007": "Select snapshot",
    "diff_s008": "Current config",
    "diff_s009": "Compare",
    "diff_s010": "Roll back to old version",
    "diff_s011": "Clear selection",
    "diff_s012": "Diff result",
    "diff_s013": "The two versions are identical — no differences",
    "diff_s014": "lines added",
    "diff_s015": "lines removed",
    "diff_s016": "Select two snapshot versions to compare",
})

mh.TPL = TPL
mh.insert_keys(
    {
        "diff_s_lblA": "旧版本（A — Before）",
        "diff_s_lblB": "新版本（B — After）",
        "diff_tf_latest2": "{label}：最新两版本",
        "diff_tf_latestcur": "{label}：最新 vs 当前",
        "diff_js_rollback_confirm": "回滚确认",
        "diff_tf_rollback_q": "确定回滚到快照「{id}」？<br>当前配置将被覆盖（系统已自动备份）。",
        "diff_js_rolledback": "已回滚: ",
    },
    {
        "diff_s_lblA": "Old version (A — Before)",
        "diff_s_lblB": "New version (B — After)",
        "diff_tf_latest2": "{label}: latest two versions",
        "diff_tf_latestcur": "{label}: latest vs current",
        "diff_js_rollback_confirm": "Rollback confirmation",
        "diff_tf_rollback_q": 'Roll back to snapshot "{id}"?<br>The current config will be overwritten (auto-backed up).',
        "diff_js_rolledback": "Rolled back: ",
    },
)
print("diff manual keys inserted")
