import os
import logging
import sqlite3
import asyncio
import feedparser
from datetime import datetime, timedelta
from typing import Dict

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.filters import Command, CommandStart
from aiogram import Router
from aiogram.utils.markdown import hbold
from aiogram.enums.dice_emoji import DiceEmoji
from aiogram import types
from aiogram import Bot, Dispatcher
from aiogram import F
from aiogram.types import Message
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from openai import OpenAI
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# Загрузка переменных окружения
load_dotenv()
API_TOKEN = os.getenv("API_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Логирование
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# OpenAI клиент
client = OpenAI(api_key=OPENAI_API_KEY)

# Telegram bot
bot = Bot(token=API_TOKEN, default_parse_mode=ParseMode.HTML)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)
scheduler = AsyncIOScheduler()

# Состояния
class PromptStates(StatesGroup):
    waiting_for_prompt = State()

# БД
DB_PATH = "users.db"
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY)")
    cur.execute("CREATE TABLE IF NOT EXISTS sent_links (link TEXT PRIMARY KEY)")
    cur.execute("CREATE TABLE IF NOT EXISTS sources (url TEXT PRIMARY KEY)")
    conn.commit()
    cur.execute("SELECT COUNT(*) FROM sources")
    if cur.fetchone()[0] == 0:
        sources = [
            'https://forklog.com/feed/',
            'https://ru.cointelegraph.com/rss',
            'https://bits.media/rss/news/',
            'https://incrypted.com/feed/',
            'https://cryptopanic.com/news/rss/',
            'https://cointelegraph.com/rss',
            'https://decrypt.co/feed',
            'https://www.coindesk.com/arc/outboundfeeds/rss/?outputType=xml',
            'https://www.cbr.ru/rss/',
            'http://www.finmarket.ru/rss/',
            'https://rssexport.rbc.ru/rbcnews/news/eco/index.rss',
            'https://www.kommersant.ru/RSS/news.xml',
            'https://www.forbes.ru/rss',
            'https://24.kg/rss/',
            'https://akipress.org/rss/news.rss',
            'https://www.themoscowtimes.com/rss',
            'https://blogs.imf.org/feed/',
            'https://www.bis.org/rss/home.xml'
        ]
        cur.executemany("INSERT OR IGNORE INTO sources (url) VALUES (?)", [(s,) for s in sources])
    conn.commit()
    conn.close()

# ---------- GPT-анализ ----------
async def gpt_check(prompt: str) -> str:
    try:
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None,
            lambda: client.chat.completions.create(
                model="gpt-3.5-turbo",  # Или "gpt-4"
                messages=[{"role": "user", "content": prompt}],
                max_tokens=5,
                temperature=0
            )
        )
        return response.choices[0].message.content.strip().lower()
    except Exception as e:
        logging.error(f"OpenAI API error: {e}", exc_info=True)
        return "нет"

# ---------- Команды ----------
@router.message(CommandStart())
async def start_cmd(message: Message):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO users (id) VALUES (?)", (message.from_user.id,))
    conn.commit()
    conn.close()
    await message.answer(
        "Привет! Я бот, который находит релевантные новости. Используй команду /digest чтобы начать.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Справка", callback_data="help")]
        ])
    )

@router.callback_query(F.data == "help")
async def help_callback(call: types.CallbackQuery):
    await call.message.edit_text("Используй:\n/digest — анализ новостей\n/help — справка\n/addsource <url>\n/removesource <url>\n/listsources")

@router.message(Command("help"))
async def help_cmd(message: Message):
    await message.answer("Команды:\n/digest — анализ новостей\n/addsource <url>\n/removesource <url>\n/listsources")

@router.message(Command("listsources"))
async def list_sources(message: Message):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT url FROM sources")
    sources = cur.fetchall()
    conn.close()
    await message.answer("\n".join(s[0] for s in sources))

@router.message(Command("addsource"))
async def add_source(message: Message):
    parts = message.text.split()
    if len(parts) != 2:
        return await message.answer("Использование: /addsource <url>")
    url = parts[1]
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO sources (url) VALUES (?)", (url,))
    conn.commit()
    conn.close()
    await message.answer(f"Источник добавлен: {url}")

@router.message(Command("removesource"))
async def remove_source(message: Message):
    parts = message.text.split()
    if len(parts) != 2:
        return await message.answer("Использование: /removesource <url>")
    url = parts[1]
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM sources WHERE url=?", (url,))
    conn.commit()
    conn.close()
    await message.answer(f"Источник удалён: {url}")

# ---------- Обработка digest ----------
@router.message(Command("digest"))
async def handle_digest(message: Message, state: FSMContext):
    await message.answer("Введи промт для поиска релевантных новостей:")
    await state.set_state(PromptStates.waiting_for_prompt)

@router.message(PromptStates.waiting_for_prompt)
async def handle_user_prompt(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Промт принят. Начинаю анализ новостей...")
    await analyze_and_send_news(message, message.text)

async def analyze_and_send_news(message: Message, user_prompt: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT url FROM sources")
    sources = [row[0] for row in cur.fetchall()]
    conn.close()

    total = 0
    relevant = 0
    stats = []

    for url in sources:
        feed = feedparser.parse(url)
        count = 0
        rel = 0
        for entry in feed.entries:
            title = entry.get("title", "")
            summary = entry.get("summary", "")
            link = entry.get("link", "")
            full = f"{title}\n\n{summary}"
            if not link or not title:
                continue
            if datetime.utcnow() - datetime(*entry.published_parsed[:6]) > timedelta(days=7):
                continue
            total += 1
            count += 1
            result = await gpt_check(f"{user_prompt}\n\n{full}")
            if "да" in result:
                relevant += 1
                rel += 1
                await message.answer(f"<b>{title}</b>\n{link}", parse_mode=ParseMode.HTML)
        stats.append(f"{url} — {count} новостей, из них релевантных: {rel}")

    summary = "\n\n".join(stats)
    await message.answer(f"Итого: {total} новостей, из них релевантных: {relevant}\n\n{summary}")

# ---------- Старт ----------
async def main():
    init_db()
    scheduler.add_job(lambda: None, "cron", hour=11)
    scheduler.start()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())