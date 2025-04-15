from collections import defaultdict
import sqlite3

class Stats:
    def __init__(self):
        self.conn = sqlite3.connect('stats.db')
        self._create_table()

    def _create_table(self):
        self.conn.execute('''CREATE TABLE IF NOT EXISTS stats
             (source TEXT PRIMARY KEY, 
              total INTEGER, 
              passed INTEGER)''')

    def update(self, source: str, passed: bool):
        self.conn.execute('''INSERT OR IGNORE INTO stats 
            VALUES (?, 0, 0)''', (source,))
        self.conn.execute('''UPDATE stats SET 
            total = total + 1,
            passed = passed + ?
            WHERE source = ?''', (int(passed), source))
        self.conn.commit()

    def get_stats(self):
        cur = self.conn.execute('SELECT * FROM stats')
        return {row[0]: {'total': row[1], 'passed': row[2]} for row in cur}
