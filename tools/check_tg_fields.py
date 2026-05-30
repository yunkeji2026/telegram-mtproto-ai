import re
content = open('src/web/templates/telegram.html', encoding='utf-8').read()
all_ids = re.findall(r'id="([^"]+)"', content)
print(f"All element IDs ({len(all_ids)}):")
for i in all_ids:
    print(" ", i)
