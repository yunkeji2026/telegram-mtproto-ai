content = open('src/web/templates/telegram.html', encoding='utf-8').read()
checks = ['预设方案', 'hero-card', 'st-tab', 'cfg-asr-engine', 'cfg-tts-engine',
          'account-info', 'log-tail', 'health-banner', 'tab-dirty']
for c in checks:
    found = "✅ FOUND" if c in content else "❌ MISSING"
    print(f"  {found:12s} {c}")
print(f"\n文件大小: {len(content)} chars, 行数: {content.count(chr(10))}")
# show first h1/h2 heading
import re
headings = re.findall(r'<h[12][^>]*>(.*?)</h[12]>', content[:3000])
print("首部标题:", headings[:3])
