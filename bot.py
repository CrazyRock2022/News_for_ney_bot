#!/usr/bin/env python3
import logging
import asyncio
import os
import time
import sqlite3
import feedparser
import openai
import aiohttp
import json
import hashlib
from contextlib import contextmanager
from dotenv import load_dotenv
from datetime import datetime, timedelta
from tenacity import retry, stop_after_attempt, wait_exponential
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.markdown import hbold
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.types.error_event import ErrorEvent

# Загрузка переменных окружения
load_dotenv()

# Конфигурация
API_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai.api_key = OPENAI_API_KEY
DB_FILE = "users.db"
REQUEST_DELAY = 1.5  # Задержка между запросами к OpenAI в секундах

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("bot_error.log", mode="a", encoding="utf-8"),
        logging.StreamHandler()
    ]
)

# Список RSS-источников
RSS_FEEDS = [
    "https://forklog.com/feed/",
    "https://ru.cointelegraph.com/rss",
    # ... остальные источники ...
]

DEFAULT_PROMPT = """Ты аналитик криптовалютного проекта A7A5. Проанализируй новость и ответь:
Может ли она быть потенциально релевантной проекту A7A5, если она касается:
- криптовалют
- стейблкоинов
- цифрового рубля
- экономики Кыргызстана
- финансовых регуляторов
Ответь одним словом: Да или Нет."""

# Инициализация бота
bot = Bot(
    token=API_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher(storage=MemoryStorage())
scheduler = AsyncIOScheduler()

# Состояния
class UserState(StatesGroup):
    awaiting_prompt = State()

# Database helpers
@contextmanager
def db_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

def init_db():
    with db_connection() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS sent_links (
            link TEXT PRIMARY KEY,
            sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)

# OpenAI helpers
class OpenAIAPIError(Exception):
    pass

class OpenAIRateLimitError(OpenAIAPIError):
    pass

PREFERRED_MODEL = "gpt-4-turbo"
FALLBACK_MODEL = "gpt-3.5-turbo"

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=(OpenAIAPIError, aiohttp.ClientError)
)
async def gpt_check(prompt: str) -> str:
    await asyncio.sleep(REQUEST_DELAY)  # Rate limiting
    
    models_to_try = [PREFERRED_MODEL, FALLBACK_MODEL]
    for model in models_to_try:
        try:
            response = await openai.ChatCompletion.acreate(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=5,
                temperature=0
            )
            answer = response.choices[0].message.content.strip().lower()
            return answer
        except openai.error.RateLimitError:
            raise OpenAIRateLimitError("Rate limit exceeded")
        except openai.error.APIError as e:
            logging.error(f"OpenAI API error: {e}")
            continue
    
    return "нет"

# News processing
async def parse_feed(url: str):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, feedparser.parse, url)

async def fetch_news(prompt: str):
    relevant_articles = []
    stats_by_source = {}
    seen_hashes = set()

    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT link FROM sent_links")
        seen_hashes.update(row["link"] for row in cur.fetchall())

    for url in RSS_FEEDS:
        try:
            feed = await parse_feed(url)
            source_title = feed.feed.get("title", url)
            stats = {"total": 0, "relevant": 0, "possible": 0, "not_relevant": 0}

            for entry in feed.entries[:50]:  # Ограничиваем количество новостей
                link = entry.get("link", "")
                if not link or link in seen_hashes:
                    continue

                title = entry.get("title", "")
                content = entry.get("description", "")
                full_text = f"Заголовок: {title}\nКонтент: {content}"
                gpt_prompt = f"{prompt}\n\n{full_text}\n\nДа или Нет?"

                answer = await gpt_check(gpt_prompt)
                stats["total"] += 1

                if "да" in answer:
                    relevant_articles.append(f"<b>{title}</b>\n{link}")
                    seen_hashes.add(link)
                    stats["relevant"] += 1
                elif "нет" in answer:
                    stats["not_relevant"] += 1
                else:
                    stats["possible"] += 1

            stats_by_source[source_title] = stats

        except Exception as e:
            logging.error(f"Error processing {url}: {e}")

    # Сохраняем новые ссылки
    if relevant_articles:
        with db_connection() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO sent_links (link) VALUES (?)",
                [(link.split("\n")[-1],) for link in relevant_articles]
            )

    return relevant_articles, stats_by_source

# Handlers
@dp.message(CommandStart())
async def start_command(message: Message):
    with db_connection() as conn:
        conn.execute("INSERT OR IGNORE INTO users (id) VALUES (?)", (message.from_user.id,))
    
    await message.answer(
        f"{hbold('Привет!')} Я бот для анализа новостей A7A5.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Справка", callback_data="help")]
        ])
    )

@dp.callback_query(F.data == "help")
async def help_callback(callback: CallbackQuery):
    await callback.message.edit_text(
        "/digest - Анализ свежих новостей\n"
        "/help - Показать справку"
    )
    await callback.answer()

@dp.message(Command("digest"))
async def digest_command(message: Message, state: FSMContext):
    await message.answer("⚡️ Анализирую новости...")
    
    try:
        articles, stats = await fetch_news(DEFAULT_PROMPT)
        total_viewed = sum(s["total"] for s in stats.values())
        total_relevant = sum(s["relevant"] for s in stats.values())

        report = ["<b>Отчёт:</b>"]
        for source, data in stats.items():
            report.append(
                f"{source}: {data['relevant']}/{data['total']} релевантных"
            )
        
        report.append(f"\n<b>Всего:</b> {total_relevant}/{total_viewed}")
        
        if articles:
            await state.update_data(relevant_news=articles)
            await message.answer(
                "\n".join(report),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="📄 Показать новости", callback_data="show_news")]
                ])
            )
        else:
            await message.answer("\n".join(report))
            await message.answer("Введите свой запрос для анализа:")
            await state.set_state(UserState.awaiting_prompt)

    except OpenAIRateLimitError:
        await message.answer("⚠️ Превышен лимит запросов к OpenAI")
    except Exception as e:
        logging.error(f"Digest error: {e}")
        await message.answer("⚠️ Ошибка при анализе новостей")

@dp.callback_query(F.data == "show_news")
async def show_news_callback(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    for article in data.get("relevant_news", []):
        await callback.message.answer(article)
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer()

# Scheduled tasks
async def scheduled_digest():
    logging.info("Running scheduled digest")
    try:
        articles, _ = await fetch_news(DEFAULT_PROMPT)
        if articles:
            with db_connection() as conn:
                users = conn.execute("SELECT id FROM users").fetchall()
            
            for user in users:
                try:
                    for article in articles[:10]:  # Лимит новостей на пользователя
                        await bot.send_message(user["id"], article)
                        await asyncio.sleep(0.5)
                except Exception as e:
                    logging.error(f"Error sending to user {user['id']}: {e}")
    except Exception as e:
        logging.error(f"Scheduled job error: {e}")

# Error handling
@dp.errors()
async def error_handler(event: ErrorEvent):
    logging.error(f"Unhandled exception: {event.exception}", exc_info=True)

# Main
async def main():
    init_db()
    scheduler.add_job(scheduled_digest, "cron", hour=11)
    scheduler.start()
    
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
