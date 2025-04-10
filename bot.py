import logging
import asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.types import ParseMode, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils import executor
from aiogram.dispatcher.filters import CommandStart
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import sqlite3
import feedparser
from datetime import datetime, timedelta
import os
from openai import AsyncOpenAI

# --- Конфигурация ---
API_TOKEN = os.getenv("API_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = AsyncOpenAI(api_key=OPENAI_API_KEY)
bot = Bot(token=API_TOKEN)
dp = Dispatcher(bot)
scheduler = AsyncIOScheduler()
DB_FILE = "users.db"
DEFAULT_PROMPT = (
    "Ты аналитик криптовалютного проекта A7A5. Проанализируй новость и ответь: "
    "может ли она быть потенциально релевантной проекту A7A5, если она касается криптовалют, "
    "стейблкоинов, цифрового рубля, экономики Кыргызстана, финансовых регуляторов или мировой криптоинфраструктуры?"
)

# --- Логирование ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("bot_error.log"),
        logging.StreamHandler()
    ]
)

# --- Инициализация БД ---
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY)")
    cur.execute("CREATE TABLE IF NOT EXISTS sent_links (link TEXT PRIMARY KEY)")
    cur.execute("CREATE TABLE IF NOT EXISTS sources (url TEXT PRIMARY KEY)")
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

# --- Обработка /start ---
@dp.message_handler(CommandStart())
async def start(message: types.Message):
    user_id = message.from_user.id
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO users (id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()
    keyboard = InlineKeyboardMarkup().add(InlineKeyboardButton("Справка", callback_data="help"))
    await message.answer("Привет! Я пришлю тебе релевантные новости по теме A7A5, крипты и цифрового рубля.", reply_markup=keyboard)

# --- Обработка /help ---
@dp.message_handler(commands=["help"])
async def help_command(message: types.Message):
    await message.answer(
        "/digest — получить свежие релевантные новости\n"
        "/addsource <url> — добавить источник\n"
        "/removesource <url> — удалить источник\n"
        "/listsources — список источников"
    )

@dp.callback_query_handler(lambda c: c.data == "help")
async def help_callback(callback: types.CallbackQuery):
    await callback.message.edit_text(
        "/digest — получить свежие релевантные новости\n"
        "/addsource <url> — добавить источник\n"
        "/removesource <url> — удалить источник\n"
        "/listsources — список источников"
    )

# --- Добавить источник ---
@dp.message_handler(commands=["addsource"])
async def add_source(message: types.Message):
    url = message.get_args()
    if url:
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute("INSERT OR IGNORE INTO sources (url) VALUES (?)", (url,))
        conn.commit()
        conn.close()
        await message.answer("Источник добавлен.")
    else:
        await message.answer("Укажи URL источника.")

# --- Удалить источник ---
@dp.message_handler(commands=["removesource"])
async def remove_source(message: types.Message):
    url = message.get_args()
    if url:
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute("DELETE FROM sources WHERE url = ?", (url,))
        conn.commit()
        conn.close()
        await message.answer("Источник удалён.")
    else:
        await message.answer("Укажи URL источника.")

# --- Показать источники ---
@dp.message_handler(commands=["listsources"])
async def list_sources(message: types.Message):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT url FROM sources")
    sources = [r[0] for r in cur.fetchall()]
    conn.close()
    await message.answer("\n".join(sources) or "Источники не заданы.")

# --- Обработка /digest ---
@dp.message_handler(commands=["digest"])
async def send_digest(message: types.Message):
    articles = await get_news(DEFAULT_PROMPT)
    relevant_articles = [a for a in articles if a['status'] == 'Relevant']

    if relevant_articles:
        for article in relevant_articles[:10]:
            await message.answer(f"<b>{article['title']}</b>\n{article['link']}", parse_mode=ParseMode.HTML)
    else:
        keyboard = InlineKeyboardMarkup().add(
            InlineKeyboardButton("Задать свой промт", callback_data="custom_prompt")
        )
        await message.answer("Нет релевантных новостей. Хочешь задать свой промт для поиска?", reply_markup=keyboard)

@dp.callback_query_handler(lambda c: c.data == "custom_prompt")
async def ask_prompt(callback: types.CallbackQuery):
    await callback.message.answer("Введите свой промт для поиска:")
    dp.register_message_handler(handle_custom_prompt, state=None)

async def handle_custom_prompt(message: types.Message):
    user_prompt = message.text
    articles = await get_news(user_prompt)
    relevant_articles = [a for a in articles if a['status'] == 'Relevant']
    if relevant_articles:
        for article in relevant_articles[:10]:
            await message.answer(f"<b>{article['title']}</b>\n{article['link']}", parse_mode=ParseMode.HTML)
    else:
        await message.answer("По этому промту тоже нет релевантных новостей.")
    dp.message_handlers.unregister(handle_custom_prompt)

# --- Функция проверки релевантности ---
async def is_relevant(title, summary, category=None, tags=None, content=None, prompt=DEFAULT_PROMPT):
    context = f"Заголовок: {title}\nОписание: {summary}"
    if category:
        context += f"\nКатегория: {category}"
    if tags:
        context += f"\nТеги: {tags}"
    if content:
        context += f"\nКонтент: {content[:1000]}"

    try:
        response = await client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": f"{prompt}\n\n{context}\n\nОтветь: Да или Нет."}],
            max_tokens=3
        )
        answer = response.choices[0].message.content.strip().lower()
        logging.info(f"GPT: {answer} | {title}")
        return "да" in answer
    except Exception as e:
        logging.error(f"OpenAI error: {e}")
        return False

# --- Парсинг новостей ---
async def get_news(prompt: str):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT url FROM sources")
    sources = [r[0] for r in cur.fetchall()]
    new_articles = []

    for url in sources:
        feed = feedparser.parse(url)
        for entry in feed.entries:
            title = entry.get("title")
            link = entry.get("link")
            summary = entry.get("summary", "")
            published = entry.get("published_parsed")
            if published:
                pub_time = datetime(*published[:6])
                if datetime.utcnow() - pub_time > timedelta(days=7):
                    continue
            cur.execute("SELECT 1 FROM sent_links WHERE link=?", (link,))
            if cur.fetchone():
                continue

            content = entry.get("content", [{}])[0].get("value", "")
            category = entry.get("category", "")
            tags = ", ".join([t['term'] for t in entry.get("tags", [])]) if "tags" in entry else ""

            relevant = await is_relevant(title, summary, category, tags, content, prompt)
            status = "Relevant" if relevant else "Irrelevant"
            cur.execute("INSERT INTO sent_links (link) VALUES (?)", (link,))
            new_articles.append({'title': title, 'link': link, 'status': status})

    conn.commit()
    conn.close()
    return new_articles

# --- Запуск по расписанию ---
async def scheduled_job():
    logging.info("Scheduled digest запущен.")
    articles = await get_news(DEFAULT_PROMPT)
    relevant_articles = [a for a in articles if a['status'] == 'Relevant']
    if relevant_articles:
        for article in relevant_articles[:3]:
            await bot.send_message(chat_id=os.getenv("ADMIN_ID"), text=f"{article['title']}\n{article['link']}")

# --- Старт бота ---
async def on_startup(_):
    init_db()
    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_webhook(url='')
    scheduler.add_job(scheduled_job, "cron", hour=11, minute=0)
    scheduler.start()

executor.start_polling(dp, skip_updates=True, on_startup=on_startup)
