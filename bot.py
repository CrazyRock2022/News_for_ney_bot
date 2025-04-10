import logging
import asyncio
import feedparser
import sqlite3
import openai
import os
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, F
from aiogram.enums.parse_mode import ParseMode
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.markdown import hbold
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.filters import Command, CommandStart
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ------------ КОНФИГ ------------
API_TOKEN = os.getenv("API_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai.api_key = OPENAI_API_KEY

logging.basicConfig(level=logging.INFO)
bot = Bot(token=API_TOKEN, default=parse_mode=ParseMode.HTML)
dp = Dispatcher(storage=MemoryStorage())
scheduler = AsyncIOScheduler()
DB_FILE = "users.db"

DEFAULT_PROMPT = (
    "Ты аналитик криптовалютного проекта A7A5. "
    "Проанализируй новость и ответь: может ли она быть потенциально релевантной проекту A7A5, "
    "если она касается криптовалют, стейблкоинов, цифрового рубля, экономики Кыргызстана, "
    "финансовых регуляторов или мировой криптоинфраструктуры? Ответь одним словом: Да или Нет."
)

# ------------ FSM ------------
class UserState(StatesGroup):
    awaiting_prompt = State()

# ------------ ИНИЦИАЛИЗАЦИЯ БД ------------
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY)")
    cur.execute("CREATE TABLE IF NOT EXISTS sent_links (link TEXT PRIMARY KEY)")
    cur.execute("CREATE TABLE IF NOT EXISTS sources (url TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()

# ------------ КОМАНДЫ ------------
@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    user_id = message.from_user.id
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO users (id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()

    await message.answer(
        f"{hbold('Привет!')} Я пришлю тебе релевантные новости по теме A7A5.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Справка", callback_data="help")]
        ])
    )

@dp.callback_query(F.data == "help")
async def help_callback(callback):
    await callback.message.edit_text(
        "/digest — свежие новости\n"
        "/addsource <url> — добавить источник\n"
        "/removesource <url> — удалить источник\n"
        "/listsources — список источников\n"
        "/help — справка"
    )

@dp.message(Command("help"))
async def help_text(msg: Message):
    await msg.answer(
        "/digest — свежие новости\n"
        "/addsource <url> — добавить источник\n"
        "/removesource <url> — удалить источник\n"
        "/listsources — список источников\n"
        "/help — справка"
    )

@dp.message(Command("addsource"))
async def add_source(msg: Message):
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        return await msg.reply("Укажи ссылку: /addsource <url>")
    url = parts[1]
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO sources (url) VALUES (?)", (url,))
    conn.commit()
    conn.close()
    await msg.reply("Источник добавлен.")

@dp.message(Command("removesource"))
async def remove_source(msg: Message):
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        return await msg.reply("Укажи ссылку: /removesource <url>")
    url = parts[1]
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("DELETE FROM sources WHERE url = ?", (url,))
    conn.commit()
    conn.close()
    await msg.reply("Источник удалён.")

@dp.message(Command("listsources"))
async def list_sources(msg: Message):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT url FROM sources")
    rows = cur.fetchall()
    conn.close()
    if not rows:
        return await msg.reply("Источников пока нет.")
    await msg.reply("\n".join(f"- {r[0]}" for r in rows))

# ------------ GPT ФИЛЬТР ------------
async def gpt_check(prompt: str) -> str:
    try:
        response = await openai.ChatCompletion.acreate(
            model="gpt-4",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=5
        )
        return response.choices[0].message.content.strip().lower()
    except Exception as e:
        logging.error(f"OpenAI error: {e}")
        return "нет"

# ------------ FETCH NEWS ------------
async def fetch_news(prompt: str):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT url FROM sources")
    sources = [r[0] for r in cur.fetchall()]
    conn.close()
    articles = []

    for url in sources:
        feed = feedparser.parse(url)
        for entry in feed.entries:
            title = entry.title
            link = entry.link
            summary = entry.get("summary", "")
            category = entry.get("category", "")
            tags = [t.get("term") for t in entry.get("tags", [])] if "tags" in entry else []
            content = entry.get("content", [{}])[0].get("value", "")
            full = f"Заголовок: {title}\nОписание: {summary}\nКатегория: {category}\nТеги: {', '.join(tags)}\nКонтент: {content}"
            answer = await gpt_check(f"{prompt}\n\n{full}\n\nДа или Нет?")
            if "да" in answer:
                articles.append(f"<b>{title}</b>\n{link}")
    return articles

# ------------ /digest ------------
class UserState(StatesGroup):
    awaiting_prompt = State()

@dp.message(Command("digest"))
async def digest_command(message: Message, state: FSMContext):
    await message.answer("Введите промт для анализа новостей:")
    await state.set_state(UserState.awaiting_prompt)

@dp.message(UserState.awaiting_prompt)
async def handle_user_prompt(message: Message, state: FSMContext):
    prompt = message.text.strip()
    await message.answer("Ищу релевантные новости...")
    results = await fetch_news(prompt)
    if results:
        for article in results:
            await message.answer(article)
    else:
        await message.answer("Релевантных новостей не найдено.")
    await state.clear()

# ------------ ПЛАНИРОВЩИК ------------
async def scheduled_job():
    logging.info("Scheduled job running...")

# ------------ СТАРТ ------------
async def main():
    init_db()
    await bot.delete_webhook(drop_pending_updates=True)
    scheduler.add_job(scheduled_job, "cron", hour=11, minute=0)
    scheduler.start()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
