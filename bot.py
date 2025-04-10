import os
import asyncio
import logging
import sqlite3
from datetime import datetime, date, time, timedelta
from time import mktime

import feedparser
from openai import OpenAI

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command, CommandStart
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext

# Включаем логирование для отладки
logging.basicConfig(level=logging.INFO)

# Настройка API-ключа OpenAI из переменной окружения
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    logging.error("Не задан API ключ OpenAI. Установите переменную окружения OPENAI_API_KEY.")
    exit(1)

# Инициализируем клиент OpenAI
openai_client = OpenAI(api_key=OPENAI_API_KEY)

# Путь к файлу базы данных
DB_FILE = "newsbot.db"

# Список RSS-источников по умолчанию (тематика: криптовалюта, экономика и т.д.)
default_sources = [
    # Криптовалюта (мировые, англоязычные)
    "https://cryptopanic.com/news/rss/",
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
    "https://www.coindesk.com/arc/outboundfeeds/rss/?outputType=xml",
    # Экономика и финансы РФ и СНГ
    "https://www.cbr.ru/rss/",                            # Банк России (новости)
    "http://www.finmarket.ru/rss/",                       # Финансы и аналитика
    "https://rssexport.rbc.ru/rbcnews/news/eco/index.rss",# РБК - Экономика
    "https://www.kommersant.ru/RSS/news.xml",             # Коммерсант - Новости
    "https://www.forbes.ru/rss",                          # Forbes RU - Бизнес и финансы
    "https://24.kg/rss/",                                 # 24.kg - Кыргызстан
    "https://akipress.org/rss/news.rss",                  # AKIpress - Кыргызстан
    "https://www.themoscowtimes.com/rss",                 # The Moscow Times - экономика России (англ)
    # Международные фин. организации
    "https://blogs.imf.org/feed/",                        # Блог МВФ
    "https://www.bis.org/rss/home.xml",                   # BIS - Банковские регуляторы
]

# Класс состояний FSM для ожидания пользовательского запроса
class DigestStates(StatesGroup):
    waiting_for_prompt = State()

# Функция проверки релевантности новости через GPT
async def is_relevant(title: str, description: str, user_prompt: str = None) -> bool:
    """
    Анализирует новость с помощью GPT и возвращает True, если новость релевантна.
    Если user_prompt указан, использует его как контекст; иначе использует дефолтную тематику.
    """
    # Формируем сообщения для ChatCompletion
    if user_prompt is None:
        # Дефолтный промт по тематике A7A5 (криптовалюта, стейблкоины, цифровой рубль, экономика Кыргызстана)
        system_content = "Вы — помощник, анализирующий новости на релевантность темам: криптовалюты, стейблкоины, цифровой рубль, экономика Кыргызстана. Отвечайте только \"Да\" или \"Нет\"."
        user_content = f"Новость: {title}\n{description}\nВопрос: Релевантна ли эта новость указанной тематике? Ответьте только \"Да\" или \"Нет\"."
    else:
        # Пользовательский промт в контексте
        system_content = "Вы — помощник, анализирующий новости на релевантность пользовательскому запросу. Отвечайте только \"Да\" или \"Нет\"."
        user_content = f"Запрос пользователя: {user_prompt}\nНовость: {title}\n{description}\nВопрос: Релевантна ли эта новость запросу пользователя? Ответьте только \"Да\" или \"Нет\"."
    messages = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_content}
    ]
    try:
        # Вызов ChatCompletion
        response = await openai_client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=messages,
            temperature=0,
            max_tokens=5
        )
        result_text = response.choices[0].message.content.strip()
    except Exception as e:
        # Логируем ошибку и считаем новость нерелевантной при сбое
        logging.error(f"Ошибка при обращении к OpenAI: {e}")
        return False
    # Проверяем ответ модели
    result_text_lower = result_text.lower()
    if result_text_lower.startswith("да") or result_text_lower.startswith("yes"):
        return True
    else:
        return False

# Функция для получения свежих новостей и фильтрации их по релевантности
async def get_relevant_articles(user_prompt: str = None):
    """
    Собирает новости из всех источников и возвращает список релевантных статей (словари с 'title' и 'link').
    Если user_prompt не указан, используется дефолтная тематика для фильтрации.
    Помечает отправленные новости в базе, чтобы не отправлять их снова.
    """
    # Подключаемся к базе и получаем список всех источников
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT url FROM sources")
    rows = cur.fetchall()
    sources = [row[0] for row in rows] if rows else []
    conn.close()
    if not sources:
        # Если нет источников в базе, используем список по умолчанию
        sources = default_sources

    articles = []  # все собранные новости
    # Параллельно получаем RSS из всех источников
    tasks = [asyncio.to_thread(feedparser.parse, url) for url in sources]
    feeds = await asyncio.gather(*tasks, return_exceptions=False)
    for feed in feeds:
        if not feed or not hasattr(feed, "entries"):
            continue
        for entry in feed.entries:
            title = entry.title if hasattr(entry, "title") else "Без заголовка"
            link = entry.link if hasattr(entry, "link") else ""
            # Описание/краткий текст новости
            desc = ""
            if hasattr(entry, "summary"):
                desc = entry.summary
            elif hasattr(entry, "description"):
                desc = entry.description
            # Ограничиваем длину описания, чтобы не перегружать модель
            if desc and len(desc) > 1000:
                desc = desc[:1000] + "..."
            # Добавляем статью в общий список
            articles.append((title, link, desc, entry))
    if not articles:
        return []  # нет новостей

    # Сортируем новости по дате (от новых к старым)
    try:
        articles.sort(key=lambda x: datetime.fromtimestamp(mktime(x[3].published_parsed)) if hasattr(x[3], "published_parsed") else datetime.min, reverse=True)
    except Exception as e:
        # Логируем ошибку сортировки, если есть
        logging.warning(f"Не удалось отсортировать новости по дате: {e}")

    relevant_list = []
    # Подключаемся к базе для отметки отправленных новостей
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS sent_news (link TEXT PRIMARY KEY)")
    conn.commit()
    for title, link, desc, entry in articles:
        if not link:
            continue
        # Пропускаем, если уже отправляли эту новость ранее
        cur.execute("SELECT 1 FROM sent_news WHERE link=?", (link,))
        if cur.fetchone():
            continue
        # Проверяем релевантность новости
        is_rel = await is_relevant(title, desc, user_prompt)
        if is_rel:
            # Отмечаем в базе как отправленную
            try:
                cur.execute("INSERT OR IGNORE INTO sent_news (link) VALUES (?)", (link,))
                conn.commit()
            except Exception as db_err:
                logging.error(f"DB error on inserting sent_news: {db_err}")
            # Сохраняем для возврата
            relevant_list.append({"title": title, "link": link})
    conn.close()
    return relevant_list

async def main():
    # Инициализация бота и диспетчера
    bot = Bot(token=os.getenv("BOT_TOKEN"), parse_mode="HTML")
    dp = Dispatcher()

    # Инициализация базы данных: таблицы для пользователей, источников, отправленных новостей
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY)")
    cur.execute("CREATE TABLE IF NOT EXISTS sources (url TEXT PRIMARY KEY)")
    cur.execute("CREATE TABLE IF NOT EXISTS sent_news (link TEXT PRIMARY KEY)")
    conn.commit()
    # Заполняем источники по умолчанию (если их еще нет)
    for url in default_sources:
        cur.execute("INSERT OR IGNORE INTO sources (url) VALUES (?)", (url,))
    conn.commit()
    conn.close()

    # Команды и обработчики

    # Старт: регистрируем пользователя и показываем кнопку "Справка"
    @dp.message_handler(CommandStart())
    async def start_cmd(message: types.Message, state: FSMContext):
        # Добавляем пользователя в базу
        user_id = message.from_user.id
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute("INSERT OR IGNORE INTO users (id) VALUES (?)", (user_id,))
        conn.commit()
        conn.close()
        # Кнопка "Справка"
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Справка", callback_data="help")]
        ])
        await message.answer(
            "Привет! Я буду присылать тебе свежие и релевантные новости по теме A7A5 (криптовалюты, цифровой рубль и экономика).",
            reply_markup=keyboard
        )

    # Callback обработчик для кнопки "Справка"
    @dp.callback_query_handler(lambda c: c.data == "help")
    async def help_clicked(callback: CallbackQuery):
        HELP_TEXT = (
            "Вот что я умею:\n\n"
            "/digest — получить свежие релевантные новости\n"
            "/addsource <url> — добавить сайт/RSS источник\n"
            "/removesource <url> — удалить источник\n"
            "/listsources — показать все источники\n"
            "/help — показать эту справку\n"
            "/cancel — отменить ожидание запроса (если вы передумали вводить свой запрос)\n"
        )
        await callback.message.edit_text(HELP_TEXT)

    # Команда /help
    @dp.message_handler(Command("help"), state="*")
    async def help_cmd(message: types.Message):
        HELP_TEXT = (
            "Вот что я умею:\n\n"
            "/digest — получить свежие релевантные новости\n"
            "/addsource <url> — добавить сайт/RSS источник\n"
            "/removesource <url> — удалить источник\n"
            "/listsources — показать все источники\n"
            "/help — показать эту справку\n"
            "/cancel — отменить ввод пользовательского запроса\n"
        )
        await message.answer(HELP_TEXT)

    # Команда /addsource
    @dp.message_handler(Command("addsource"), state="*")
    async def add_source_cmd(message: types.Message):
        # Извлекаем аргумент URL
        args = message.text.split(maxsplit=1)
        if len(args) < 2:
            await message.reply("Пожалуйста, укажите URL после команды /addsource.")
            return
        url = args[1].strip()
        if not url:
            await message.reply("URL не распознан. Попробуйте еще раз.")
            return
        # Добавляем в БД
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute("INSERT OR IGNORE INTO sources (url) VALUES (?)", (url,))
        conn.commit()
        # Проверяем, добавлен ли новый источник
        if cur.rowcount == 0:
            # rowcount 0 значит запись уже существовала
            msg = "Этот источник уже был добавлен ранее."
        else:
            msg = "Новый источник добавлен."
        conn.close()
        await message.answer(msg)

    # Команда /removesource
    @dp.message_handler(Command("removesource"), state="*")
    async def remove_source_cmd(message: types.Message):
        args = message.text.split(maxsplit=1)
        if len(args) < 2:
            await message.reply("Пожалуйста, укажите URL после команды /removesource.")
            return
        url = args[1].strip()
        if not url:
            await message.reply("URL не распознан. Попробуйте еще раз.")
            return
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute("DELETE FROM sources WHERE url=?", (url,))
        conn.commit()
        if cur.rowcount == 0:
            msg = "Такого источника в списке не было."
        else:
            msg = "Источник удален."
        conn.close()
        await message.answer(msg)

    # Команда /listsources
    @dp.message_handler(Command("listsources"), state="*")
    async def list_sources_cmd(message: types.Message):
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute("SELECT url FROM sources")
        rows = cur.fetchall()
        conn.close()
        if not rows:
            await message.answer("Список источников пуст.")
        else:
            source_list = [row[0] for row in rows]
            text = "📋 <b>Источники RSS</b>:\n" + "\n".join(source_list)
            await message.answer(text)

    # Команда /cancel для отмены пользовательского промта
    @dp.message_handler(Command("cancel"), state="*")
    async def cancel_cmd(message: types.Message, state: FSMContext):
        # Если мы ждем пользовательский промт, сбрасываем состояние
        if await state.get_state() == DigestStates.waiting_for_prompt.state:
            await state.clear()
            await message.answer("Ожидание ввода отменено.")
        else:
            await message.answer("Нет активного ожидания ввода, нечего отменять.")

    # Команда /digest — сбор и отправка новостей
    @dp.message_handler(Command("digest"), state="*")
    async def digest_cmd(message: types.Message, state: FSMContext):
        # Если бот ожидал пользовательский промт от предыдущего вызова, сбросим это состояние
        await state.clear()
        await message.answer("🔄 Ищу свежие новости, пожалуйста подождите...")
        articles = await get_relevant_articles()  # используем дефолтную тематику
        if articles is None:
            # Ошибка при получении новостей (например, сбой OpenAI)
            await message.answer("Произошла ошибка при получении новостей. Попробуйте позже.")
            return
        if not articles:
            # Не найдено релевантных новостей — предлагаем ввести свой запрос
            await message.answer("По теме A7A5 пока нет релевантных новостей. Введите свой запрос, по которому вы хотите найти новости:")
            # Устанавливаем состояние ожидания пользовательского промта и сохраняем свежие новости для повторного анализа
            # (при необходимости можно сохранить новости, но здесь будем повторно собирать при вводе запроса)
            await state.set_state(DigestStates.waiting_for_prompt)
        else:
            # Отправляем найденные новости (заголовок + ссылка)
            for art in articles:
                title = art["title"]
                link = art["link"]
                # Отправляем каждую новость отдельным сообщением
                await message.answer(f"<b>{title}</b>\n{link}")

    # Обработчик ввода пользовательского запроса (когда бот в состоянии waiting_for_prompt)
    @dp.message_handler(state=DigestStates.waiting_for_prompt)
    async def custom_prompt_entered(message: types.Message, state: FSMContext):
        user_prompt = message.text.strip()
        if not user_prompt:
            await message.answer("Запрос не должен быть пустым. Попробуйте снова или введите /cancel для отмены.")
            return
        await message.answer(f"🔎 Ищу новости по запросу: \"{user_prompt}\"...")
        # Собираем и фильтруем новости по пользовательскому запросу (используем ту же функцию, но с кастомным промтом)
        articles = await get_relevant_articles(user_prompt)
        if articles is None:
            # Ошибка при анализе
            await message.answer("Произошла ошибка при обработке вашего запроса. Попробуйте позже.")
            await state.clear()
            return
        if not articles:
            await message.answer("К сожалению, по вашему запросу не нашлось релевантных новостей.")
        else:
            for art in articles:
                title = art["title"]
                link = art["link"]
                await message.answer(f"<b>{title}</b>\n{link}")
        # Очищаем состояние (возвращаемся к обычному режиму)
        await state.clear()

    # Планирование ежедневной рассылки (ежедневный дайджест)
    async def daily_digest():
        logging.info("Выполняется ежедневная рассылка новостей...")
        articles = await get_relevant_articles()  # получаем релевантные новости по дефолтной тематике
        if articles is None:
            # Если ошибка при получении (например, проблема с OpenAI), пропускаем рассылку сегодня
            logging.error("Не удалось подготовить ежедневный дайджест из-за ошибки.")
            return
        if not articles:
            # Нет новых релевантных новостей для рассылки
            logging.info("Нет новых новостей для ежедневной рассылки.")
            return
        # Получаем всех пользователей для рассылки
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute("SELECT id FROM users")
        users = [row[0] for row in cur.fetchall()]
        conn.close()
        # Отправляем каждому пользователю все найденные новости
        for user_id in users:
            for art in articles:
                title = art["title"]
                link = art["link"]
                try:
                    await bot.send_message(user_id, f"<b>{title}</b>\n{link}")
                except Exception as send_err:
                    logging.warning(f"Не удалось отправить сообщение пользователю {user_id}: {send_err}")

    # Запускаем ежедневный дайджест по расписанию (например, каждый день в 9:00 утра)
    async def schedule_daily_digest():
        # Расчет времени до ближайшего запуска (сегодня в 9:00 или завтра, если уже прошло)
        now = datetime.now()
        target_time = datetime.combine(date.today(), time(hour=9, minute=0))
        if now >= target_time:
            # Если текущее время уже позже 9:00, планируем на завтра
            target_time += timedelta(days=1)
        delay = (target_time - now).total_seconds()
        # Ожидаем до времени запуска, затем каждые 24 часа
        await asyncio.sleep(delay)
        while True:
            await daily_digest()
            await asyncio.sleep(24 * 3600)  # 24 часа

    # Запускаем задачу рассылки (не блокирует основного polling)
    asyncio.create_task(schedule_daily_digest())

    # Отключаем webhook и запускаем polling
    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("Бот запущен. Ожидание сообщений...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
