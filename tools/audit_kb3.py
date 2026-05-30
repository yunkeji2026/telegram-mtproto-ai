import sqlite3, pathlib

conn = sqlite3.connect('config/knowledge_base.db')
cur = conn.cursor()
for t in ['kb_entries', 'kb_rules', 'kb_examples', 'kb_drafts']:
    cur.execute(f'SELECT COUNT(*) FROM {t}')
    n = cur.fetchone()[0]
    print(f'{t}: {n} rows')
    if 0 < n <= 5:
        cur.execute(f'SELECT * FROM {t}')
        for row in cur.fetchall():
            print(' ', str(row)[:200])
conn.close()

# Find persona YAML/JSON files
print("\n--- Persona files ---")
for ext in ['*.yaml', '*.yml', '*.json']:
    for f in pathlib.Path('.').rglob(ext):
        if 'persona' in f.name.lower() or 'persona' in str(f.parent).lower():
            print(f)

# grep for 客服 in config
print("\n--- config.yaml persona/skill refs ---")
cfg = pathlib.Path('config/config.yaml')
if cfg.exists():
    for i, line in enumerate(cfg.read_text(encoding='utf-8').splitlines(), 1):
        if any(kw in line for kw in ['persona', 'skill', 'kefu', '客服', 'customer_service']):
            print(f'  L{i}: {line.rstrip()}')
