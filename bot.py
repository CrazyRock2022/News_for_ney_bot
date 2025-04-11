import logging
import asyncio
import os
import time
import sqlite3
import feedparser
import openai
import aiohttp
import json
from dotenv import load_dotenv
from datetime import datetime, timedelta

# Загрузка переменных окружения из файла .env
load_dotenv()

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.markdown import hbold
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiogram.enums import ParseMode

# Установка API-ключей и токенов из переменных окружения
API_TOKEN = os.getenv("API_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai.api_key = OPENAI_API_KEY  # устанавливаем ключ для библиотеки openai (если будет использоваться)

# Настройка логирования в файл
file_handler = logging.FileHandler("bot_error.log", mode="a", encoding="utf-8")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[file_handler]
)

# Константы и настройки бота
DB_FILE = "users.db"
# Список RSS-источников (по умолчанию)
RSS_FEEDS = [
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

# Промт по умолчанию для GPT-анализа релевантности
DEFAULT_PROMPT = (
    "Ты аналитик криптовалютного проекта A7A5. Проанализируй новость и ответь: "
    "может ли она быть потенциально релевантной проекту A7A5, если она касается "
    "криптовалют, стейблкоинов, цифрового рубля, экономики Кыргызстана, финансовых "
    "регуляторов или мировой криптоинфраструктуры? Ответь одним словом: Да или Нет."
)

# Инициализация бота, диспетчера и планировщика
bot = Bot(token=API_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher(storage=MemoryStorage())
scheduler = AsyncIOScheduler()

# ------- Состояния пользователя (FSM) -------
class UserState(StatesGroup):
    awaiting_prompt = State()  # ожидание пользовательского запроса для анализа

# ------- ИНИЦИАЛИЗАЦИЯ БД -------
def init_db():
    """Создает таблицы пользователей и отправленных ссылок (если не существуют)."""
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY)")
    cur.execute("CREATE TABLE IF NOT EXISTS sent_links (link TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()

# ------- ХЕНДЛЕРЫ КОМАНД -------
@dp.message(CommandStart())
async def start_command(message: Message, state: FSMContext):
    """Обработчик команды /start — регистрирует нового пользователя и отправляет приветствие."""
    user_id = message.from_user.id
    # Добавляем пользователя в базу (если уже существует, ничего не произойдет)
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO users (id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()
    # Отправляем приветственное сообщение с кнопкой "Справка"
    greeting_text = f"{hbold('Привет!')} Я буду присылать тебе релевантные новости по теме A7A5."
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Справка", callback_data="help")]
    ])
    await message.answer(greeting_text, reply_markup=keyboard)

@dp.callback_query(F.data == "help")
async def help_callback(callback: CallbackQuery):
    """Обработчик нажатия кнопки "Справка" — показывает список команд."""
    help_text = (
        "/digest — собрать и проанализировать свежие новости\n"
        "/help — показать эту справку"
    )
    await callback.message.edit_text(help_text)
    # Отправляем ответ на callback (убирает значок загрузки на кнопке)
    await callback.answer()

@dp.message(Command("help"))
async def help_command(msg: Message):
    """Обработчик команды /help — выводит список команд."""
    help_text = (
        "/digest — собрать и проанализировать свежие новости\n"
        "/help — показать эту справку"
    )
    await msg.answer(help_text)

# ------- GPT АНАЛИЗ -------
# Классы исключений для ошибок OpenAI API
class OpenAIAPIError(Exception):
    """Общее исключение для ошибок OpenAI API."""
    pass

class OpenAIRateLimitError(OpenAIAPIError):
    """Исключение при превышении лимитов или квоты OpenAI API (HTTP 429)."""
    pass

# Настройки моделей OpenAI
PREFERRED_MODEL = "gpt-4"
FALLBACK_MODEL = "gpt-3.5-turbo"
current_model = PREFERRED_MODEL  # текущая используемая модель (начинаем с gpt-4, при недоступности переключимся)

async def gpt_check(prompt: str) -> str:
    """Отправляет запрос к OpenAI и возвращает ответ модели ('да' или 'нет')."""
    global current_model
    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json"
    }
    # Список моделей для попытки: сначала текущая (предпочтительная), затем запасная при необходимости
    models_to_try = [current_model] if current_model != PREFERRED_MODEL else [PREFERRED_MODEL, FALLBACK_MODEL]
    async with aiohttp.ClientSession() as session:
        for model in models_to_try:
            data = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 5,
                "temperature": 0
            }
            try:
                async with session.post(url, headers=headers, json=data) as resp:
                    if resp.status == 200:
                        # Успешный ответ от API
                        result = await resp.json()
                        answer = result["choices"][0]["message"]["content"].strip().lower()
                        # Обновляем текущую модель на успешную (если переключились на FALLBACK_MODEL)
                        current_model = model
                        return answer
                    # Обработка ошибок при статусе != 200
                    error_text = await resp.text()
                    try:
                        error_json = json.loads(error_text)
                    except json.JSONDecodeError:
                        error_json = {}
                    err_message = error_json.get("error", {}).get("message", "")
                    err_code = error_json.get("error", {}).get("code", "")
                    if resp.status == 429:
                        logging.error(f"OpenAI API error 429: {err_code} - {err_message}", exc_info=True)
                        # Превышена квота или частотный лимит
                        raise OpenAIRateLimitError(err_message or "Rate limit exceeded")
                    if resp.status in (400, 404) and err_code == "model_not_found":
                        logging.warning(f"OpenAI model not found: {err_message}")
                        # Если модель не найдена, переходим к следующей модели (если есть)
                        continue
                    if resp.status == 401:
                        logging.error(f"OpenAI API authentication error: {err_message}", exc_info=True)
                        raise OpenAIAPIError("Authentication failed for OpenAI API")
                    # Прочие ошибки API
                    logging.error(f"OpenAI API error {resp.status}: {err_message}", exc_info=True)
                    raise OpenAIAPIError(err_message or f"HTTP {resp.status} error")
            except OpenAIRateLimitError:
                # Превышены лимиты/квоты OpenAI — прекращаем дальнейшие попытки
                raise
            except aiohttp.ClientError as e:
                # Ошибка сети при обращении к OpenAI
                logging.error(f"OpenAI API network error: {e}", exc_info=True)
                raise OpenAIAPIError("OpenAI API network error")
            except OpenAIAPIError:
                # Другие ошибки OpenAI API (аутентификация, внутренняя ошибка и т.д.)
                raise
            except Exception as e:
                # Непредвиденная ошибка: логируем и, если есть другая модель, пробуем её
                logging.error(f"Unexpected OpenAI error: {e}", exc_info=True)
                if model == models_to_try[-1]:
                    # Если это была последняя модель, возвращаем "нет"
                    return "нет"
                else:
                    # Иначе пробуем следующую модель
                    continue
    # Если ни одна модель не вернула ответ, по умолчанию считаем новость нерелевантной
    return "нет"

# ------- ОБРАБОТКА НОВОСТЕЙ -------
async def fetch_news(prompt: str):
    """Парсит все RSS-источники и анализирует новости с помощью GPT.
    Возвращает (список релевантных новостей, статистика по источникам)."""
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    # Получаем уже отправленные ссылки, чтобы не отправлять повторно
    cur.execute("SELECT link FROM sent_links")
    sent_links_set = {row[0] for row in cur.fetchall()}
    relevant_articles = []    # список сообщений с релевантными новостями (заголовок + ссылка)
    stats_by_source = {}      # статистика по каждому источнику

    now = datetime.now()
    one_week_ago = now - timedelta(days=7)

    # Обходим все источники и обрабатываем каждую новость
    for url in RSS_FEEDS:
        feed = feedparser.parse(url)
        source_title = feed.feed.get("title", url) if hasattr(feed, "feed") else url
        total_count = 0
        relevant_count = 0
        possible_count = 0
        not_relevant_count = 0

        for entry in feed.entries:
            link = entry.get("link", "")
            # Пропускаем новость, если нет ссылки или она уже отправлялась
            if not link or link in sent_links_set:
                continue
            # Пропускаем новость, если она старше 7 дней (при наличии даты)
            entry_date = None
            if "published_parsed" in entry and entry.published_parsed:
                try:
                    entry_date = datetime.fromtimestamp(time.mktime(entry.published_parsed))
                except Exception:
                    entry_date = None
            if entry_date and entry_date < one_week_ago:
                continue

            total_count += 1
            title = entry.get("title", "(без заголовка)")
            summary = entry.get("summary", "")
            category = entry.get("category", "")
            tags = [t.get("term", "") for t in entry.tags] if "tags" in entry else []
            content = ""
            if "content" in entry and entry.content:
                content = entry.content[0].get("value", "")

            # Полный текст новости для анализа
            full_text = (
                f"Заголовок: {title}\n"
                f"Описание: {summary}\n"
                f"Категория: {category}\n"
                f"Теги: {', '.join(tags)}\n"
                f"Контент: {content}"
            )
            # Промт для GPT (вопрос + новость), требуем ответ "Да" или "Нет"
            gpt_prompt = f"{prompt}\n\n{full_text}\n\nДа или Нет?"
            answer = await gpt_check(gpt_prompt)
            # Классификация ответа модели
            if "да" in answer or "yes" in answer:
                relevant_count += 1
                relevant_articles.append(f"<b>{title}</b>\n{link}")
                sent_links_set.add(link)  # отмечаем ссылку как отправленную
            elif "нет" in answer or "no" in answer:
                not_relevant_count += 1
            else:
                possible_count += 1

        # Сохраняем статистику по текущему источнику
        stats_by_source[source_title] = {
            "total": total_count,
            "relevant": relevant_count,
            "possible": possible_count,
            "not_relevant": not_relevant_count
        }

    # Записываем новые релевантные ссылки в БД
    for article in relevant_articles:
        link = article.split("\n")[-1]
        cur.execute("INSERT OR IGNORE INTO sent_links (link) VALUES (?)", (link,))
    conn.commit()
    conn.close()
    return relevant_articles, stats_by_source

# ------- КОМАНДА /digest -------
@dp.message(Command("digest"))
async def digest_command(message: Message, state: FSMContext):
    """Обработчик команды /digest — собирает новости, анализирует и выводит отчёт."""
    await message.answer("⚡️ Собираю свежие новости, пожалуйста подождите...")
    try:
        relevant_articles, stats = await fetch_news(DEFAULT_PROMPT)
    except OpenAIRateLimitError:
        # Превышен лимит запросов / квота OpenAI
        await message.answer("⚠️ Превышен лимит запросов к OpenAI. Попробуйте позже.")
        return
    except OpenAIAPIError:
        # Другая ошибка OpenAI (например, ключ недействителен или сеть недоступна)
        await message.answer("⚠️ Ошибка при анализе новостей: не удалось получить ответ от AI.")
        return

    # Подсчитываем суммарные показатели
    total_viewed = sum(data["total"] for data in stats.values())
    total_relevant = sum(data["relevant"] for data in stats.values())
    total_possible = sum(data["possible"] for data in stats.values())
    total_not_rel = sum(data["not_relevant"] for data in stats.values())

    # Формируем текст отчёта по источникам и общий итог
    report_lines = ["<b>Отчёт по источникам:</b>"]
    for source_title, data in stats.items():
        report_lines.append(
            f"{source_title}: просмотрено {data['total']}, "
            f"релевантных {data['relevant']}, "
            f"возможно релевантных {data['possible']}, "
            f"нерелевантных {data['not_relevant']}"
        )
    report_lines.append(
        f"\n<b>Общий итог:</b> просмотрено {total_viewed}, "
        f"релевантных {total_relevant}, "
        f"возможно релевантных {total_possible}, "
        f"нерелевантных {total_not_rel}."
    )
    digest_text = "\n".join(report_lines)

    if total_relevant > 0:
        # Добавляем кнопку для получения списка релевантных новостей
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📄 Получить новости", callback_data="get_news")]
        ])
        await message.answer(digest_text, reply_markup=keyboard)
        # Сохраняем список релевантных новостей во временное состояние пользователя
        await state.update_data(relevant_news_list=relevant_articles)
    else:
        # Отправляем отчёт пользователю
        await message.answer(digest_text)
        # Если нет релевантных новостей — предлагаем ввести свой запрос
        await message.answer("Релевантных новостей не найдено.\nВведите свой запрос для анализа новостей:")
        await state.set_state(UserState.awaiting_prompt)

@dp.message(UserState.awaiting_prompt)
async def handle_user_prompt(message: Message, state: FSMContext):
    """Обрабатывает пользовательский запрос для анализа новостей (если по умолчанию не найдено релевантных)."""
    prompt = message.text.strip()
    await message.answer("🔎 Выполняю анализ новостей по вашему запросу, пожалуйста подождите...")
    relevant_articles, stats = await fetch_news(prompt)
    if relevant_articles:
        for article in relevant_articles:
            await message.answer(article)
    else:
        await message.answer("По вашему запросу релевантных новостей не найдено.")
    # Сбрасываем состояние пользователя
    await state.clear()

# ------- CALLBACK: ПОЛУЧЕНИЕ НОВОСТЕЙ -------
@dp.callback_query(F.data == "get_news")
async def get_news_callback(callback: CallbackQuery, state: FSMContext):
    """Обработчик нажатия кнопки 'Получить новости' — высылает сохранённые релевантные новости."""
    user_data = await state.get_data()
    news_list = user_data.get("relevant_news_list", [])
    if not news_list:
        await callback.answer("Нет новостей для отображения.", show_alert=True)
        return
    for article in news_list:
        await callback.message.answer(article)
    # Убираем кнопку у исходного сообщения, чтобы исключить повторное нажатие
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer()
    # Очищаем сохранённый список релевантных новостей из состояния
    await state.update_data(relevant_news_list=[])

# ------- ЕЖЕДНЕВНАЯ РАССЫЛКА -------
async def scheduled_job():
    """Ежедневная задача (в 11:00) — собирает новости и рассылает релевантные статьи всем пользователям."""
    logging.info("Scheduled job started: fetching and sending news...")
    try:
        relevant_articles, stats = await fetch_news(DEFAULT_PROMPT)
        if relevant_articles:
            conn = sqlite3.connect(DB_FILE)
            cur = conn.cursor()
            cur.execute("SELECT id FROM users")
            user_ids = [row[0] for row in cur.fetchall()]
            conn.close()
            for user_id in user_ids:
                for article in relevant_articles:
                    try:
                        await bot.send_message(user_id, article)
                    except Exception as e:
                        logging.error(f"Failed to send news to user {user_id}: {e}", exc_info=True)
                        # Если бот заблокирован пользователем или чат недоступен — удаляем пользователя из БД
                        if "Forbidden" in str(e) or "blocked" in str(e):
                            conn2 = sqlite3.connect(DB_FILE)
                            cur2 = conn2.cursor()
                            cur2.execute("DELETE FROM users WHERE id = ?", (user_id,))
                            conn2.commit()
                            conn2.close()
            logging.info(f"Scheduled job: sent {len(relevant_articles)} news to {len(user_ids)} users.")
        else:
            logging.info("Scheduled job: no relevant news found to send.")
    except Exception as e:
        logging.error(f"Error in scheduled job: {e}", exc_info=True)

# Глобальный обработчик ошибок для диспетчера (логирует все непойманные исключения)
from aiogram.types.error_event import ErrorEvent

@dp.errors()
async def global_error_handler(event: ErrorEvent):
    logging.error(f"Unhandled exception: {event.exception}", exc_info=True)
    return True  # предотвращаем дальнейшую обработку ошибки, если она уже залогирована

# ------- ЗАПУСК ПОЛЛИНГА -------
async def main():
    # Инициализация базы данных
    init_db()
    # Планируем ежедневную задачу (каждый день в 11:00)
    scheduler.add_job(scheduled_job, "cron", hour=11, minute=0)
    scheduler.start()
    # Запуск бота (long polling)
    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("Bot started polling...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
