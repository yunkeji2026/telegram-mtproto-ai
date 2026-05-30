import sqlite3
conn = sqlite3.connect('config/knowledge_base.db')
cur = conn.cursor()
cur.execute("SELECT * FROM kb_meta")
print("kb_meta:", cur.fetchall())
for t in ['kb_entries', 'kb_rules', 'kb_error_codes', 'kb_drafts']:
    cur.execute(f'SELECT COUNT(*) FROM {t}')
    print(f'{t}: {cur.fetchone()[0]}')
conn.close()
