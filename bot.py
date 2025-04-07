import logging
import asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.types import ParseMode
from aiogram.utils import executor
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiogram.dispatcher.filters import CommandStart
import feedparser
import sqlite3
from datetime import datetime, timedelta
import openai
import os

API_TOKEN = '7790214437:AAE1rt9jfsQ76RLlyvOuZvnFqHUIE2EbmyA'  # замени на свой токен
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

openai.api_key = OPENAI_API_KEY

logging.basicConfig(level=logging.INFO)
bot = Bot(token=API_TOKEN)
dp = Dispatcher(bot)

scheduler = AsyncIOScheduler()
DB_FILE = "users.db"

RSS_FEEDS = [
    'https://forklog.com/feed/',
    'https://ru.cointelegraph.com/rss',
    'https://rssexport.rbc.ru/rbcnews/news/eco/index.rss',
    'https://akipress.org/rss/news.rss',
    'https://24.kg/rss/'
]

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY)")
    cur.execute("CREATE TABLE IF NOT EXISTS sent_links (link TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()

@dp.message_handler(CommandStart())
async def start(message: types.Message):
    user_id = message.from_user.id
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO users (id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()
    await message.answer("Привет! Я буду присылать тебе только релевантные новости по теме A7A5, крипты и цифрового рубля.")

@dp.message_handler(commands=["digest"])
async def send_digest(message: types.Message):
    articles = await get_news()
    if articles:
        for a in articles:
            await message.answer(f"<b>{a['title']}</b>\n{a['link']}", parse_mode=ParseMode.HTML)
    else:
        await message.answer("Пока нет свежих релевантных новостей.")

async def is_relevant(title, summary):
    prompt = f"Определи, относится ли следующая новость к криптовалютам, цифровому рублю, стейблкоинам, крипте в Кыргызстане или проекту A7A5:\n\n'{title} — {summary}'\n\nОтветь одним словом: Да или Нет."
    try:
        response = await openai.ChatCompletion.acreate(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=3
        )
        answer = response.choices[0].message['content'].strip().lower()
        logging.info(f"GPT ответ: {answer} — {'релевантно' if 'да' in answer else 'нет'}")
        return "да" in answer
    except Exception as e:
        logging.warning(f"OpenAI error: {e}")
        return False

async def get_news():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    new_articles = []
    for url in RSS_FEEDS:
        feed = feedparser.parse(url)
        for entry in feed.entries:
            title = entry.title
            link = entry.link
            summary = entry.get("summary", "")
            published = entry.get("published_parsed")
            if published:
                pub_time = datetime(*published[:6])
                if datetime.utcnow() - pub_time > timedelta(hours=24):
                    continue
            cur.execute("SELECT 1 FROM sent_links WHERE link=?", (link,))
            if not cur.fetchone():
                relevant = await is_relevant(title, summary)
                if relevant:
                    cur.execute("INSERT INTO sent_links (link) VALUES (?)", (link,))
                    new_articles.append({'title': title, 'link': link})
    conn.commit()
    conn.close()
    return new_articles

async def scheduled_job():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT id FROM users")
    users = cur.fetchall()
    conn.close()
    articles = await get_news()
    if not articles:
        return
    for user in users:
        for a in articles:
            try:
                await bot.send_message(user[0], f"<b>{a['title']}</b>\n{a['link']}", parse_mode=ParseMode.HTML)
            except Exception as e:
                logging.warning(f"Failed to send to {user[0]}: {e}")

async def on_startup(_):
    init_db()
    scheduler.add_job(scheduled_job, "cron", hour=11, minute=0)
    scheduler.start()

if __name__ == '__main__':
    init_db()
    executor.start_polling(dp, skip_updates=True, on_startup=on_startup)
