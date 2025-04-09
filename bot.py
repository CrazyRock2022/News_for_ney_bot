async def on_startup(_):
    # Инициализация базы данных
    init_db()

    # Удаляем старые вебхуки
    await bot.delete_webhook(drop_pending_updates=True)

    # Убираем вебхук, чтобы точно не было установлено
    await bot.set_webhook(url='')

    # Запуск планировщика
    scheduler.add_job(scheduled_job, "cron", hour=11, minute=0)
    scheduler.start()

async def scheduled_job():
    # Ваш код для выполнения задачи, например, отправка новостей
    print("Scheduled job running!")
import logging
import asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.types import ParseMode, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils import executor
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiogram.dispatcher.filters import CommandStart
import feedparser
import sqlite3
from datetime import datetime, timedelta
import openai
import os
from typing import Union

API_TOKEN = os.getenv("API_TOKEN")  # замените на свой
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai.api_key = OPENAI_API_KEY

logging.basicConfig(level=logging.INFO)
bot = Bot(token=API_TOKEN)
dp = Dispatcher(bot)
scheduler = AsyncIOScheduler()
DB_FILE = "users.db"

# Фиксированные темы для фильтрации новостей
FIXED_TOPICS = [
    "A7A5",
    "Кыргызстан",
    "kyrgyzstan",
    "крипта",
    "crypto",
    "цифровой рубль",
    "digital ruble",
    "стейблкоин",
    "stablecoin",
    "CBDC",
    "digital currency"
]

# ---------- ИНИЦИАЛИЗАЦИЯ БД ----------
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY)")
    cur.execute("CREATE TABLE IF NOT EXISTS sent_links (link TEXT PRIMARY KEY)")
    cur.execute("CREATE TABLE IF NOT EXISTS sources (url TEXT PRIMARY KEY)")
    # начальные источники
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

# ---------- КОМАНДЫ ----------

@dp.message_handler(CommandStart())
async def start(message: types.Message):
    user_id = message.from_user.id
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO users (id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()

    keyboard = InlineKeyboardMarkup().add(
        InlineKeyboardButton("Справка", callback_data="help")
    )
    await message.answer("Привет! Я пришлю тебе свежие и релевантные новости по теме A7A5, крипты и цифрового рубля.", reply_markup=keyboard)

# ---------- СПРАВКА ----------
HELP_TEXT = (
    "Вот что я умею:\n\n"
    "/digest — получить свежие релевантные новости\n"
    "/addsource <url> — добавить сайт/RSS источник\n"
    "/removesource <url> — удалить источник\n"
    "/listsources — показать все источники\n"
    "/help — справка\n"
)

@dp.callback_query_handler(lambda c: c.data == "help")
async def help_callback(callback: types.CallbackQuery):
    await callback.message.edit_text(HELP_TEXT)

@dp.message_handler(commands=["help"])
async def help_command(message: types.Message):
    await message.answer(HELP_TEXT)

@dp.message_handler(commands=["digest"])
async def send_digest(message: types.Message):
    articles = await get_news()  # Получаем новости

    # Статистика по каждому источнику
    sources_stats = {}
    for article in articles:
        source = article['source']
        if source not in sources_stats:
            sources_stats[source] = {'relevant': 0, 'possible': 0, 'irrelevant': 0, 'total': 0}

        if article['status'] == 'Relevant':
            sources_stats[source]['relevant'] += 1
        elif article['status'] == 'Irrelevant':
            sources_stats[source]['irrelevant'] += 1
        elif article['status'] == 'Possible':
            sources_stats[source]['possible'] += 1

        sources_stats[source]['total'] += 1

    # Отправляем статистику
    stats_message = "Результаты:\n\n"
    total_relevant = total_possible = total_irrelevant = 0

    for source, stats in sources_stats.items():
        stats_message += f"{source} — прочитано {stats['total']} новостей, из которых {stats['relevant']} релевантных, {stats['possible']} возможно релевантных, {stats['irrelevant']} нерелевантных\n"
        total_relevant += stats['relevant']
        total_possible += stats['possible']
        total_irrelevant += stats['irrelevant']

    stats_message += f"\nИтого: прочитано {total_relevant + total_possible + total_irrelevant} новостей, из которых {total_relevant} релевантных, {total_possible} возможно релевантных, {total_irrelevant} нерелевантных.\n"

    # Кнопка для получения новостей
    keyboard = InlineKeyboardMarkup().add(
        InlineKeyboardButton("Получить новости", callback_data="get_news")
    )

    await message.answer(stats_message, reply_markup=keyboard)

# ---------- Кнопка "Получить новости" ----------
@dp.callback_query_handler(lambda c: c.data == "get_news")
async def get_news_callback(callback: types.CallbackQuery):
    # Сначала отправляем релевантные новости
    articles = await get_news()
    relevant_articles = [article for article in articles if article['status'] == 'Relevant']
    
    if relevant_articles:
        for article in relevant_articles:
            await callback.message.answer(f"<b>{article['title']}</b>\n{article['link']}", parse_mode=ParseMode.HTML)
    else:
        # Если релевантных новостей нет, запрашиваем у пользователя ввод
        await callback.message.answer("Нет релевантных новостей. Пожалуйста, введите промт для поиска:")
        await dp.register_message_handler(handle_custom_prompt, state=None)

# ---------- ОБРАБОТЧИК ПРОМТА ПОЛЬЗОВАТЕЛЯ ----------
async def handle_custom_prompt(message: types.Message):
    user_prompt = message.text  # Получаем промт от пользователя
    await message.answer(f"Ваш запрос: {user_prompt}. Я ищу новости, связанные с этим.")
    
    # Пример вызова GPT с запросом от пользователя
    response = await openai.ChatCompletion.acreate(
        model="gpt-4-turbo",
        messages=[{"role": "user", "content": f"Найди новости по теме: {user_prompt}"}],
        max_tokens=100
    )
    answer = response.choices[0].message['content'].strip()
    await message.answer(f"Результаты поиска:\n{answer}")

# ---------- GPT-ФИЛЬТР ----------
async def is_relevant(title, summary, tags=None, category=None, content=None):
    full_context = f"Заголовок: {title}\nОписание: {summary}"
    if category:
        full_context += f"\nКатегория: {category}"
    if tags:
        full_context += f"\nТеги: {', '.join(tags)}"
    if content:
        full_context += f"\nПолный текст: {content[:1000]}..."

    prompt = (
        f"Ты аналитик криптовалютного проекта A7A5. "
        f"Проанализируй новость и ответь: может ли она быть потенциально релевантной проекту A7A5, "
        f"если она касается криптовалют, стейблкоинов, цифрового рубля, экономики Кыргызстана, финансовых регуляторов, "
        f"или мировой криптоинфраструктуры?\n\n"
        f"{full_context}\n\n"
        f"Ответь одним словом: Да или Нет."
    )

    try:
        response = await openai.ChatCompletion.acreate(
            model="gpt-4-turbo",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=3
        )
        answer = response.choices[0].message['content'].strip().lower()
        return "да" in answer
    except Exception as e:
        logging.warning(f"OpenAI error: {e}")
        return False

# ---------- ПАРСИНГ ----------
async def get_news():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT url FROM sources")
    sources = [r[0] for r in cur.fetchall()]
    new_articles = []

    for url in sources:
        feed = feedparser.parse(url)
        for entry in feed.entries:
            title = entry.title
            link = entry.link
            summary = entry.get("summary", "")
            published = entry.get("published_parsed")

            if published:
                pub_time = datetime(*published[:6])
                if datetime.utcnow() - pub_time > timedelta(hours=168):
                    continue

            cur.execute("SELECT 1 FROM sent_links WHERE link=?", (link,))
            if not cur.fetchone():
                tags = [t['term'] for t in entry.get('tags', [])] if 'tags' in entry else []
                category = entry.get('category')
                content = entry.get('content', [{}])[0].get('value') if 'content' in entry else None

                status = await is_relevant(title, summary, tags, category, content)
                if status:
                    new_articles.append({
                        'title': title,
                        'link': link,
                        'status': 'Relevant',
                        'source': url
                    })
                elif status is None:
                    new_articles.append({
                        'title': title,
                        'link': link,
                        'status': 'Possible',
                        'source': url
                    })
                else:
                    new_articles.append({
                        'title': title,
                        'link': link,
                        'status': 'Irrelevant',
                        'source': url
                    })

    conn.commit()
    conn.close()
    return new_articles

# ---------- НАЧАЛО РАБОТЫ БОТА И СТАРТ ПОЛЛИНГА ----------
async def on_startup(_):
    # Инициализация базы данных
    init_db()

    # Удаление старых вебхуков
    await bot.delete_webhook(drop_pending_updates=True)

    # Убираем вебхук
    await bot.set_webhook(url='')

    # Запуск планировщика
    scheduler.add_job(scheduled_job, "cron", hour=11, minute=0)
    scheduler.start()

# Здесь бот будет ждать обновлений
executor.start_polling(dp, skip_updates=True, on_startup=on_startup)
