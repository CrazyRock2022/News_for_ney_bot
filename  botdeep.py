#!/usr/bin/env python3
import os
import sys
import time
import json
import signal
import sqlite3
import feedparser
import aiohttp
import asyncio
import logging
import hashlib
from datetime import datetime, timedelta
from contextlib import contextmanager
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from tenacity import retry, stop_after_attempt, wait_exponential

# Загрузка переменных окружения
from dotenv import load_dotenv
load_dotenv()

# Конфигурация
API_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai.api_key = OPENAI_API_KEY

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("newsbot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)

# Инициализация бота
bot = Bot(
    token=API_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher(storage=MemoryStorage())
scheduler = AsyncIOScheduler()

# Состояния FSM
class UserState(StatesGroup):
    ADDING_SOURCE = State()
    AWAITING_PROMPT = State()

# Пользовательские парсеры
def parse_cryptopanic(entry):
    currencies = ", ".join([c["code"] for c in entry.get("currencies", [])])
    return {
        "title": entry.get("title", "Без заголовка"),
        "link": entry.get("link", "#"),
        "content": f"{entry.get('description', '')}\nКриптовалюты: {currencies}"
    }

SOURCE_PARSERS = {
    "cryptopanic.com": parse_cryptopanic,
}

# Работа с базой данных
@contextmanager
def db_connection():
    conn = sqlite3.connect("newsbot.db")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

def init_db():
    with db_connection() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS sources (
            url TEXT PRIMARY KEY,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.execute("""
        CREATE TABLE IF NOT EXISTS sent_links (
            link TEXT PRIMARY KEY,
            sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")

# OpenAI интеграция
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
async def analyze_with_gpt(text: str, prompt: str) -> str:
    try:
        response = await openai.ChatCompletion.acreate(
            model="gpt-4-turbo",
            messages=[{
                "role": "user",
                "content": f"{prompt}\n\n{text[:3000]}\nОтвет:"
            }],
            max_tokens=50,
            temperature=0.3
        )
        return response.choices[0].message.content.strip().lower()
    except Exception as e:
        logging.error(f"OpenAI error: {str(e)}")
        return "нет"

# Обработка RSS-лент
async def fetch_feed(url: str):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status == 200:
                    return feedparser.parse(await response.text())
    except Exception as e:
        logging.error(f"Ошибка получения фида {url}: {str(e)}")
    return None

async def process_entries(feed, url):
    source_name = feed.feed.get("title", url)
    parser = SOURCE_PARSERS.get(url.split("//")[-1].split("/")[0], None)
    
    with db_connection() as conn:
        sent_links = {row["link"] for row in conn.execute("SELECT link FROM sent_links")}
    
    relevant = []
    stats = {"total": 0, "relevant": 0}
    
    for entry in feed.entries[:50]:
        if parser:
            data = parser(entry)
        else:
            data = {
                "title": entry.get("title", ""),
                "link": entry.get("link", ""),
                "content": entry.get("description", "")
            }
        
        if not data["link"] or data["link"] in sent_links:
            continue
        
        stats["total"] += 1
        analysis_text = f"{data['title']}\n{data['content']}"
        answer = await analyze_with_gpt(analysis_text, DEFAULT_PROMPT)
        
        if "да" in answer:
            relevant.append((data["title"], data["link"]))
            stats["relevant"] += 1
            with db_connection() as conn:
                conn.execute("INSERT INTO sent_links (link) VALUES (?)", (data["link"],))
    
    return source_name, stats, relevant

# Основной обработчик новостей
async def fetch_news():
    relevant = []
    stats = {}
    
    with db_connection() as conn:
        sources = [row["url"] for row in conn.execute("SELECT url FROM sources")]
    
    for url in sources:
        try:
            feed = await fetch_feed(url)
            if not feed or not feed.entries:
                continue
            
            source_name, source_stats, source_relevant = await process_entries(feed, url)
            stats[source_name] = source_stats
            relevant.extend(source_relevant)
        
        except Exception as e:
            logging.error(f"Ошибка обработки {url}: {str(e)}")
    
    return relevant, stats

# Хендлеры команд
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    with db_connection() as conn:
        conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (message.from_user.id,))
    
    await message.answer(
        "📰 Бот для мониторинга новостей A7A5\n\n"
        "Доступные команды:\n"
        "/digest - Получить свежие новости\n"
        "/add_source - Добавить RSS-источник\n"
        "/help - Справка",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="🆘 Помощь", callback_data="help")]
        )
    )

@dp.callback_query(F.data == "help")
async def show_help(callback: types.CallbackQuery):
    await callback.message.edit_text(
        "ℹ️ Справка по командам:\n\n"
        "/digest - Запустить анализ новостей\n"
        "/add_source [url] - Добавить RSS-ленту\n"
        "/help - Показать это сообщение"
    )
    await callback.answer()

@dp.message(Command("digest"))
async def cmd_digest(message: types.Message, state: FSMContext):
    await message.answer("⏳ Анализирую новости...")
    
    try:
        relevant, stats = await fetch_news()
        report = ["📊 Отчёт по источникам:"]
        
        for source, data in stats.items():
            report.append(f"▪️ {source}: {data['relevant']}/{data['total']}")
        
        if relevant:
            await state.update_data(relevant_news=relevant)
            await message.answer(
                "\n".join(report),
                reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                    [types.InlineKeyboardButton(
                        text=f"📩 Получить новости ({len(relevant)})", 
                        callback_data="get_news")]
                ])
            )
        else:
            await message.answer("\n".join(report) + "\n\n❌ Релевантных новостей не найдено")
    
    except Exception as e:
        logging.error(f"Ошибка digest: {str(e)}")
        await message.answer("⚠️ Ошибка при обработке новостей")

@dp.message(Command("add_source"))
async def cmd_add_source(message: types.Message, state: FSMContext):
    args = message.text.split()
    if len(args) < 2:
        await message.answer("❌ Укажите URL RSS-ленты\nПример: /add_source https://example.com/rss")
        return
    
    url = args[1].strip()
    if not url.startswith("http"):
        await message.answer("❌ Неверный формат URL")
        return
    
    try:
        with db_connection() as conn:
            conn.execute("INSERT INTO sources (url) VALUES (?)", (url,))
        await message.answer(f"✅ Источник добавлен: {url}")
    except sqlite3.IntegrityError:
        await message.answer("⚠️ Этот источник уже существует")

@dp.callback_query(F.data == "get_news")
async def send_news(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    news = data.get("relevant_news", [])
    
    for title, link in news:
        await callback.message.answer(f"<b>{title}</b>\n{link}")
    
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer()

# Ежедневная рассылка
async def daily_digest():
    logging.info("Запуск ежедневной рассылки...")
    relevant, _ = await fetch_news()
    
    if not relevant:
        logging.info("Нет новостей для рассылки")
        return
    
    with db_connection() as conn:
        users = [row["user_id"] for row in conn.execute("SELECT user_id FROM users")]
    
    for user_id in users:
        try:
            for title, link in relevant[:5]:
                await bot.send_message(user_id, f"<b>{title}</b>\n{link}")
                await asyncio.sleep(1)
        except Exception as e:
            logging.error(f"Ошибка отправки для {user_id}: {str(e)}")

# Запуск и завершение
async def on_shutdown():
    logging.info("Завершение работы...")
    await bot.close()

async def main():
    init_db()
    scheduler.add_job(daily_digest, "cron", hour=11)
    scheduler.start()
    
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda s, f: asyncio.run(on_shutdown()))
    asyncio.run(main())