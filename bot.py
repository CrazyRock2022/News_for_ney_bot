import asyncio
import logging
import os
import feedparser
from datetime import datetime, timedelta
from typing import Optional, List, Dict

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import StatesGroup, State
from aiogram.filters import Command, CommandStart
from aiogram.utils.markdown import hbold
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
import openai

# --- Загрузка окружения ---
load_dotenv()
API_TOKEN = os.getenv("API_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENROUTER_KEY = os.getenv("OPENROUTER_KEY")

openai.api_key = OPENAI_API_KEY
openai.api_base = "https://api.openai.com/v1"

# --- Логирование ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# --- Telegram init ---
bot = Bot(token=API_TOKEN, default_parse_mode=ParseMode.HTML)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()
dp.include_router(router)

# --- Переменные и состояния ---
scheduler = AsyncIOScheduler()
sent_links_cache = set()
user_prompts: Dict[int, str] = {}
last_status: Dict[str, str] = {"time": "не проводился", "method": "неизвестно", "error": None}

class WaitingPrompt(StatesGroup):
    waiting_for_prompt = State()

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

KEYWORDS = ["крипта", "токен", "блокчейн", "рубль", "CBDC", "регулятор", "stablecoin", "цифровой", "ассет", "decentralized"]

# --- ФИЛЬТРАТОР С ТРЕМЯ УРОВНЯМИ ---

async def multi_filter(prompt: str) -> str:
    global last_status

    try:
        # === OpenAI GPT ===
        openai.api_key = OPENAI_API_KEY
        openai.api_base = "https://api.openai.com/v1"
        response = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: openai.ChatCompletion.create(
                model="gpt-3.5-turbo",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=5,
                temperature=0,
            )
        )
        last_status.update({"method": "OpenAI GPT", "error": None})
        return response.choices[0].message.content.strip().lower()

    except Exception as e:
        logger.warning(f"GPT-ошибка: {e}")
        last_status.update({"method": "OpenRouter", "error": "OpenAI GPT недоступен"})

        try:
            # === OpenRouter ===
            client = openai.OpenAI(
                api_key=OPENROUTER_KEY,
                base_url="https://openrouter.ai/api/v1"
            )
            response = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: client.chat.completions.create(
                    model="openchat/openchat-3.5-0106",
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=5,
                    temperature=0,
                )
            )
            last_status.update({"method": "OpenRouter", "error": None})
            return response.choices[0].message.content.strip().lower()

        except Exception as e2:
            logger.warning(f"OpenRouter ошибка: {e2}")
            last_status.update({"method": "ключевые слова", "error": "Все LLM недоступны"})

            # === Ключевые слова ===
            for word in KEYWORDS:
                if word.lower() in prompt.lower():
                    return "да"
            return "нет"

# --- Команды Telegram ---

@router.message(CommandStart())
async def cmd_start(message: Message):
    kb = InlineKeyboardBuilder()
    kb.button(text="Справка", callback_data="help")
    await message.answer(
        f"Привет, {hbold(message.from_user.first_name)}! Я бот проекта A7A5 🤖\n\n"
        "Я анализирую новости из проверенных источников с помощью искусственного интеллекта "
        "и показываю только релевантные.\n\n"
        "Нажми кнопку или введи команду:",
        reply_markup=kb.as_markup()
    )

@router.message(Command("help"))
@router.callback_query(F.data == "help")
async def help_handler(event):
    msg = (
        "📌 <b>Команды бота:</b>\n"
        "/digest — запустить анализ новостей\n"
        "/status — статус фильтрации и последнего анализа\n"
        "/help — показать справку\n\n"
        "✍️ Если не найдено новостей — можешь ввести свой промт вручную."
    )
    if isinstance(event, Message):
        await event.answer(msg)
    else:
        await event.message.edit_text(msg)
@router.message(Command("digest"))
async def cmd_digest(message: Message, state: FSMContext):
    await message.answer("🔍 Анализирую свежие новости, подождите...")
    user_prompt = user_prompts.get(message.from_user.id)
    articles = await get_news(message.from_user.id, user_prompt)

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

    found = len(relevant) + len(possible)
    last_status["time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if found == 0:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✍️ Ввести свой промт", callback_data="prompt_retry")]
        ])
        await message.answer("😕 Не найдено релевантных новостей.\n"
                             "Можешь ввести свой запрос:", reply_markup=kb)
        await state.set_state(WaitingPrompt.waiting_for_prompt)
        return

    text = "📊 <b>Результаты анализа:</b>\n\n"
    for source, s in stats.items():
        text += f"{source} — всего {s['t']}, релевантных {s['r']}, возможно {s['p']}, других {s['i']}\n"
    text += f"\n<b>Всего подходящих:</b> {found}\nФильтрация: <i>{last_status['method']}</i>"

    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Показать новости", callback_data="show_news")]]
    )
    await state.update_data(relevant=relevant, possible=possible)
    await message.answer(text, reply_markup=kb)

@router.message(Command("status"))
async def cmd_status(message: Message):
    text = (
        f"📌 <b>Последний анализ:</b>\n"
        f"Время: {last_status['time']}\n"
        f"Фильтрация: {last_status['method']}\n"
        f"Ошибка: {last_status['error'] or 'нет'}"
    )
    await message.answer(text)

@router.callback_query(F.data == "prompt_retry")
async def retry_prompt(callback: CallbackQuery, state: FSMContext):
    await state.set_state(WaitingPrompt.waiting_for_prompt)
    await callback.message.answer("✍️ Введи свой промт:")

@router.message(WaitingPrompt.waiting_for_prompt)
async def receive_prompt(message: Message, state: FSMContext):
    prompt = message.text.strip()
    if len(prompt) < 10:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Попробовать снова", callback_data="prompt_retry")]
        ])
        await message.answer("Слишком коротко. Попробуй подробнее:", reply_markup=kb)
        return
    user_prompts[message.from_user.id] = prompt
    await message.answer("🔁 Запускаю повторный анализ по твоему запросу...")
    await state.clear()
    await cmd_digest(message, state)

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

# --- Получение и фильтрация новостей ---
async def get_news(user_id: int, user_prompt: Optional[str]) -> List[dict]:
    result = []
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
                if not user_prompt else user_prompt
            )

            verdict = await multi_filter(prompt)
            status = "irrelevant"
            if "да" in verdict:
                status = "relevant"
            elif "возможно" in verdict or "не уверен" in verdict:
                status = "possible"

            result.append({
                "title": title,
                "link": link,
                "status": status,
                "source": url
            })
            sent_links_cache.add(link)
    return result

# --- Планировщик рассылки ---
async def scheduled_job():
    for uid in list(user_prompts.keys()):
        try:
            await bot.send_message(uid, "⏰ Ваш ежедневный дайджест: /digest")
        except Exception as e:
            logger.warning(f"Не удалось отправить сообщение {uid}: {e}")

# --- MAIN ---
async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    scheduler.add_job(scheduled_job, trigger="cron", hour=11, minute=0)
    scheduler.start()
    logger.info("Бот запущен.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
    


