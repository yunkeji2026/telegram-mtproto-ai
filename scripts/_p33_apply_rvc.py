# -*- coding: utf-8 -*-
"""P33b: voice_call.html JS 层 i18n 批量收口（i18n_jsconv plan + 手工修正）。"""
from __future__ import annotations

import json
from pathlib import Path

from scripts import i18n_jsconv as jc
from scripts import i18n_mh as mh

TPL = Path("src/web/templates/voice_call.html")
WORK = Path(".")

# AudioWorklet 源码串里的中文注释 — 不进用户 UI，禁止误改
_SKIP_LINES = {581, 585, 587, 589}

EN = {
    "rvc_js_001": "Loaded",
    "rvc_js_002": "memories about them (tap to view)",
    "rvc_js_003": "more",
    "rvc_js_004": "long-term memories about them",
    "rvc_js_005": "Playful",
    "rvc_js_006": "Serious",
    "rvc_js_007": "Calm",
    "rvc_js_008": "Warm",
    "rvc_js_009": "Gentle",
    "rvc_js_010": "Excited",
    "rvc_js_011": "Melancholy",
    "rvc_js_012": "No personas available (check config/profiles_runtime.yaml)",
    "rvc_js_013": "Cloned",
    "rvc_js_014": "Default voice",
    "rvc_js_015": "mo ago",
    "rvc_js_016": "Fresh chat (no memory)",
    "rvc_js_017": "Enter another chat_key manually…",
    "rvc_js_018": "Synthesizing with",
    "rvc_js_019": "'s voice…",
    "rvc_js_020": "Playing",
    "rvc_js_021": "cloned voice",
    "rvc_js_022": "reference voice)",
    "rvc_js_023": "Preview failed",
    "rvc_js_024": "Preview uses the real call clone voice (engine must be running)",
    "rvc_js_025": "Reference voice (actual call is decided by the voice host)",
    "rvc_js_026": "Upload voice sample",
    "rvc_js_027": "Voice cloned",
    "rvc_js_028": "Ready for calls; upload again to replace)",
    "rvc_js_029": "Using default voice · Upload",
    "rvc_js_030": "s of clean speech to clone their voice (auto-converted to 16 kHz mono)",
    "rvc_js_031": "Uploading & processing",
    "rvc_js_032": "'s voice…",
    "rvc_js_033": "Unrecognized audio — use wav/mp3 or another common format",
    "rvc_js_034": "File too large",
    "rvc_js_035": "Wrong access token (see Advanced)",
    "rvc_js_036": "Invalid persona",
    "rvc_js_037": "Empty file",
    "rvc_js_038": "saved, but quality is low:",
    "rvc_js_039": "see tips below)",
    "rvc_js_040": "cloned (can improve:",
    "rvc_js_041": "Voice cloned — will be used on the next call",
    "rvc_js_042": "Upload timed out — retry",
    "rvc_js_043": "'s voice sample and revert to default?",
    "rvc_js_044": "Removed — back to default voice",
    "rvc_js_045": "Remove failed",
    "rvc_js_046": "Hi there, nice to meet you.",
    "rvc_js_047": "Synthesis failed (check TTS config/network)",
    "rvc_js_048": "Playback failed",
    "rvc_js_049": "Synthesis timed out — retry",
    "rvc_js_050": "Requesting microphone…",
    "rvc_js_051": "Microphone permission denied",
    "rvc_js_052": "Connected — waking up…",
    "rvc_js_053": "In call ·",
    "rvc_js_054": "memories about them",
    "rvc_js_055": "Fresh chat · no history loaded",
    "rvc_js_056": "Something went wrong",
    "rvc_js_057": "Call ended",
    "rvc_js_058": "Connection error",
    "rvc_js_059": "Connecting to voice host…",
    "rvc_js_060": "Waking engine…",
    "rvc_js_061": "first cold start ~",
    "rvc_js_062": "Releasing VRAM…",
    "rvc_js_063": "Voice host unreachable (check host/firewall)",
    "rvc_js_064": "Retry",
    "rvc_js_065": "Not loaded (click",
    "rvc_js_066": "Start engine",
    "rvc_js_067": "on the right to use VRAM)",
    "rvc_js_068": "Ready — you can connect",
    "rvc_js_069": "VRAM",
    "rvc_js_070": "Releasing…",
    "rvc_js_071": "Call",
    "rvc_js_072": "Engine ready — you can connect",
    "rvc_js_073": "Load timed out — retry (check GPU host worker logs)",
    "rvc_js_074": "Release timed out — retry",
    "rvc_js_075": "Incorrect access token",
    "rvc_js_076": "Voice feature disabled",
    "rvc_js_077": "Hanging up…",
}

# 整句 Tf 键（plan 里 qmix 拆太碎的几处）
TF_EN = {
    "rvc_tf_mem_summary": "Loaded {n} memories about them (tap to view)",
    "rvc_tf_mem_more": "… {n} more",
    "rvc_tf_mem_long": "Loaded {n} long-term memories about them",
    "rvc_tf_preview_synth": "Synthesizing with “{name}”…",
    "rvc_tf_preview_listen": "🔊 Preview “{name}”",
    "rvc_tf_upload_for": "🎙️ Upload voice for “{name}”",
    "rvc_tf_uploading": "⏳ Uploading & processing “{name}”…",
    "rvc_tf_remove_q": "Remove “{name}”'s voice sample and revert to default?",
    "rvc_tf_toast_low": "⚠️ “{name}” saved, but quality is low: {issue} (see tips below)",
    "rvc_tf_toast_yellow": "🎙️ “{name}” cloned (can improve: {issue})",
    "rvc_tf_toast_ok": "🎙️ “{name}” voice cloned — will be used on the next call",
    "rvc_tf_call_with": "📞 Call {name}",
    "rvc_tf_engine_load": "Waking engine… {secs}s (first cold start ~15–60s)",
    "rvc_tf_engine_connect": "Connecting to voice host… {secs}s",
    "rvc_tf_engine_release": "Releasing VRAM… {secs}s",
    "rvc_tf_engine_ready_vram": "Ready — you can connect · VRAM {gb}GB",
    "rvc_tf_unreachable": "Voice host unreachable (check host/firewall) — click Retry",
    "rvc_tf_engine_idle": "Not loaded (click Start engine on the right to use VRAM)",
    "rvc_tf_in_call": "In call · {lang}",
    "rvc_tf_mem_toast": "💭 Loaded {n} memories about them",
    "rvc_tf_rel_min": "{n} min ago",
    "rvc_tf_rel_hour": "{n} h ago",
    "rvc_tf_rel_day": "{n} d ago",
    "rvc_tf_rel_month": "{n} mo ago",
    "rvc_tf_clone_badge": "🎙️ Cloned{warn}",
    "rvc_tf_cloned_base": "✅ Voice cloned{dur}",
    "rvc_tf_cloned_hint": "{base}{health} (ready for calls; upload again to replace)",
    "rvc_tf_default_hint": "Using default voice · Upload 6–15s of clean speech to clone their voice (auto 16 kHz mono)",
    "rvc_tf_play_tag_clone": " (🎙️ {tag})",
    "rvc_tf_play_tag_edge": " (reference voice)",
    "rvc_tf_http_fail": "Operation failed (HTTP {code})",
}

TF_ZH = {
    "rvc_tf_mem_summary": "💭 已带入 {n} 条关于 TA 的记忆（点开看）",
    "rvc_tf_mem_more": "… 还有 {n} 条",
    "rvc_tf_mem_long": "💭 已带入 {n} 条关于 TA 的长期记忆",
    "rvc_tf_preview_synth": "正在用「{name}」的音色合成…",
    "rvc_tf_preview_listen": "🔊 试听「{name}」",
    "rvc_tf_upload_for": "🎙️ 为「{name}」上传真人声",
    "rvc_tf_uploading": "⏳ 上传并处理「{name}」的声音…",
    "rvc_tf_remove_q": "移除「{name}」的真人声，恢复默认音？",
    "rvc_tf_toast_low": "⚠️ 「{name}」已存，但质量偏低：{issue}（见下方建议）",
    "rvc_tf_toast_yellow": "🎙️ 「{name}」已克隆（可优化：{issue}）",
    "rvc_tf_toast_ok": "🎙️ 「{name}」声音已克隆，下次通话即用",
    "rvc_tf_call_with": "📞 和{name}通话",
    "rvc_tf_engine_load": "正在唤醒引擎… {secs}s（首次冷启动约 15~60s）",
    "rvc_tf_engine_connect": "连接语音主机中… {secs}s",
    "rvc_tf_engine_release": "释放显存中… {secs}s",
    "rvc_tf_engine_ready_vram": "已就绪，可接通 · 显存 {gb}GB",
    "rvc_tf_unreachable": "语音主机不可达（检查主机 / 防火墙）— 点「重试」",
    "rvc_tf_engine_idle": "未载入（点右侧「启动引擎」占用显存）",
    "rvc_tf_in_call": "通话中 · {lang}",
    "rvc_tf_mem_toast": "💭 已带入 {n} 条关于 TA 的记忆",
    "rvc_tf_rel_min": "{n}分钟前",
    "rvc_tf_rel_hour": "{n}小时前",
    "rvc_tf_rel_day": "{n}天前",
    "rvc_tf_rel_month": "{n}个月前",
    "rvc_tf_clone_badge": "🎙️已克隆{warn}",
    "rvc_tf_cloned_base": "✅ 已克隆真人声{dur}",
    "rvc_tf_cloned_hint": "{base}{health}（通话即用，可重传替换）",
    "rvc_tf_default_hint": "当前用默认音 · 传 6–15s 干净人声即克隆 TA 的音色（自动转 16k 单声道）",
    "rvc_tf_play_tag_clone": "（🎙️克隆真声{tone}）",
    "rvc_tf_play_tag_edge": "（参考音色）",
    "rvc_tf_http_fail": "操作失败（HTTP {code}）",
}


def _filter_plan():
    plan = json.loads((WORK / "_js_plan.json").read_text(encoding="utf-8"))
    plan = [e for e in plan if e.get("line") not in _SKIP_LINES]
    # 修正 rvc_js_012 整句
    for e in plan:
        if "rvc_js_012" in e.get("new", ""):
            e["new"] = "'<div class=\"p-empty\">'+window.T('rvc_js_012')+'</div>'"
    (WORK / "_js_plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    new = json.loads((WORK / "_js_new.json").read_text(encoding="utf-8"))
    new["rvc_js_012"] = "没有可用人设（检查 config/profiles_runtime.yaml）"
    del new["rvc_js_078"]
    del new["rvc_js_079"]
    del new["rvc_js_080"]
    del new["rvc_js_081"]
    (WORK / "_js_new.json").write_text(json.dumps(new, ensure_ascii=False, indent=1), encoding="utf-8")
    EN["rvc_js_012"] = "No personas available (check config/profiles_runtime.yaml)"


def main():
    jc.TPL = TPL
    jc.KEY_PREFIX = "rvc_js_"
    jc.WORK = WORK
    _filter_plan()
    jc.apply(EN)
    mh.TPL = str(TPL)
    mh.insert_keys(TF_ZH, TF_EN)
    print("voice_call JS i18n applied")


if __name__ == "__main__":
    main()
