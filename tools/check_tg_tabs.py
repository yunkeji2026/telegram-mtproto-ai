import re
content = open('src/web/templates/telegram.html', encoding='utf-8').read()
ids = re.findall(r'id="(st-[^"]+)"', content)
print("Tab/pane IDs:", ids)
funcs = re.findall(r'function (\w+)\(', content)
print("JS functions:", funcs[:30])
# Count lines with cfg-
cfg_lines = [l.strip()[:80] for l in content.split('\n') if 'id="cfg-' in l]
print(f"\nFields with id=cfg-* ({len(cfg_lines)}):")
for l in cfg_lines[:20]:
    print(" ", l)
