import asyncio
import logging
import feedparser
import sqlite3
from datetime import datetime, timedelta
import openai
import os
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram import types
from aiogram.client.default import DefaultBotProperties
from apscheduler.schedulers.asyncio import AsyncIOScheduler

API_TOKEN = os.getenv("API_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai.api_key = OPENAI_API_KEY

bot = Bot(token=API_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

DB_FILE = "users.db"
scheduler = AsyncIOScheduler()

DEFAULT_PROMPT = (
    "Ты аналитик криптовалютного проекта A7A5. "
    "Проанализируй новость и ответь: может ли она быть потенциально релевантной проекту A7A5, "
    "если она касается криптовалют, стейблкоинов, цифрового рубля, экономики Кыргызстана, финансовых регуляторов, "
    "или мировой криптоинфраструктуры?\n\n{context}\n\nОтветь одним словом: Да или Нет."
)

class PromptState(StatesGroup):
    waiting_for_prompt = State()

user_prompt: dict[int, str] = {}

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY)")
    cur.execute("CREATE TABLE IF NOT EXISTS sent_links (link TEXT PRIMARY KEY)")
    cur.execute("CREATE TABLE IF NOT EXISTS sources (url TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()

@router.message(F.text == "/start")
async def start(message: Message):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO users (id) VALUES (?)", (message.from_user.id,))
    conn.commit()
    conn.close()
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Справка", callback_data="help")]
    ])
    await message.answer("Привет! Я бот по новостям A7A5. Нажми /digest для анализа.", reply_markup=keyboard)

@router.callback_query(F.data == "help")
async def help_callback(callback: types.CallbackQuery):
    text = (
        "Команды:\n"
        "/digest — получить новости\n"
        "/addsource <url> — добавить источник\n"
        "/listsources — список источников\n"
        "/removesource <url> — удалить источник"
    )
    await callback.message.edit_text(text)

@router.message(F.text.startswith("/addsource"))
async def add_source(message: Message):
    parts = message.text.split()
    if len(parts) < 2:
        return await message.reply("Укажи ссылку: /addsource <url>")
    url = parts[1]
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO sources (url) VALUES (?)", (url,))
    conn.commit()
    conn.close()
    await message.reply("Источник добавлен!")

@router.message(F.text.startswith("/removesource"))
async def remove_source(message: Message):
    parts = message.text.split()
    if len(parts) < 2:
        return await message.reply("Укажи ссылку: /removesource <url>")
    url = parts[1]
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("DELETE FROM sources WHERE url=?", (url,))
    conn.commit()
    conn.close()
    await message.reply("Источник удалён!")

@router.message(F.text == "/listsources")
async def list_sources(message: Message):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT url FROM sources")
    urls = cur.fetchall()
    conn.close()
    if not urls:
        return await message.reply("Источников нет.")
    await message.reply("Источники:\n" + "\n".join(url[0] for url in urls))

@router.message(F.text == "/digest")
async def run_digest(message: Message, state: FSMContext):
    await message.answer("Нет релевантных новостей. Введи промт (например: «найди новости о ФРС и ставке рубля»):")
    await state.set_state(PromptState.waiting_for_prompt)

@router.message(PromptState.waiting_for_prompt)
async def receive_prompt(message: Message, state: FSMContext):
    user_prompt[message.from_user.id] = message.text
    await message.answer("Ищу новости...")
    articles = await get_news(message.from_user.id)
    if not articles:
        await message.answer("Релевантных новостей не найдено.")
    else:
        for article in articles:
            await message.answer(f"<b>{article['title']}</b>\n{article['link']}", parse_mode="HTML")
    await state.clear()

async def is_relevant(user_id: int, title: str, summary: str, content: str) -> bool:
    context = f"Заголовок: {title}\nОписание: {summary}\nПолный текст: {content[:1000]}"
    prompt = user_prompt.get(user_id, DEFAULT_PROMPT.format(context=context))
    try:
        response = await openai.ChatCompletion.acreate(
            model="gpt-4-turbo",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=3
        )
        answer = response.choices[0].message.content.strip().lower()
        return "да" in answer
    except Exception as e:
        logging.warning(f"OpenAI error: {e}")
        return False

async def get_news(user_id: int):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT url FROM sources")
    sources = [row[0] for row in cur.fetchall()]
    conn.close()

    articles = []
    for url in sources:
        feed = feedparser.parse(url)
        for entry in feed.entries:
            title = entry.title
            link = entry.link
            summary = entry.get("summary", "")
            content = entry.get("content", [{}])[0].get("value", "") if 'content' in entry else ""
            if await is_relevant(user_id, title, summary, content):
                articles.append({"title": title, "link": link})
    return articles

async def on_startup():
    init_db()
    await bot.delete_webhook(drop_pending_updates=True)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(dp.start_polling(bot, on_startup=on_startup))
