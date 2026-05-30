"""S4: Add persona_ids to LINE accounts in config.yaml."""
import re

with open('config/config.yaml', encoding='utf-8') as f:
    content = f.read()

# Insert persona_ids: [lin_jiaxin] after line_ij8 account block's 'enabled: true'
# and persona_ids: [zhang_jingguang] after line_xw8's 'enabled: true'

def insert_after_enabled(content, account_id, persona_id):
    # Pattern: match the account block starting with '- account_id: <id>'
    # then find 'enabled: true' within the next ~10 lines of that account
    pattern = (
        r'(- account_id: ' + re.escape(account_id) + r'\n'
        r'(?:.*\n){0,8}?'
        r'    enabled: true)(\n)'
    )
    replacement = r'\1\n    persona_ids: [' + persona_id + r']\2'
    new_content, n = re.subn(pattern, replacement, content)
    if n == 0:
        print(f"WARNING: no match for {account_id}")
    else:
        print(f"OK: {account_id} -> {persona_id}")
    return new_content

content = insert_after_enabled(content, 'line_ij8', 'lin_jiaxin')
content = insert_after_enabled(content, 'line_xw8', 'zhang_jingguang')

with open('config/config.yaml', 'w', encoding='utf-8') as f:
    f.write(content)

print("Done. Verifying...")
for account_id in ('line_ij8', 'line_xw8'):
    import re as _re
    m = _re.search(rf'account_id: {account_id}.*?persona_ids.*?\n', content, _re.DOTALL)
    if m:
        print(f"  {account_id}: found persona_ids")
    else:
        print(f"  {account_id}: MISSING persona_ids!")
