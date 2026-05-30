import sqlite3

conn = sqlite3.connect('config/knowledge_base.db')
cur = conn.cursor()
cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
print('tables:', [r[0] for r in cur.fetchall()])
for t in ['kb_facts', 'kb_drafts', 'kb_categories']:
    try:
        cur.execute(f'SELECT COUNT(*) FROM {t}')
        n = cur.fetchone()[0]
        if n > 0:
            cur.execute(f'SELECT * FROM {t} LIMIT 2')
            rows = cur.fetchall()
            print(f'{t}: {n} rows, sample:', rows)
        else:
            print(f'{t}: 0 rows')
    except Exception as e:
        print(f'{t}: {e}')
conn.close()

# audit bot.db for personas
import sqlite3
conn2 = sqlite3.connect('config/bot.db')
cur2 = conn2.cursor()
cur2.execute("SELECT name FROM sqlite_master WHERE type='table'")
print('\nbot.db tables:', [r[0] for r in cur2.fetchall()])
for t in ['personas', 'persona_bindings']:
    try:
        cur2.execute(f'SELECT COUNT(*) FROM {t}')
        n = cur2.fetchone()[0]
        if n > 0:
            cur2.execute(f'SELECT * FROM {t} LIMIT 3')
            rows = cur2.fetchall()
            print(f'{t}: {n} rows, sample:', rows)
        else:
            print(f'{t}: 0 rows')
    except Exception as e:
        print(f'{t}: {e}')
conn2.close()
