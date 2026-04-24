import sqlite3
c = sqlite3.connect('config/messenger_rpa_state.db')
c.execute("DELETE FROM messenger_rpa_meta WHERE k='send_counters_v1'")
c.execute("DELETE FROM messenger_rpa_approvals WHERE run_id='test-suggest'")
c.commit()
print('counters left:', c.execute("SELECT * FROM messenger_rpa_meta WHERE k='send_counters_v1'").fetchall())
print('test approvals left:', c.execute("SELECT id FROM messenger_rpa_approvals WHERE run_id='test-suggest'").fetchall())
c.close()
