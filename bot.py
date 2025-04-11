import logging
import asyncio
import os
import time
import sqlite3
import feedparser
import openai
from datetime import datetime, timedelta
from typing import List, Dict
from dotenv import load_dotenv

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
import html

# Загрузка переменных окружения из .env файла
load_dotenv()
# Установка API ключей и токенов (из переменных окружения)
API_TOKEN = os.getenv("API_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai.api_key = OPENAI_API_KEY

# Настройка логирования в файл
file_handler = logging.FileHandler("bot_error.log", mode="a", encoding="utf-8")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[file_handler]
)

# Константы и настройки бота
DB_FILE = "users.db"
# Список RSS-источников по умолчанию (для первоначального наполнения базы данных)
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

# Промт по умолчанию для GPT-фильтра
DEFAULT_PROMPT = (
    "Ты аналитик криптовалютного проекта A7A5. Проанализируй новость и ответь: "
    "может ли она быть потенциально релевантной проекту A7A5, если она касается "
    "криптовалют, стейблкоинов, цифрового рубля, экономики Кыргызстана, финансовых "
    "регуляторов или мировой криптоинфраструктуры? Ответь одним словом: Да или Нет."
)

# Инициализация бота, диспетчера и планировщика
bot = Bot(token=API_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
scheduler = AsyncIOScheduler()

# ------- FSM State --------
class UserState(StatesGroup):
    awaiting_prompt = State()  # Состояние ожидания пользовательского промта

# ------- ИНИЦИАЛИЗАЦИЯ БД -------
def init_db():
    """Создает таблицы пользователей, источников и отправленных ссылок (если не существуют)."""
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY)")
    cur.execute("CREATE TABLE IF NOT EXISTS sent_links (link TEXT PRIMARY KEY)")
    cur.execute("CREATE TABLE IF NOT EXISTS sources (id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL UNIQUE)")
    # Первоначальное заполнение таблицы sources из списка RSS_FEEDS
    cur.execute("SELECT COUNT(*) FROM sources")
    count = cur.fetchone()[0]
    if count == 0:
        for url in RSS_FEEDS:
            cur.execute("INSERT OR IGNORE INTO sources (url) VALUES (?)", (url,))
    conn.commit()
    conn.close()

# ------- ХЕНДЛЕРЫ КОМАНД -------
@dp.message(CommandStart())
async def start_command(message: Message, state: FSMContext):
    """Обработчик команды /start — регистрирует нового пользователя и отображает приветствие."""
    user_id = message.from_user.id
    # Добавляем пользователя в базу (если уже есть, ничего не произойдет)
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO users (id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()
    # Приветственное сообщение с кнопкой "Справка"
    greeting_text = f"{hbold('Привет!')} Я буду присылать тебе релевантные новости по теме A7A5."
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Справка", callback_data="help")]
    ])
    await message.answer(greeting_text, reply_markup=keyboard)

@dp.callback_query(F.data == "help")
async def help_callback(callback: CallbackQuery):
    """Обработчик нажатия кнопки 'Справка' — редактирует сообщение, выводя список команд."""
    help_text = (
        "/digest — собрать и проанализировать свежие новости\n"
        "/addsource <RSS-URL> — добавить источник\n"
        "/removesource <URL или ID> — удалить источник\n"
        "/listsources — показать все источники\n"
        "/help — показать эту справку"
    )
    await callback.message.edit_text(help_text)

@dp.message(Command("help"))
async def help_command(msg: Message):
    """Обработчик команды /help — выводит список команд."""
    help_text = (
        "/digest — собрать и проанализировать свежие новости\n"
        "/addsource <RSS-URL> — добавить источник\n"
        "/removesource <URL или ID> — удалить источник\n"
        "/listsources — показать все источники\n"
        "/help — показать эту справку"
    )
    await msg.answer(help_text)

@dp.message(Command("addsource"))
async def add_source_command(message: Message):
    """Обработчик команды /addsource — добавляет новый RSS источник по URL."""
    # Ожидается, что команда вызывается вместе с URL (через пробел)
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Использование: /addsource <URL RSS-ленты>")
        return
    url = args[1].strip()
    if not url:
        await message.answer("Пожалуйста, укажите URL RSS-ленты после команды.")
        return
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    # Проверяем, есть ли уже такой источник
    cur.execute("SELECT id FROM sources WHERE url = ?", (url,))
    exists = cur.fetchone()
    if exists:
        conn.close()
        await message.answer("Этот источник уже добавлен.")
    else:
        try:
            cur.execute("INSERT INTO sources (url) VALUES (?)", (url,))
            conn.commit()
            conn.close()
            await message.answer(f"Источник добавлен: {html.escape(url)}")
        except Exception as e:
            logging.error(f"Failed to add source {url}: {e}", exc_info=True)
            conn.close()
            await message.answer("Не удалось добавить источник. Убедитесь, что URL указан корректно.")

@dp.message(Command("removesource"))
async def remove_source_command(message: Message):
    """Обработчик команды /removesource — удаляет указанный RSS источник (по URL или ID)."""
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Использование: /removesource <URL или ID источника>")
        return
    target = args[1].strip()
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    if target.isdigit():
        source_id = int(target)
        # Удаляем по ID
        cur.execute("SELECT url FROM sources WHERE id = ?", (source_id,))
        row = cur.fetchone()
        if not row:
            conn.close()
            await message.answer("Источник с указанным ID не найден.")
            return
        url_to_remove = row[0]
        cur.execute("DELETE FROM sources WHERE id = ?", (source_id,))
        conn.commit()
        conn.close()
        await message.answer(f"Источник удалён: {html.escape(url_to_remove)}")
    else:
        url = target
        cur.execute("DELETE FROM sources WHERE url = ?", (url,))
        changes = cur.rowcount
        conn.commit()
        conn.close()
        if changes:
            await message.answer(f"Источник удалён: {html.escape(url)}")
        else:
            await message.answer("Указанный источник не найден в базе.")

@dp.message(Command("listsources"))
async def list_sources_command(message: Message):
    """Обработчик команды /listsources — выводит список всех текущих RSS-источников."""
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT id, url FROM sources")
    sources_list = cur.fetchall()
    conn.close()
    if not sources_list:
        await message.answer("Список источников пуст. Добавьте новый источник командой /addsource.")
    else:
        response_lines = [hbold("Текущие источники:")]
        for source_id, url in sources_list:
            response_lines.append(f"{source_id}. {html.escape(url)}")
        response_text = "\n".join(response_lines)
        await message.answer(response_text)

# ------- GPT АНАЛИЗ -------
from openai import OpenAI

client = OpenAI(api_key=OPENAI_API_KEY)

async def gpt_check(prompt: str) -> str:
    """Отправляет запрос к OpenAI GPT-4 и возвращает ответ модели ('да' или 'нет')."""
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
        answer = response.choices[0].message.content.strip().lower()
        return answer
    except Exception as e:
        logging.error(f"OpenAI API error: {e}", exc_info=True)
        return "нет"

# ------- ОБРАБОТКА НОВОСТЕЙ -------
async def fetch_news(prompt: str):
    """Парсит все RSS-источники и анализирует новости с помощью GPT.
    Возвращает кортеж: (список релевантных новостей, статистика по источникам)."""
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    # Получаем уже отправленные ссылки, чтобы не дублировать
    cur.execute("SELECT link FROM sent_links")
    sent_links_set = {row[0] for row in cur.fetchall()}
    relevant_articles: List[str] = []   # список сообщений с релевантными новостями (для отправки)
    stats_by_source: Dict[str, Dict[str, int]] = {}     # статистика по каждому источнику

    now = datetime.now()
    one_week_ago = now - timedelta(days=7)

    # Получаем все источники из базы данных и парсим их RSS
    cur.execute("SELECT url FROM sources")
    sources = [row[0] for row in cur.fetchall()]
    for url in sources:
        feed = feedparser.parse(url)
        # Название источника для отчёта (если есть, иначе используем URL)
        source_title = feed.feed.get("title", url) if hasattr(feed, 'feed') else url
        total_count = 0
        relevant_count = 0
        possible_count = 0
        not_relevant_count = 0

        for entry in feed.entries:
            link = entry.get("link", "")
            # Пропускаем, если нет ссылки или уже отправляли эту новость ранее
            if not link or link in sent_links_set:
                continue
            # Пропускаем новости старше 7 дней (если дата указана)
            entry_date = None
            if 'published_parsed' in entry and entry.published_parsed:
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
            tags = []
            if "tags" in entry:
                tags = [t.get("term", "") for t in entry.tags]
            content = ""
            if "content" in entry and entry.content:
                content = entry.content[0].get("value", "")

            # Формируем полный текст для анализа (заголовок, описание, категория, теги, контент)
            full_text = (
                f"Заголовок: {title}\n"
                f"Описание: {summary}\n"
                f"Категория: {category}\n"
                f"Теги: {', '.join(tags)}\n"
                f"Контент: {content}"
            )
            # Промт для GPT (объединяем промт и текст новости, требуем ответ Да/Нет)
            gpt_prompt = f"{prompt}\n\n{full_text}\n\nДа или Нет?"
            answer = await gpt_check(gpt_prompt)
            # Классифицируем ответ: "да" (релевантно), "нет" (нерелевантно), иначе — возможно релевантно
            if "да" in answer or "yes" in answer:
                relevant_count += 1
                # Формируем сообщение для пользователя: заголовок (bold) и ссылка
                relevant_articles.append(f"<b>{title}</b>\n{link}")
                # Помечаем ссылку как отправленную
                sent_links_set.add(link)
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

    # Добавляем новые релевантные ссылки в базу (таблица sent_links)
    for article in relevant_articles:
        link = article.split("\n")[-1]
        cur.execute("INSERT OR IGNORE INTO sent_links (link) VALUES (?)", (link,))
    conn.commit()
    conn.close()
    return relevant_articles, stats_by_source

# ------- КОМАНДА /digest -------
@dp.message(Command("digest"))
async def digest_command(message: Message, state: FSMContext):
    """Обработчик команды /digest — собирает новости и выводит отчёт по источникам."""
    await message.answer("⚡️ Собираю свежие новости, пожалуйста подождите...")
    # Анализируем новости с использованием дефолтного промта
    relevant_articles, stats = await fetch_news(DEFAULT_PROMPT)
    # Суммируем общие показатели по всем источникам
    total_viewed = sum(data["total"] for data in stats.values())
    total_relevant = sum(data["relevant"] for data in stats.values())
    total_possible = sum(data["possible"] for data in stats.values())
    total_not_rel = sum(data["not_relevant"] for data in stats.values())
    # Если найдены релевантные новости
    if total_relevant > 0:
        # Формируем текст отчёта по каждому источнику
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
        # Кнопка для получения списка релевантных новостей
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📄 Получить новости", callback_data="get_news")]
        ])
        # Отправляем отчёт с кнопкой
        await message.answer(digest_text, reply_markup=keyboard)
        # Сохраняем список релевантных новостей во временное состояние пользователя
        await state.update_data(relevant_news_list=relevant_articles)
    else:
        # Если релевантных новостей не найдено — предлагаем ввести пользовательский запрос
        await message.answer("Релевантных новостей не найдено.\nВведите свой запрос для анализа новостей:")
        await state.set_state(UserState.awaiting_prompt)

# ------- ХЕНДЛЕР ПОЛЬЗОВАТЕЛЬСКОГО ПРОМПТА -------
@dp.message(UserState.awaiting_prompt)
async def handle_user_prompt(message: Message, state: FSMContext):
    """Обрабатывает пользовательский промт для анализа новостей (если по умолчанию не найдено релевантных)."""
    prompt = message.text.strip()
    await message.answer("🔎 Выполняю анализ новостей по вашему запросу, пожалуйста подождите...")
    # Повторно анализируем все новости с использованием пользовательского промта
    relevant_articles, stats = await fetch_news(prompt)
    if relevant_articles:
        # Отправляем каждую найденную релевантную новость отдельным сообщением
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
    # Отправляем каждую релевантную новость пользователю
    for article in news_list:
        await callback.message.answer(article)
    # Убираем у исходного сообщения кнопку, чтобы избежать повторного использования
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer()
    # Очищаем список сохранённых новостей из состояния
    await state.update_data(relevant_news_list=[])

# ------- ЕЖЕДНЕВНАЯ РАССЫЛКА -------
async def scheduled_job():
    """Ежедневная задача (выполняется в 11:00) — собирает новости и рассылает релевантные всем пользователям."""
    logging.info("Scheduled job started: fetching and sending news...")
    try:
        # Анализируем новости с дефолтным промтом
        relevant_articles, stats = await fetch_news(DEFAULT_PROMPT)
        # Если появились новые релевантные статьи, рассылаем их всем пользователям
        if relevant_articles:
            conn = sqlite3.connect(DB_FILE)
            cur = conn.cursor()
            cur.execute("SELECT id FROM users")
            user_ids = [row[0] for row in cur.fetchall()]
            conn.close()
            # Рассылаем каждому пользователю все релевантные новости
            for user_id in user_ids:
                for article in relevant_articles:
                    try:
                        await bot.send_message(user_id, article)
                    except Exception as e:
                        # Логируем ошибку отправки (например, если бот заблокирован пользователем)
                        logging.error(f"Failed to send news to user {user_id}: {e}", exc_info=True)
                        # Если бот заблокирован или чат недоступен — удаляем пользователя из базы
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
