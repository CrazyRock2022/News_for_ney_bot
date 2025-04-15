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
