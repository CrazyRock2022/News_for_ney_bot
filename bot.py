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

API_TOKEN = os.getenv("API_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai.api_key = OPENAI_API_KEY

logging.basicConfig(level=logging.INFO)
bot = Bot(token=API_TOKEN)
dp = Dispatcher(bot)
scheduler = AsyncIOScheduler()
DB_FILE = "users.db"

# Храним кастомный промт в памяти
user_prompt: dict[int, str] = {}

# ---------- БАЗА ДАННЫХ ----------
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY)")
    cur.execute("CREATE TABLE IF NOT EXISTS sent_links (link TEXT PRIMARY KEY)")
    cur.execute("CREATE TABLE IF NOT EXISTS sources (url TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()

# ---------- СТАРТ ----------
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
    await message.answer("Привет! Я бот проекта A7A5. Нажми «/digest», чтобы получить свежие релевантные новости.", reply_markup=keyboard)

# ---------- СПРАВКА ----------
HELP_TEXT = (
    "/digest — получить новости\n"
    "/addsource <url> — добавить источник\n"
    "/removesource <url> — удалить источник\n"
    "/listsources — список всех источников\n"
    "/help — справка\n"
)

@dp.callback_query_handler(lambda c: c.data == "help")
async def help_callback(callback: types.CallbackQuery):
    await callback.message.edit_text(HELP_TEXT)

@dp.message_handler(commands=["help"])
async def help_command(message: types.Message):
    await message.answer(HELP_TEXT)

# ---------- ДОБАВИТЬ ИСТОЧНИК ----------
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

# ---------- УДАЛИТЬ ИСТОЧНИК ----------
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

# ---------- ПОКАЗАТЬ ИСТОЧНИКИ ----------
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

# ---------- /DIGEST ----------
@dp.message_handler(commands=["digest"])
async def send_digest(message: types.Message):
    articles, stats = await get_news(user_id=message.from_user.id)
    if articles:
        summary = "Результаты анализа:\n\n"
        total = {"Relevant": 0, "Possible": 0, "Irrelevant": 0}
        for source, s in stats.items():
            summary += f"{source} — {s['total']} новостей, релевантных: {s['Relevant']}, возможно: {s['Possible']}, не подходят: {s['Irrelevant']}\n"
            for k in total:
                total[k] += s[k]
        summary += f"\nВсего: {sum(total.values())} новостей — релевантных: {total['Relevant']}, возможно: {total['Possible']}, не подходят: {total['Irrelevant']}"

        keyboard = InlineKeyboardMarkup().add(
            InlineKeyboardButton("Получить релевантные", callback_data="get_news")
        )
        await message.answer(summary, reply_markup=keyboard)
    else:
        await message.answer("Нет релевантных новостей за неделю.\n\nВведите свой запрос для анализа:")
        user_prompt[message.from_user.id] = "__awaiting__"

# ---------- ОБРАБОТКА ВВОДА ПРОМТА ----------
@dp.message_handler(lambda msg: user_prompt.get(msg.from_user.id) == "__awaiting__")
async def handle_custom_prompt(message: types.Message):
    user_prompt[message.from_user.id] = message.text.strip()
    await message.answer(f"Принято! Теперь буду искать по смыслу: «{message.text.strip()}».\nНажмите /digest")

# ---------- КНОПКА ----------
@dp.callback_query_handler(lambda c: c.data == "get_news")
async def get_news_callback(callback: types.CallbackQuery):
    articles, _ = await get_news(user_id=callback.from_user.id)
    relevant = [a for a in articles if a['status'] == 'Relevant']
    if not relevant:
        return await callback.message.answer("Релевантных новостей не найдено.")
    for art in relevant:
        await callback.message.answer(f"<b>{art['title']}</b>\n{art['link']}", parse_mode=ParseMode.HTML)

# ---------- GPT-ФИЛЬТР ----------
async def is_relevant(title, summary, user_prompt_text):
    full_context = f"Заголовок: {title}\nОписание: {summary}"
    prompt = (
        f"Ты аналитик проекта A7A5. Определи по смыслу, может ли эта новость быть релевантной.\n"
        f"{'Контекст: ' + user_prompt_text if user_prompt_text else 'Ищи темы: криптовалюты, CBDC, цифровой рубль, экономика Кыргызстана.'}\n\n"
        f"{full_context}\n\nОтветь одним словом: Да или Нет."
    )
    try:
        response = await openai.ChatCompletion.acreate(
            model="gpt-4",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=3
        )
        result = response.choices[0].message.content.strip().lower()
        return result == "да"
    except Exception as e:
        logging.error(f"OpenAI error: {e}")
        return None

# ---------- ПАРСИНГ НОВОСТЕЙ ----------
async def get_news(user_id):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT url FROM sources")
    urls = [r[0] for r in cur.fetchall()]
    articles = []
    stats = {}

    user_custom_prompt = user_prompt.get(user_id, "")

    for url in urls:
        stats[url] = {"Relevant": 0, "Possible": 0, "Irrelevant": 0, "total": 0}
        feed = feedparser.parse(url)
        for entry in feed.entries:
            title = entry.title
            link = entry.link
            summary = entry.get("summary", "")
            published = entry.get("published_parsed")
            if published:
                pub_time = datetime(*published[:6])
                if datetime.utcnow() - pub_time > timedelta(days=7):
                    continue
            cur.execute("SELECT 1 FROM sent_links WHERE link=?", (link,))
            if cur.fetchone():
                continue
            cur.execute("INSERT INTO sent_links (link) VALUES (?)", (link,))
            stats[url]["total"] += 1

            relevant = await is_relevant(title, summary, user_custom_prompt)
            if relevant is True:
                status = "Relevant"
            elif relevant is None:
                status = "Possible"
            else:
                status = "Irrelevant"

            stats[url][status] += 1
            articles.append({"title": title, "link": link, "status": status, "source": url})
    conn.commit()
    conn.close()
    return articles, stats

# ---------- ПЛАНИРОВЩИК ----------
async def scheduled_job():
    pass  # Здесь может быть авторассылка

async def on_startup(_):
    init_db()
    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_webhook(url="")
    scheduler.add_job(scheduled_job, "cron", hour=11)
    scheduler.start()

executor.start_polling(dp, skip_updates=True, on_startup=on_startup)
