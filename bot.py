import logging
import asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.types import ParseMode, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils import executor
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiogram.dispatcher.filters import CommandStart
import feedparser
import sqlite3
from datetime import datetime, timedelta
import openai
import os

API_TOKEN = os.getenv("API_TOKEN")  # замените на свой
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai.api_key = OPENAI_API_KEY

logging.basicConfig(level=logging.INFO)
bot = Bot(token=API_TOKEN)
dp = Dispatcher(bot)
scheduler = AsyncIOScheduler()
DB_FILE = "users.db"

# ---------- ИНИЦИАЛИЗАЦИЯ БД ----------

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY)")
    cur.execute("CREATE TABLE IF NOT EXISTS sent_links (link TEXT PRIMARY KEY)")
    cur.execute("CREATE TABLE IF NOT EXISTS sources (url TEXT PRIMARY KEY)")
    cur.execute("CREATE TABLE IF NOT EXISTS topics (word TEXT PRIMARY KEY)")
    # начальные источники
    default_sources = [
        'https://forklog.com/feed/',
        'https://ru.cointelegraph.com/rss',
        'https://rssexport.rbc.ru/rbcnews/news/eco/index.rss',
        'https://akipress.org/rss/news.rss',
        'https://24.kg/rss/'
    ]
    for url in default_sources:
        cur.execute("INSERT OR IGNORE INTO sources (url) VALUES (?)", (url,))
    # начальные темы
    default_topics = ['A7A5', 'Кыргызстан', 'крипта', 'цифровой рубль', 'стейблкоин', 'CBDC']
    for word in default_topics:
        cur.execute("INSERT OR IGNORE INTO topics (word) VALUES (?)", (word,))
    conn.commit()
    conn.close()

# ---------- КОМАНДЫ ----------

@dp.message_handler(CommandStart())
async def start(message: types.Message):
    user_id = message.from_user.id
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO users (id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()

    keyboard = InlineKeyboardMarkup().add(
        InlineKeyboardButton("Справка", callback_data="help")
    )
    await message.answer("Привет! Я пришлю тебе свежие и релевантные новости по теме A7A5, крипты и цифрового рубля.", reply_markup=keyboard)

@dp.callback_query_handler(lambda c: c.data == "help")
@dp.message_handler(commands=["help"])
async def help_command(message: types.Message | types.CallbackQuery):
    text = (
        "Вот что я умею:\n\n"
        "/digest — получить свежие релевантные новости\n"
        "/addsource <url> — добавить сайт/RSS источник\n"
        "/removesource <url> — удалить источник\n"
        "/listsources — показать все источники\n"
        "/addtopic <тема> — добавить ключевое слово\n"
        "/removetopic <тема> — удалить ключевое слово\n"
        "/listtopics — показать все ключевые слова\n"
        "/help — справка\n"
    )
    if isinstance(message, types.CallbackQuery):
        await message.message.edit_text(text)
    else:
        await message.answer(text)

@dp.message_handler(commands=["digest"])
async def send_digest(message: types.Message):
    articles = await get_news()
    if articles:
        for a in articles:
            await message.answer(f"<b>{a['title']}</b>\n{a['link']}", parse_mode=ParseMode.HTML)
    else:
        await message.answer("Пока нет свежих релевантных новостей.")

# ---------- ИСТОЧНИКИ ----------

@dp.message_handler(commands=["addsource"])
async def add_source(message: types.Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        return await message.reply("Укажи ссылку: /addsource <url>")
    url = parts[1]
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO sources (url) VALUES (?)", (url,))
    conn.commit()
    conn.close()
    await message.reply("Источник добавлен!")

@dp.message_handler(commands=["removesource"])
async def remove_source(message: types.Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        return await message.reply("Укажи ссылку: /removesource <url>")
    url = parts[1]
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("DELETE FROM sources WHERE url = ?", (url,))
    conn.commit()
    conn.close()
    await message.reply("Источник удалён.")

@dp.message_handler(commands=["listsources"])
async def list_sources(message: types.Message):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT url FROM sources")
    rows = cur.fetchall()
    conn.close()
    if not rows:
        return await message.reply("Источников пока нет.")
    sources = "\n".join(f"- {r[0]}" for r in rows)
    await message.reply(f"Текущие источники:\n{sources}")

# ---------- ТЕМЫ ----------

@dp.message_handler(commands=["addtopic"])
async def add_topic(message: types.Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        return await message.reply("Укажи тему: /addtopic <слово>")
    word = parts[1].lower()
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO topics (word) VALUES (?)", (word,))
    conn.commit()
    conn.close()
    await message.reply("Тема добавлена!")

@dp.message_handler(commands=["removetopic"])
async def remove_topic(message: types.Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        return await message.reply("Укажи тему: /removetopic <слово>")
    word = parts[1].lower()
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("DELETE FROM topics WHERE word = ?", (word,))
    conn.commit()
    conn.close()
    await message.reply("Тема удалена.")

@dp.message_handler(commands=["listtopics"])
async def list_topics(message: types.Message):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT word FROM topics")
    rows = cur.fetchall()
    conn.close()
    if not rows:
        return await message.reply("Тем пока нет.")
    topics = ", ".join([r[0] for r in rows])
    await message.reply(f"Текущие темы: {topics}")

# ---------- GPT-ФИЛЬТР ----------

async def is_relevant(title, summary, tags=None, category=None, content=None):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT word FROM topics")
    keywords = [row[0] for row in cur.fetchall()]
    conn.close()

    topic_list = ", ".join(keywords)

    full_context = f"Заголовок: {title}\nОписание: {summary}"

    if category:
        full_context += f"\nКатегория: {category}"
    if tags:
        full_context += f"\nТеги: {', '.join(tags)}"
    if content:
        full_context += f"\nПолный текст: {content[:1000]}..."  # ограничим до 1000 символов

    prompt = (
        f"Определи, относится ли следующая новость к темам: {topic_list}.\n\n"
        f"{full_context}\n\n"
        f"Ответь одним словом: Да или Нет."
    )

    try:
        response = await openai.ChatCompletion.acreate(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=3
        )
        answer = response.choices[0].message['content'].strip().lower()
        logging.info(f"GPT: {answer} -> {'релевантно' if 'да' in answer else 'нет'}")
        return "да" in answer
    except Exception as e:
        logging.warning(f"OpenAI error: {e}")
        return False

# ---------- ПАРСИНГ ----------

async def get_news():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT url FROM sources")
    sources = [r[0] for r in cur.fetchall()]
    new_articles = []

    for url in sources:
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
                tags = [t['term'] for t in entry.get('tags', [])] if 'tags' in entry else []
                category = entry.get('category')
                content = entry.get('content', [{}])[0].get('value') if 'content' in entry else None

                if await is_relevant(title, summary, tags, category, content):
                    cur.execute("INSERT INTO sent_links (link) VALUES (?)", (link,))
                    new_articles.append({'title': title, 'link': link})

    conn.commit()
    conn.close()
    return new_articles

# ---------- РАССЫЛКА ----------

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
                logging.warning(f"Send error: {e}")

# ---------- СТАРТ ----------

async def on_startup(_):
    init_db()
    await bot.delete_webhook(drop_pending_updates=True)  # вот эта строка сбрасывает Webhook
    scheduler.add_job(scheduled_job, "cron", hour=11, minute=0)
    scheduler.start()
if __name__ == "__main__":
    init_db()
    executor.start_polling(dp, skip_updates=True, on_startup=on_startup)
