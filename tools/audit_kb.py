"""S3 审计：列出 KB / persona 现状。"""
import pathlib, sqlite3

for db in list(pathlib.Path(".").glob("*.db")) + list(pathlib.Path("data").glob("*.db") if pathlib.Path("data").exists() else []):
    try:
        conn = sqlite3.connect(str(db))
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [r[0] for r in cur.fetchall()]
        print(f"\n=== {db} ===  tables: {tables}")
        for t in tables:
            cur.execute(f'SELECT COUNT(*) FROM "{t}"')
            n = cur.fetchone()[0]
            if n > 0:
                cur.execute(f'SELECT * FROM "{t}" LIMIT 1')
                sample = cur.fetchone()
                print(f"  {t}: {n} rows, sample={str(sample)[:120]}")
            else:
                print(f"  {t}: 0 rows")
        conn.close()
    except Exception as e:
        print(f"  ERR {db}: {e}")

import pathlib
persona_dir = pathlib.Path("personas")
if persona_dir.exists():
    for f in persona_dir.glob("**/*.yaml"):
        print(f"\nPERSONA FILE: {f}")
        print(f.read_text(encoding="utf-8")[:400])
        print("---")
