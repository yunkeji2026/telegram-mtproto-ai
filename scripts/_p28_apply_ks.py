# -*- coding: utf-8 -*-
from pathlib import Path
from scripts import i18n_jsconv as jc
from scripts import i18n_htmlconv as hc
from scripts import i18n_mh as mh

TPL = "src/web/templates/kb_cold_start.html"

jc.TPL = Path(TPL)
jc.KEY_PREFIX = "ks_js_"
jc.apply({
    "ks_js_001": "Knowledge base unavailable",
    "ks_js_002": "Cold — seeding recommended",
    "ks_js_003": "Knowledge available",
    "ks_js_004": "Seed in one click",
    "ks_js_005": "Seeding…",
    "ks_js_006": "Seeding failed",
    "ks_js_007": "Seed again",
})

hc.TPL = Path(TPL)
hc.KEY_PREFIX = "ks_s"
hc.apply({
    "ks_s001": "Knowledge-base cold start",
    "ks_s002": "Back to setup wizard",
    "ks_s003": "Channels are connected, but the AI has no private-domain knowledge yet. Pick a scenario to seed starter scripts, then fine-tune in the knowledge base.",
    "ks_s004": "Enabled KB entries",
    "ks_s005": "Categories covered",
})

mh.TPL = TPL
mh.insert_keys(
    {
        "ks_s006": "播种后建议：",
        "ks_s010": "① 到",
        "ks_s011": "② 用",
        "ks_s008": "按自家业务修改话术；",
        "ks_s009": "输入客户问法，确认 AI 能命中作答。",
        "ks_tf_packcount": "{n} 条起步话术",
        "ks_tf_added": "新增 {added} 条",
        "ks_tf_added_skip": "新增 {added} 条，跳过 {skipped} 条已存在",
    },
    {
        "ks_s006": "After seeding: ",
        "ks_s010": "① Go to",
        "ks_s011": "② Use",
        "ks_s008": "and tailor scripts to your business; ",
        "ks_s009": "enter sample customer questions and confirm the AI answers correctly.",
        "ks_tf_packcount": "{n} starter scripts",
        "ks_tf_added": "Added {added}",
        "ks_tf_added_skip": "Added {added}, skipped {skipped} already existing",
    },
)
print("kb_cold_start applied")
