# -*- coding: utf-8 -*-
from pathlib import Path
from scripts import i18n_jsconv as jc
from scripts import i18n_htmlconv as hc
from scripts import i18n_mh as mh

TPL = "src/web/templates/admin_tts_dashboard.html"

jc.TPL = Path(TPL)
jc.KEY_PREFIX = "atd_js_"
jc.apply({
    "atd_js_001": "No files yet",
    "atd_js_002": "Cleaning…",
    "atd_js_003": "Cleanup failed",
})

hc.TPL = Path(TPL)
hc.KEY_PREFIX = "atd_s"
hc.apply({
    "atd_s001": "Overview",
    "atd_s002": "Files",
    "atd_s003": "Total size",
    "atd_s004": "File age",
    "atd_s005": "Oldest file",
    "atd_s006": "Newest file",
    "atd_s007": "Files older than 24h should be cleaned up",
    "atd_s008": "Prefix breakdown",
    "atd_s009": "Ops actions",
})

mh.TPL = TPL
mh.insert_keys(
    {
        "atd_s000": "TTS 预览文件仪表盘",
        "atd_s_h1": "预览文件仪表盘",
        "atd_s_back": "返回首页",
        "atd_s014": "自动刷新: 30s |",
        "atd_s_disk": "磁盘占用估算 (假设 1GB 阈值)",
        "atd_s015": "tts-*: 通用 | line-tts-*: LINE | wa-tts-*: WhatsApp",
        "atd_s016": "清理 >1h",
        "atd_s017": "清理 >24h",
        "atd_s018": "清理 >7d",
        "atd_tf_cleaned": "已清理 {n} 个文件",
    },
    {
        "atd_s000": "TTS preview file dashboard",
        "atd_s_h1": "preview file dashboard",
        "atd_s_back": "Back to home",
        "atd_s014": "Auto-refresh: 30s |",
        "atd_s_disk": "Disk usage estimate (1GB threshold assumed)",
        "atd_s015": "tts-*: general | line-tts-*: LINE | wa-tts-*: WhatsApp",
        "atd_s016": "Clean >1h",
        "atd_s017": "Clean >24h",
        "atd_s018": "Clean >7d",
        "atd_tf_cleaned": "Removed {n} file(s)",
    },
)
print("admin_tts_dashboard applied")
