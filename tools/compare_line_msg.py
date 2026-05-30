"""Compare LINE vs Messenger feature coverage."""
import re

line_r = open('src/integrations/line_rpa/runner.py', encoding='utf-8').read()
msg_r  = open('src/integrations/messenger_rpa/runner.py', encoding='utf-8').read()
line_html = open('src/web/templates/line_rpa.html', encoding='utf-8').read()
msg_html  = open('src/web/templates/messenger_rpa.html', encoding='utf-8').read()
line_rt = open('src/web/routes/line_rpa_routes.py', encoding='utf-8').read()
msg_rt  = open('src/web/routes/messenger_rpa_routes.py', encoding='utf-8').read()

print("=== Runner 功能对比 ===")
features = {
    "会话摘要/summary":      ('summary', 'session_summary'),
    "图片OCR/Vision":        ('ocr', 'vision', 'image_description'),
    "语音回复TTS":           ('tts', 'voice_reply', 'voice_sent'),
    "ContactHooks":         ('contact_hooks', 'rpa_hooks', 'ContactHooks'),
    "daily_cap":            ('daily_cap',),
    "backoff/退避":          ('backoff',),
    "状态机/state":          ('state_machine', 'RunnerState', '_state'),
    "health_check":         ('health_check', 'device_health', 'device_unhealthy'),
    "自动接受好友请求":       ('accept_friend', 'accept_request', 'message_request'),
    "proactive主动发消息":   ('proactive', '_proactive'),
    "run_once_target":      ('run_once_target',),
    "sticker贴纸":          ('sticker',),
    "重试retry":             ('retry', '_retry'),
}
for feat, keywords in features.items():
    l_has = any(k in line_r for k in keywords)
    m_has = any(k in msg_r for k in keywords)
    flag = "  ✅" if l_has else "  ❌"
    flag2 = "✅" if m_has else "❌"
    print(f"  {flag} LINE  {flag2} MSG   {feat}")

print("\n=== 路由 API 对比 ===")
line_apis = re.findall(r'@app\.(get|post|put|delete)\("([^"]+)"', line_rt)
msg_apis  = re.findall(r'@app\.(get|post|put|delete)\("([^"]+)"', msg_rt)
line_paths = {p for _, p in line_apis}
msg_paths  = {p for _, p in msg_apis}
print(f"  LINE: {len(line_paths)} endpoints, MSG: {len(msg_paths)} endpoints")

# Key MSG endpoints not in LINE
msg_only_key = [p for p in sorted(msg_paths) if 'messenger' in p or 'rpa' in p]
print(f"\n  MSG unique patterns (sample):")
for p in msg_only_key[:15]:
    print(f"    {p}")

print("\n=== UI 功能对比 ===")
ui_features = {
    "Hero状态卡":           ('hero-card', 'tg-hero', 'rpa-hero'),
    "Sub-tabs分组":         ('st-tab', 'sub-tab'),
    "预设方案":             ('预设方案', 'preset', 'applyPreset'),
    "健康检查横幅":         ('health-banner', 'health_banner'),
    "实时日志":             ('log-tail', 'log_tail', 'logStream'),
    "今日统计":             ('today-stat', 'acc-st-', 'daily-stat'),
    "A/B测试":              ('ab-test', 'variant', 'prompt_variant'),
    "发送速度控制":         ('interval', 'min_interval', 'backoff'),
    "语言强制":             ('force_lang', 'force-lang'),
    "账号信息卡":           ('account-info', 'acc-info'),
    "脏点指示":             ('tab-dot', 'dirty'),
}
for feat, kws in ui_features.items():
    l = any(k in line_html for k in kws)
    m = any(k in msg_html for k in kws)
    print(f"  {'✅' if l else '❌'} LINE  {'✅' if m else '❌'} MSG   {feat}")
