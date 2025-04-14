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
from openai import OpenAI, APIConnectionError, RateLimitError, OpenAIError

# Загрузка переменных окружения
load_dotenv()
API_TOKEN = os.getenv("API_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

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

# Временное хранилище отправленных ссылок
sent_links_cache = set()
user_prompts: Dict[int, str] = {}

# Состояния
class WaitingPrompt(StatesGroup):
    waiting_for_prompt = State()

# Многоуровневая фильтрация: OpenAI → OpenRouter → ключи
async def gpt_check(prompt: str) -> str:
    messages = [
        {"role": "system", "content": "Ты ИИ, который определяет релевантность новости по запросу."},
        {"role": "user", "content": prompt}
    ]
    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: client.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=messages,
                temperature=0
            )
        )
        reply = response.choices[0].message.content.strip()
        logger.info(f"[OpenAI GPT] Ответ: {reply}")
        return reply
    except (RateLimitError, APIConnectionError, OpenAIError) as e:
        logger.warning(f"[OpenAI GPT] Ошибка: {e}, пробуем OpenRouter...")

    try:
        client = OpenAI(api_key=OPENROUTER_API_KEY, base_url="https://openrouter.ai/api/v1")
        response = await loop.run_in_executor(
            None,
            lambda: client.chat.completions.create(
                model="openchat/openchat-3.5-0106",
                messages=messages,
                temperature=0
            )
        )
        reply = response.choices[0].message.content.strip()
        logger.info(f"[OpenRouter] Ответ: {reply}")
        return reply
    except Exception as e:
        logger.error(f"[OpenRouter] Ошибка: {e}")
        return None

async def multi_filter(text: str, prompt: str) -> str:
    try:
        result = await gpt_check(prompt + f"\n\nНовость:\n{text}")
        if result and "да" in result.lower():
            return "relevant"
        elif result and "возможно" in result.lower():
            return "possible"
        else:
            return "irrelevant"
    except Exception as e:
        logger.warning(f"GPT упал, пробуем ключевые слова: {e}")

    # Ключевые слова fallback
    keywords = ["крипта", "токен", "рубль", "цифровой", "CBDC", "стейблкоин", "блокчейн", "регулятор"]
    for word in keywords:
        if word in text.lower():
            return "relevant"
    return "irrelevant"

# Команда /start
@router.message(CommandStart())
async def cmd_start(message: Message):
    kb = InlineKeyboardBuilder()
    kb.button(text="Справка", callback_data="help")
    await message.answer(
        f"Привет, {hbold(message.from_user.first_name)}! Я бот проекта A7A5.\n\n"
        "Я анализирую свежие новости из проверенных источников с помощью GPT "
        "и присылаю только то, что действительно важно.",
        reply_markup=kb.as_markup()
    )

# Команда /help и кнопка
@router.callback_query(F.data == "help")
async def callback_help(callback: CallbackQuery):
    await callback.message.edit_text(
        "/digest — запустить анализ новостей\n"
        "/status — показать статус анализа\n"
        "/help — показать справку\n"
        "Если не найдено новостей — можешь ввести свой промт."
    )

@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "/digest — запустить анализ новостей\n"
        "/status — показать статус анализа\n"
        "/help — показать справку\n"
        "Если не найдено новостей — можешь ввести свой промт."
    )

# Команда /digest
@router.message(Command("digest"))
async def cmd_digest(message: Message, state: FSMContext):
    await message.answer("Анализирую новости, подождите...")
    articles, stats = await get_news(message.from_user.id)

    relevant, possible, irrelevant = [], [], []
    for a in articles:
        if a["status"] == "relevant":
            relevant.append(a)
        elif a["status"] == "possible":
            possible.append(a)
        else:
            irrelevant.append(a)

    found = len(relevant) + len(possible)
    if found == 0:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Ввести промт", callback_data="prompt_retry")]]
        )
        await message.answer("Не найдено подходящих новостей.\nМожешь ввести свой запрос:", reply_markup=kb)
        await state.set_state(WaitingPrompt.waiting_for_prompt)
        return

    text = "📊 <b>Результаты анализа:</b>\n\n"
    for src, s in stats.items():
        text += f"{src} — всего {s['t']}, релевантных {s['r']}, возможно {s['p']}, остальных {s['i']}\n"
    text += f"\n<b>Всего найдено:</b> {found} подходящих новостей."

    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Показать новости", callback_data="show_news")]]
    )
    await state.update_data(relevant=relevant, possible=possible)
    await message.answer(text, reply_markup=kb)

# Команда /status
@router.message(Command("status"))
async def cmd_status(message: Message):
    await message.answer("✅ Бот активен.\n"
                         "Фильтрация: GPT → OpenRouter → ключевые слова\n"
                         f"Источников: {len(NEWS_SOURCES)}\n"
                         f"Отправлено ссылок: {len(sent_links_cache)}")

# Получение новостей
async def get_news(user_id: int) -> (List[dict], Dict[str, dict]):
    result = []
    stats = {}
    user_prompt = user_prompts.get(user_id)
    use_custom_prompt = user_prompt is not None

    for url in NEWS_SOURCES:
        try:
            feed = feedparser.parse(url)
            entries = feed.entries
        except Exception as e:
            logger.warning(f"Ошибка RSS {url}: {e}")
            continue

        for entry in entries:
            title = entry.get("title", "").strip()
            link = entry.get("link", "").strip()
            summary = entry.get("summary", "")
            content = entry.get("content", [{}])[0].get("value", "")

            if link in sent_links_cache:
                continue

            pub_time = None
            if "published_parsed" in entry:
                pub_time = datetime(*entry.published_parsed[:6])
            elif "updated_parsed" in entry:
                pub_time = datetime(*entry.updated_parsed[:6])
            else:
                continue

            if datetime.utcnow() - pub_time > timedelta(days=7):
                continue

            prompt = (
                f"Заголовок: {title}\nОписание: {summary}\nКонтент: {content[:1000]}\n\n"
                "Может ли это быть релевантно проекту A7A5, криптовалютам, цифровому рублю, экономике или регуляторам?"
                if not use_custom_prompt else user_prompt
            )

            verdict = await multi_filter(f"{title}\n\n{summary}", prompt)
            stats.setdefault(url, {"r": 0, "p": 0, "i": 0, "t": 0})
            stats[url]["t"] += 1
            stats[url][verdict[0]] += 1

            result.append({
                "title": title,
                "link": link,
                "status": verdict,
                "source": url
            })
            sent_links_cache.add(link)

    return result, stats

# Кнопка: показать новости
@router.callback_query(F.data == "show_news")
async def show_news(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    relevant_news = data.get("relevant", [])
    possible_news = data.get("possible", [])

    if not relevant_news and not possible_news:
        await callback.message.answer("Нет новостей для отображения.")
        return

    await callback.message.answer("📰 Вот подходящие новости:")
    for item in relevant_news + possible_news:
        await callback.message.answer(f"<b>{item['title']}</b>\n{item['link']}")

# Кнопка: ввести промт
@router.callback_query(F.data == "prompt_retry")
async def retry_prompt(callback: CallbackQuery, state: FSMContext):
    await state.set_state(WaitingPrompt.waiting_for_prompt)
    await callback.message.answer("✍️ Введите свой промт:")

# Ввод промта от пользователя
@router.message(WaitingPrompt.waiting_for_prompt)
async def receive_prompt(message: Message, state: FSMContext):
    prompt = message.text.strip()
    if len(prompt) < 10:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Попробовать снова", callback_data="prompt_retry")]]
        )
        await message.answer("Промт слишком короткий. Попробуйте подробнее:", reply_markup=kb)
        return

    user_prompts[message.from_user.id] = prompt
    await message.answer("Понял! Провожу анализ по твоему запросу...")
    await state.clear()
    await cmd_digest(message, state)

# Рассылка
async def scheduled_job():
    for uid in list(user_prompts.keys()):
        try:
            await bot.send_message(uid, "⏰ Ваш ежедневный дайджест: /digest")
        except Exception as e:
            logger.warning(f"Не удалось отправить сообщение {uid}: {e}")

# Main
async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    scheduler.add_job(scheduled_job, trigger="cron", hour=11, minute=0)
    scheduler.start()
    logger.info("Бот запущен.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())