import json
import yaml
from pathlib import Path
import sqlite3

class StatsManager:
    def __init__(self):
        self.conn = sqlite3.connect('stats.db')
        self._create_table()

    def _create_table(self):
        with self.conn:
            self.conn.execute('''CREATE TABLE IF NOT EXISTS stats
                (source TEXT PRIMARY KEY, total INTEGER, passed INTEGER)''')

    def update(self, source: str, passed: bool):
        with self.conn:
            self.conn.execute('''INSERT OR IGNORE INTO stats VALUES (?, 0, 0)''', (source,))
            self.conn.execute('''UPDATE stats SET total = total + 1, passed = passed + ? 
                              WHERE source = ?''', (int(passed), source))

    def get_paginated(self, page: int, per_page=5):
        offset = (page - 1) * per_page
        with self.conn:
            cur = self.conn.execute('SELECT * FROM stats LIMIT ? OFFSET ?', (per_page, offset))
            total = self.conn.execute('SELECT COUNT(*) FROM stats').fetchone()[0]
            
        return (
            {row[0]: {'total': row[1], 'passed': row[2]} for row in cur},
            (total + per_page - 1) // per_page
        )

class SourceManager:
    def __init__(self):
        self.sources_file = Path("sources.json")
        self._init_file()

    def _init_file(self):
        if not self.sources_file.exists():
            self.sources_file.write_text('["https://forklog.com/feed/"]')

    def get_sources(self):
        return json.loads(self.sources_file.read_text())

    def add_source(self, url: str):
        sources = self.get_sources()
        sources.append(url)
        self.sources_file.write_text(json.dumps(sources))

class KeywordManager:
    def __init__(self):
        self.keywords_file = Path("keywords.yaml")
        self._init_file()

    def _init_file(self):
        if not self.keywords_file.exists():
            self.keywords_file.write_text(yaml.dump(["крипта", "биткоин"]))

    def get_keywords(self):
        return yaml.safe_load(self.keywords_file.read_text())

    def add_keyword(self, keyword: str):
        keywords = self.get_keywords()
        keywords.append(keyword)
        self.keywords_file.write_text(yaml.dump(keywords))
import feedparser
from cachetools import TTLCache
from datetime import datetime, timedelta

class CacheManager:
    def __init__(self, ttl=300):
        self.cache = TTLCache(maxsize=100, ttl=ttl)

    def get(self, key):
        return self.cache.get(key)

    def set(self, key, value):
        self.cache[key] = value

class RateLimiter:
    def __init__(self, max_requests, period):
        self.requests = {}
        self.max_requests = max_requests
        self.period = timedelta(seconds=period)

    async def check_limit(self, handler, event, data):
        user_id = event.from_user.id
        now = datetime.now()
        
        if user_id not in self.requests:
            self.requests[user_id] = []
        
        # Удаляем старые запросы
        self.requests[user_id] = [
            t for t in self.requests[user_id]
            if now - t < self.period
        ]
        
        if len(self.requests[user_id]) >= self.max_requests:
            await event.answer("Превышен лимит запросов. Попробуйте позже.")
            return False
        
        self.requests[user_id].append(now)
        return True

class FeedValidator:
    def is_valid_rss(self, url):
        try:
            feed = feedparser.parse(url)
            return len(feed.entries) > 0
        except:
            return False
