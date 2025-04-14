import asyncio
import logging
import os
import sqlite3
import feedparser
from datetime import datetime, timedelta
from typing import Optional, List, Dict

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import StatesGroup, State
from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.utils.markdown import hbold
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from openai import OpenAI

# Загрузка переменных окружения
load_dotenv()
API_TOKEN = os.getenv("API_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

client = OpenAI(api_key=OPENAI_API_KEY)

# Логирование
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Бот и диспетчер
bot = Bot(token=API_TOKEN, default_parse_mode=ParseMode.HTML)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()
dp.include_router(router)

# Расписание
scheduler = AsyncIOScheduler()

# Источники
NEWS_SOURCES = [
    "https://forklog.com/feed/",
    "https://ru.cointelegraph.com/rss",
    "https://bits.media/rss/news/",
    "https://incrypted.com/feed/",
    "https://cryptopanic.com/news/rss/",
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
    "https://www.coindesk.com/arc/outboundfeeds/rss/?outputType=xml",
    "https://www.cbr.ru/rss/",
    "http://www.finmarket.ru/rss/",
    "https://rssexport.rbc.ru/rbcnews/news/eco/index.rss",
    "https://www.kommersant.ru/RSS/news.xml",
    "https://www.forbes.ru/rss",
    "https://24.kg/rss/",
    "https://akipress.org/rss/news.rss",
    "https://www.themoscowtimes.com/rss",
    "https://blogs.imf.org/feed/",
    "https://www.bis.org/rss/home.xml"
]

# Временное хранилище отправленных ссылок (не в БД)
sent_links_cache = set()

# Пользовательские промты
user_prompts: Dict[int, str] = {}

# Состояния
class WaitingPrompt(StatesGroup):
    waiting_for_prompt = State()

# ---------- ФУНКЦИЯ GPT-ФИЛЬТРА ----------

async def gpt_check(prompt: str) -> str:
    """Отправляет запрос к OpenAI GPT-4 и возвращает ответ модели."""
    try:
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None,
            lambda: client.chat.completions.create(
                model="gpt-4",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=5,
                temperature=0
            )
        )
        return response.choices[0].message.content.strip().lower()
    except Exception as e:
        logger.exception("OpenAI API error")
        return "нет"

# ---------- КОМАНДА /start ----------

@router.message(CommandStart())
async def cmd_start(message: Message):
    kb = InlineKeyboardBuilder()
    kb.button(text="Справка", callback_data="help")
    await message.answer(
        f"Привет, {hbold(message.from_user.first_name)}! Я бот проекта A7A5.\n\n"
        "Я анализирую свежие новости из проверенных источников с помощью GPT-4 "
        "и присылаю только то, что действительно важно.",
        reply_markup=kb.as_markup()
    )

# ---------- КНОПКА СПРАВКА ----------

@router.callback_query(F.data == "help")
async def callback_help(callback: CallbackQuery):
    await callback.message.edit_text(
        "/digest — запустить анализ новостей\n"
        "/help — показать справку\n"
        "Также я могу принять ваш промт, если не нашлось новостей"
    )

@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "/digest — запустить анализ новостей\n"
        "/help — показать справку\n"
        "Также я могу принять ваш промт, если не нашлось новостей"
    )

# ---------- ОБРАБОТКА КНОПКИ "ПОЛУЧИТЬ НОВОСТИ" ----------

@router.callback_query(F.data == "show_news")
async def show_news(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    relevant_news = data.get("relevant", [])
    possible_news = data.get("possible", [])

    if not relevant_news and not possible_news:
        await callback.message.answer("Нет новостей для отображения.")
        return

    await callback.message.answer("Отправляю подходящие новости:")

    for item in relevant_news + possible_news:
        await callback.message.answer(f"<b>{item['title']}</b>\n{item['link']}")

# ---------- КОМАНДА /digest ----------

@router.message(Command("digest"))
async def cmd_digest(message: Message, state: FSMContext):
    await message.answer("Анализирую новости, подождите...")
    articles = await get_news(message.from_user.id)

    stats = {}
    relevant, possible, irrelevant = [], [], []

    for art in articles:
        source = art["source"]
        stats.setdefault(source, {"r": 0, "p": 0, "i": 0, "t": 0})
        stats[source]["t"] += 1

        if art["status"] == "relevant":
            stats[source]["r"] += 1
            relevant.append(art)
        elif art["status"] == "possible":
            stats[source]["p"] += 1
            possible.append(art)
        else:
            stats[source]["i"] += 1
            irrelevant.append(art)

    total = len(articles)
    found = len(relevant) + len(possible)

    if found == 0:
        await message.answer(
            "Не найдено подходящих новостей.\n"
            "Введите свой промт (запрос) — и я проведу анализ снова:"
        )
        await state.set_state(WaitingPrompt.waiting_for_prompt)
        return

    text = "Результаты анализа:\n\n"
    for source, s in stats.items():
        text += f"{source} — всего {s['t']}, релевантных {s['r']}, возможно {s['p']}, остальных {s['i']}\n"

    text += f"\nВсего найдено {found} подходящих новостей."

    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Получить новости", callback_data="show_news")]]
    )

    await state.update_data(relevant=relevant, possible=possible)
    await message.answer(text, reply_markup=kb)
    
# ---------- ПОЛУЧЕНИЕ НОВОСТЕЙ ----------

async def get_news(user_id: int) -> List[dict]:
    result = []
    user_prompt = user_prompts.get(user_id)
    use_custom_prompt = user_prompt is not None

    for url in NEWS_SOURCES:
        try:
            feed = feedparser.parse(url)
            entries = feed.entries
        except Exception as e:
            logger.warning(f"Ошибка чтения RSS {url}: {e}")
            continue

        for entry in entries:
            title = entry.get("title", "").strip()
            link = entry.get("link", "").strip()
            summary = entry.get("summary", "")
            content = entry.get("content", [{}])[0].get("value", "")

            # Пропуск если уже отправляли
            if link in sent_links_cache:
                continue

            # Определение времени публикации
            pub_time = None
            if "published_parsed" in entry:
                pub_time = datetime(*entry.published_parsed[:6])
            elif "updated_parsed" in entry:
                pub_time = datetime(*entry.updated_parsed[:6])
            else:
                logger.warning(f"Нет даты публикации: {title}")
                continue

            if datetime.utcnow() - pub_time > timedelta(days=7):
                continue

            # Промт
            prompt = (
                f"Заголовок: {title}\n"
                f"Описание: {summary}\n"
                f"Контент: {content[:1000]}\n\n"
                "Может ли это быть потенциально релевантно проекту A7A5, криптовалютам, цифровому рублю, экономике или регуляторам?"
                if not use_custom_prompt else user_prompt
            )

            answer = await gpt_check(prompt)
            status = "irrelevant"

            if "да" in answer:
                status = "relevant"
            elif "возможно" in answer or "не уверен" in answer:
                status = "possible"

            result.append({
                "title": title,
                "link": link,
                "status": status,
                "source": url
            })

            sent_links_cache.add(link)

    return result

# ---------- ОБРАБОТКА ВВОДА ПРОМТА ----------

@router.message(WaitingPrompt.waiting_for_prompt)
async def receive_prompt(message: Message, state: FSMContext):
    prompt = message.text.strip()
    if len(prompt) < 10:
        await message.answer("Промт слишком короткий. Попробуй подробнее.")
        return

    user_prompts[message.from_user.id] = prompt
    await message.answer("Понял! Провожу анализ по твоему запросу...")
    await state.clear()
    await cmd_digest(message, state)

# ---------- ЕЖЕДНЕВНАЯ РАССЫЛКА ----------

async def scheduled_job():
    users = list(user_prompts.keys())
    for uid in users:
        try:
            await bot.send_message(uid, "Ваш ежедневный дайджест новостей /digest")
        except Exception as e:
            logger.warning(f"Не удалось отправить сообщение пользователю {uid}: {e}")

# ---------- MAIN ----------

async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    scheduler.add_job(scheduled_job, trigger="cron", hour=11, minute=0)
    scheduler.start()
    logger.info("Бот запущен.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
    
