"""S3: 清空知识库中的客服内容，保留结构表。"""
import sqlite3
import shutil
from datetime import datetime
from pathlib import Path

DB = Path("config/knowledge_base.db")
BAK = Path(f"config/knowledge_base.db.bak_s3_{datetime.now().strftime('%Y%m%d_%H%M%S')}")

# 1. backup
shutil.copy2(DB, BAK)
print(f"Backed up to {BAK}")

conn = sqlite3.connect(str(DB))
cur = conn.cursor()

# 2. Show counts before
tables = ['kb_entries', 'kb_rules', 'kb_examples', 'kb_drafts',
          'kb_translations', 'kb_error_codes', 'kb_feedback',
          'kb_miss_log', 'kb_entry_versions', 'kb_query_log',
          'kb_entry_images', 'kb_meta']
print("\nBefore:")
for t in tables:
    try:
        cur.execute(f'SELECT COUNT(*) FROM {t}')
        print(f"  {t}: {cur.fetchone()[0]}")
    except Exception as e:
        print(f"  {t}: {e}")

# 3. Clear customer-service content tables (keep analytics: miss_log, query_log, feedback)
cs_tables = ['kb_entries', 'kb_rules', 'kb_examples', 'kb_drafts',
             'kb_translations', 'kb_error_codes', 'kb_entry_versions',
             'kb_entry_images']
for t in cs_tables:
    try:
        cur.execute(f'DELETE FROM {t}')
        print(f"Cleared {t}: {cur.rowcount} rows deleted")
    except Exception as e:
        print(f"  SKIP {t}: {e}")

conn.commit()
conn.close()
print("\nDone. KB customer-service content cleared.")
