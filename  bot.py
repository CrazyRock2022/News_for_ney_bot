import os
import logging
import asyncio
import sqlite3
import feedparser
from datetime import datetime, timedelta
from typing import Dict

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.markdown import hbold
from aiogram import types
from aiogram.client.default import DefaultBotProperties
from aiogram.utils.callback_answer import CallbackAnswerMiddleware

from openai import OpenAI
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiogram.enums import ChatAction
from aiogram.filters import Command, CommandStart

load_dotenv()

API_TOKEN = os.getenv("API_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

client = OpenAI(api_key=OPENAI_API_KEY)

# ---------------- ЛОГИ ----------------
logging.basicConfig(level=logging.INFO)

# ---------------- БОТ ----------------
bot = Bot(token=API_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
scheduler = AsyncIOScheduler()

DB_PATH = "users.db"
DEFAULT_PROMPT = (
    "Ты — эксперт по криптовалютам и цифровой экономике. "
    "Проанализируй новость. Относится ли она к таким темам, как криптовалюты, стейблкоины, цифровой рубль, экономика Кыргызстана, цифровые валюты центробанков? "
    "Ответь только 'да' или 'нет'."
)

user_prompts: Dict[int, str] = {}

# ---------------- ИНИЦИАЛИЗАЦИЯ БД ----------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY)")
    cur.execute("CREATE TABLE IF NOT EXISTS sources (url TEXT PRIMARY KEY)")
    default_sources = [
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
        'https://www.bis.org/rss/home.xml',
    ]
    for url in default_sources:
        cur.execute("INSERT OR IGNORE INTO sources (url) VALUES (?)", (url,))
    conn.commit()
    conn.close()

# ---------------- GPT АНАЛИЗ ----------------
async def gpt_check(prompt: str) -> str:
    try:
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None,
            lambda: client.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=5,
                temperature=0
            )
        )
        return response.choices[0].message.content.strip().lower()
    except Exception as e:
        logging.warning(f"GPT error: {e}")
        return "нет"

# ---------------- ПАРСИНГ ----------------
async def fetch_news(user_id: int, prompt: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT url FROM sources")
    sources = [r[0] for r in cur.fetchall()]
    conn.close()

    stats = {}
    relevant_articles = []

    for url in sources:
        feed = feedparser.parse(url)
        stats[url] = {"total": 0, "relevant": 0, "skipped": 0}

        for entry in feed.entries:
            stats[url]["total"] += 1
            title = entry.get("title", "")
            summary = entry.get("summary", "")
            content = entry.get("content", [{}])[0].get("value", "")
            link = entry.get("link", "")
            category = entry.get("category", "")

            # Подготовка текста для GPT
            full_text = f"{title}\n\n{summary}\n\n{content}\n\n{category}"
            full_text = full_text.strip().replace("\xa0", " ")

            answer = await gpt_check(f"{prompt}\n\n{full_text}")
            if "да" in answer:
                stats[url]["relevant"] += 1
                relevant_articles.append((title, link))
            else:
                stats[url]["skipped"] += 1

    return relevant_articles, stats

# ---------------- /START и /HELP ----------------
@dp.message(CommandStart())
async def start(message: Message):
    user_id = message.from_user.id
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO users (id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Справка", callback_data="help")]
        ]
    )
    await message.answer("Добро пожаловать! Нажмите на кнопку или введите /digest для начала.", reply_markup=keyboard)

@dp.callback_query(F.data == "help")
async def help_callback(callback: CallbackQuery):
    await callback.message.edit_text(
        "/digest — получить новости\n"
        "/help — справка\n"
        "Вопросы и предложения: @your_contact"
    )

@dp.message(Command("help"))
async def help_command(message: Message):
    await message.answer(
        "/digest — получить новости\n"
        "/help — справка\n"
        "Вопросы и предложения: @your_contact"
    )

# ---------------- /DIGEST ----------------
@dp.message(Command("digest"))
async def digest(message: Message):
    user_id = message.from_user.id
    user_prompts.pop(user_id, None)  # Сброс кастомного промта

    await message.answer("Поиск релевантных новостей...")

    articles, stats = await fetch_news(user_id, DEFAULT_PROMPT)

    # Отправка результатов
    report = "Результаты по источникам:\n\n"
    total = {"all": 0, "rel": 0, "skip": 0}
    for src, stat in stats.items():
        total["all"] += stat["total"]
        total["rel"] += stat["relevant"]
        total["skip"] += stat["skipped"]
        report += f"• {src}\n— всего: {stat['total']}, релевантных: {stat['relevant']}, пропущено: {stat['skipped']}\n"

    report += f"\nИтого новостей: {total['all']}, релевантных: {total['rel']}, не подошли: {total['skip']}"

    buttons = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Получить новости", callback_data="get_news")],
        [InlineKeyboardButton(text="Задать свой запрос", callback_data="custom_prompt")]
    ])

    await message.answer(report, reply_markup=buttons)
    dp["articles"] = {user_id: articles}

# ---------------- КНОПКИ ----------------
@dp.callback_query(F.data == "get_news")
async def get_news_callback(callback: CallbackQuery):
    user_id = callback.from_user.id
    articles = dp.get("articles", {}).get(user_id, [])
    if not articles:
        await callback.message.answer("Нет новостей.")
        return
    for title, link in articles[:20]:
        await callback.message.answer(f"<b>{title}</b>\n{link}")

@dp.callback_query(F.data == "custom_prompt")
async def custom_prompt(callback: CallbackQuery):
    await callback.message.answer("Введите свой текст запроса (например, 'Найди всё про цифровой рубль и санкции').")
    user_prompts[callback.from_user.id] = "awaiting_input"

@dp.message()
async def prompt_handler(message: Message):
    user_id = message.from_user.id
    if user_prompts.get(user_id) == "awaiting_input":
        user_prompts[user_id] = message.text
        await message.answer("Запрос принят. Поиск начался...")
        articles, stats = await fetch_news(user_id, message.text)

        if articles:
            for title, link in articles[:20]:
                await message.answer(f"<b>{title}</b>\n{link}")
        else:
            await message.answer("Релевантных новостей не найдено.")
        user_prompts.pop(user_id, None)

# ---------------- СТАРТ ----------------
async def on_startup():
    init_db()
    scheduler.start()
    logging.info("Бот запущен.")

if __name__ == "__main__":
    import asyncio
    asyncio.run(dp.start_polling(bot, on_startup=on_startup))