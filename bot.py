import logging
import asyncio
import feedparser
import sqlite3
import openai
import os
from datetime import datetime
from aiogram import Bot, Dispatcher, F
from aiogram.enums.parse_mode import ParseMode
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.utils.markdown import hbold
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.filters import Command, CommandStart
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ------------ КОНФИГ ------------
API_TOKEN = os.getenv("API_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai.api_key = OPENAI_API_KEY

# Настройка логирования в файл
logging.basicConfig(
    filename="bot_error.log",
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    encoding="utf-8",
    filemode="a"
)

# Инициализация бота и диспетчера
bot = Bot(token=API_TOKEN, default_parse_mode=ParseMode.HTML)
dp = Dispatcher(storage=MemoryStorage())
scheduler = AsyncIOScheduler()

# База данных
DB_FILE = "users.db"

# Список RSS-источников по умолчанию
DEFAULT_SOURCE_URLS = [
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
    "Ты аналитик проекта A7A5. Проанализируй новость и ответь: релевантна ли она проекту A7A5, "
    "если она касается криптовалют, цифрового рубля, CBDC, стейблкоинов или экономики Кыргызстана, "
    "экономики России, ФРС или других финансовых регуляторов? Ответь одним словом: Да или Нет."
)

# ------------ FSM ------------
class UserState(StatesGroup):
    awaiting_prompt = State()  # Состояние ожидания пользовательского промта

# ------------ ИНИЦИАЛИЗАЦИЯ БД ------------
def init_db():
    """Создает таблицы пользователей, отправленных ссылок и источников (если не существуют). 
    Также заполняет таблицу источников адресами по умолчанию."""
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY)")
    cur.execute("CREATE TABLE IF NOT EXISTS sent_links (link TEXT PRIMARY KEY)")
    cur.execute("CREATE TABLE IF NOT EXISTS sources (url TEXT PRIMARY KEY)")
    # Заполнение источников по умолчанию
    for url in DEFAULT_SOURCE_URLS:
        cur.execute("INSERT OR IGNORE INTO sources (url) VALUES (?)", (url,))
    conn.commit()
    conn.close()

# ------------ КОМАНДЫ ------------
@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    """Обработчик команды /start — регистрирует нового пользователя и отображает приветствие."""
    user_id = message.from_user.id
    # Добавляем пользователя в базу (если уже есть, ничего не произойдет)
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO users (id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()
    # Приветственное сообщение с кнопкой "Справка"
    await message.answer(
        f"{hbold('Привет!')} Я буду присылать тебе релевантные новости по теме A7A5.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Справка", callback_data="help")]])
    )

@dp.callback_query(F.data == "help")
async def help_callback(callback: CallbackQuery):
    """Обработчик нажатия кнопки 'Справка' — редактирует сообщение, выводя список команд."""
    await callback.message.edit_text(
        "/digest — начать анализ свежих новостей\n"
        "/addsource <url> — добавить источник\n"
        "/removesource <url> — удалить источник\n"
        "/listsources — показать список источников\n"
        "/help — показать справку"
    )

@dp.message(Command("help"))
async def help_command(msg: Message):
    """Обработчик команды /help — выводит список команд."""
    await msg.answer(
        "/digest — начать анализ свежих новостей\n"
        "/addsource <url> — добавить источник\n"
        "/removesource <url> — удалить источник\n"
        "/listsources — показать список источников\n"
        "/help — показать справку"
    )

@dp.message(Command("addsource"))
async def add_source(msg: Message):
    """Обработчик команды /addsource — добавляет новый RSS-источник в список."""
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        return await msg.reply("Укажи ссылку: /addsource <url>")
    url = parts[1].strip()
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO sources (url) VALUES (?)", (url,))
    conn.commit()
    conn.close()
    await msg.reply("Источник добавлен.")

@dp.message(Command("removesource"))
async def remove_source(msg: Message):
    """Обработчик команды /removesource — удаляет указанный RSS-источник из списка."""
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        return await msg.reply("Укажи ссылку: /removesource <url>")
    url = parts[1].strip()
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("DELETE FROM sources WHERE url = ?", (url,))
    conn.commit()
    conn.close()
    await msg.reply("Источник удалён.")

@dp.message(Command("listsources"))
async def list_sources(msg: Message):
    """Обработчик команды /listsources — выводит список всех текущих RSS-источников."""
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT url FROM sources")
    rows = cur.fetchall()
    conn.close()
    if not rows:
        return await msg.reply("Список источников пуст.")
    sources_list = "\n".join(f"- {r[0]}" for r in rows)
    await msg.reply(f"Источники:\n{sources_list}")

# ------------ GPT АНАЛИЗ ------------
async def gpt_check(prompt: str) -> str:
    """Отправляет запрос к OpenAI GPT-4 и возвращает ответ модели (строку 'да' или 'нет')."""
    try:
        response = await openai.ChatCompletion.acreate(
            model="gpt-4",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=5,
            temperature=0
        )
        answer = response.choices[0].message.content.strip().lower()
        return answer
    except Exception as e:
        logging.error(f"OpenAI API error: {e}", exc_info=True)
        # В случае ошибки API считаем новость нерелевантной
        return "нет"

# ------------ ОБРАБОТКА НОВОСТЕЙ ------------
async def fetch_news(prompt: str):
    """Парсит все RSS-источники и анализирует новости с помощью GPT.
    Возвращает кортеж: (список релевантных новостей, статистика по источникам)."""
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    # Получаем все источники из БД
    cur.execute("SELECT url FROM sources")
    source_rows = cur.fetchall()
    sources = [row[0] for row in source_rows]
    # Получаем уже отправленные ссылки, чтобы не дублировать
    cur.execute("SELECT link FROM sent_links")
    sent_links_set = {row[0] for row in cur.fetchall()}

    relevant_articles = []         # список сообщений с релевантными новостями
    stats_by_source = {}           # статистика по каждому источнику

    # Обходим каждый источник и парсим его RSS
    for url in sources:
        feed = feedparser.parse(url)
        # Определяем название источника для отчёта (если нет, используем URL)
        source_title = feed.feed.get("title", url) if hasattr(feed, 'feed') else url
        total_count = 0
        relevant_count = 0
        possible_count = 0
        not_relevant_count = 0

        for entry in feed.entries:
            link = entry.get("link", "")
            # Пропускаем новость, если она уже отправлялась ранее
            if not link or link in sent_links_set:
                continue
            total_count += 1
            title = entry.get("title", "(без заголовка)")
            summary = entry.get("summary", "")
            category = entry.get("category", "")
            # Некоторые RSS используют 'tags' для списка тегов
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
            # Запрос к GPT: добавляем промт и требование ответа "Да или Нет?"
            gpt_prompt = f"{prompt}\n\n{full_text}\n\nДа или Нет?"
            answer = await gpt_check(gpt_prompt)
            # Классифицируем ответ: "да" (релевантно), "нет" (нерелевантно), иначе — возможно релевантно
            if "да" in answer or "yes" in answer:
                relevant_count += 1
                # Формируем сообщение с новостью (заголовок + ссылка, в формате HTML)
                relevant_articles.append(f"<b>{title}</b>\n{link}")
                # Отмечаем эту ссылку как отправленную (в базу добавим позже)
                sent_links_set.add(link)
                # Сохраняем для вставки в БД после анализа
            elif "нет" in answer or "no" in answer:
                not_relevant_count += 1
            else:
                # Ответ не однозначно "да" или "нет" (возможно "возможно" или другая форма)
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
        # Извлекаем ссылку из сформированного сообщения (после новой строки)
        # Сообщение формата "<b>Title</b>\nhttp://link"
        link = article.split("\n")[-1]
        cur.execute("INSERT OR IGNORE INTO sent_links (link) VALUES (?)", (link,))
    conn.commit()
    conn.close()
    return relevant_articles, stats_by_source

# ------------ КОМАНДА /digest ------------
@dp.message(Command("digest"))
async def digest_command(message: Message, state: FSMContext):
    """Обработчик команды /digest — выполняет анализ новостей и выводит отчёт."""
    await message.answer("⚡️ Собираю свежие новости, пожалуйста подождите...")
    # Запускаем анализ новостей с дефолтным промтом
    relevant_articles, stats = await fetch_news(DEFAULT_PROMPT)
    # Подсчитываем общие итоги по всем источникам
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
        # Кнопка для получения подробностей (релевантные новости)
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📄 Получить новости", callback_data="get_news")]
        ])
        # Отправляем отчёт с кнопкой
        await message.answer(digest_text, reply_markup=keyboard)
        # Сохраняем список релевантных новостей во временное хранилище (для данного пользователя)
        # Используем FSM storage (данные состояния) без переключения состояния
        await state.update_data(relevant_news_list=relevant_articles)
    else:
        # Если релевантных новостей нет – запрашиваем у пользователя промт для повторного анализа
        await message.answer("Релевантных новостей не найдено.\nВведите свой запрос для анализа новостей:")
        await state.set_state(UserState.awaiting_prompt)

@dp.message(UserState.awaiting_prompt)
async def handle_user_prompt(message: Message, state: FSMContext):
    """Обрабатывает пользовательский промт для анализа новостей (после отсутствия релевантных новостей по умолчанию)."""
    prompt = message.text.strip()
    await message.answer("🔎 Выполняю анализ новостей по вашему запросу, пожалуйста подождите...")
    # Повторно анализируем все новости с использованием пользовательского промта
    relevant_articles, stats = await fetch_news(prompt)
    if relevant_articles:
        # Отправляем каждую найденную релевантную новость
        for article in relevant_articles:
            await message.answer(article)
    else:
        await message.answer("По вашему запросу релевантных новостей не найдено.")
    # Сбрасываем состояние пользователя
    await state.clear()

# ------------ CALLBACK: ПОЛУЧЕНИЕ НОВОСТЕЙ ------------
@dp.callback_query(F.data == "get_news")
async def get_news_callback(callback: CallbackQuery, state: FSMContext):
    """Обработчик нажатия кнопки 'Получить новости' — высылает релевантные новости, найденные в дайджесте."""
    # Получаем сохранённый список релевантных новостей для этого пользователя
    user_data = await state.get_data()
    news_list = user_data.get("relevant_news_list", [])
    if not news_list:
        await callback.answer("Нет новостей для отображения.", show_alert=True)
        return
    # Отправляем каждую релевантную новость сообщением
    for article in news_list:
        await callback.message.answer(article)
    # Убираем у исходного сообщения кнопку, чтобы не нажать повторно
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer()  # Закрываем всплывающее "часики"
    # Очищаем сохранённые новости из состояния
    await state.update_data(relevant_news_list=[])

# ------------ ЕЖЕДНЕВНАЯ ЗАДАЧА ------------
async def scheduled_job():
    """Ежедневная задача (запускается в 11:00) — собирает новости и рассылает релевантные всем пользователям."""
    logging.info("Scheduled job started: fetching and sending news...")
    try:
        # Используем дефолтный промт для анализа
        relevant_articles, stats = await fetch_news(DEFAULT_PROMPT)
        # Если есть новые релевантные статьи, рассылаем их пользователям
        if relevant_articles:
            # Получаем всех пользователей
            conn = sqlite3.connect(DB_FILE)
            cur = conn.cursor()
            cur.execute("SELECT id FROM users")
            user_ids = [row[0] for row in cur.fetchall()]
            conn.close()
            # Рассылаем каждому пользователю список новостей
            for user_id in user_ids:
                for article in relevant_articles:
                    try:
                        await bot.send_message(user_id, article)
                    except Exception as e:
                        # Логируем ошибку отправки (например, бот заблокирован пользователем)
                        logging.error(f"Failed to send news to user {user_id}: {e}", exc_info=True)
                        # Если бот заблокирован или чат недоступен — удаляем пользователя из БД
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

# ------------ СТАРТ ПОЛЛИНГА ------------
async def main():
    # Инициализация базы данных и таблиц
    init_db()
    # Старт планировщика (ежедневно в 11:00)
    scheduler.add_job(scheduled_job, "cron", hour=11, minute=0)
    scheduler.start()
    # Запуск бота (long polling)
    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("Bot started polling...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
