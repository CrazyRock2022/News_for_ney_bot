# -*- coding: utf-8 -*-
import os
import logging
import asyncio
import feedparser
import sqlite3
import json
import yaml
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Optional

from aiogram import Bot, Dispatcher, Router, F
from aiogram.enums import ParseMode
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.filters import Command, CommandStart
from aiogram.client.default import DefaultBotProperties

# ==================== ИНИЦИАЛИЗАЦИЯ ====================
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# Конфигурация
API_TOKEN = os.getenv("API_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

bot = Bot(
    token=API_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
storage = MemoryStorage()
dp = Dispatcher()
router = Router()
dp.include_router(router)

# ==================== МОДУЛИ ====================
class StatsManager:
    def __init__(self):
        self.conn = sqlite3.connect('stats.db')
        self._create_table()

    def _create_table(self):
        self.conn.execute('''CREATE TABLE IF NOT EXISTS stats
             (source TEXT PRIMARY KEY, total INTEGER, passed INTEGER)''')

    def update(self, source: str, passed: bool):
        self.conn.execute('''INSERT OR IGNORE INTO stats VALUES (?, 0, 0)''', (source,))
        self.conn.execute('''UPDATE stats SET total = total + 1, passed = passed + ? 
                          WHERE source = ?''', (int(passed), source))
        self.conn.commit()

    def get_stats(self):
        cur = self.conn.execute('SELECT * FROM stats')
        return {row[0]: {'total': row[1], 'passed': row[2]} for row in cur}

class SourceManager:
    def __init__(self):
        self.sources_file = Path("sources.json")
        self._init_file()

    def _init_file(self):
        if not self.sources_file.exists():
            self.sources_file.write_text('["https://forklog.com/feed/"]')

    def get_sources(self):
        return json.loads(self.sources_file.read_text())

    def add_source(self, url: str):
        sources = self.get_sources()
        sources.append(url)
        self.sources_file.write_text(json.dumps(sources))

class KeywordManager:
    def __init__(self):
        self.keywords_file = Path("keywords.yaml")
        self._init_file()

    def _init_file(self):
        if not self.keywords_file.exists():
            self.keywords_file.write_text(yaml.dump(["крипта", "биткоин"]))

    def get_keywords(self):
        return yaml.safe_load(self.keywords_file.read_text())

    def add_keyword(self, keyword: str):
        keywords = self.get_keywords()
        keywords.append(keyword)
        self.keywords_file.write_text(yaml.dump(keywords))

# Инициализация менеджеров
stats = StatsManager()
sources = SourceManager()
keywords = KeywordManager()

# ==================== КОМАНДЫ ====================
@router.message(CommandStart())
async def start(message: Message):
    builder = InlineKeyboardBuilder()
    builder.button(text="🔍 Выбрать фильтр", callback_data="filter_menu")
    builder.button(text="📊 Статистика", callback_data="show_stats")
    builder.button(text="⚙️ Настройки", callback_data="settings_menu")
    await message.answer("🎛️ Главное меню:", reply_markup=builder.as_markup())

@router.callback_query(F.data == "filter_menu")
async def filter_menu(callback: CallbackQuery):
    builder = InlineKeyboardBuilder()
    builder.button(text="🔑 Ключевые слова", callback_data="set_filter_keywords")
    builder.button(text="🤖 OpenAI", callback_data="set_filter_openai")
    builder.button(text="🚀 OpenRouter", callback_data="set_filter_openrouter")
    builder.button(text="◀️ Назад", callback_data="main_menu")
    builder.adjust(2)
    await callback.message.edit_text("🎚️ Выберите фильтр:", reply_markup=builder.as_markup())

@router.callback_query(F.data == "show_stats")
async def show_stats(callback: CallbackQuery):
    stats_data = stats.get_stats()
    text = "📊 Статистика по источникам:\n\n"
    for source, data in stats_data.items():
        text += f"{source}:\n🔹 Всего: {data['total']}\n🔹 Подходящих: {data['passed']}\n\n"
    await callback.message.edit_text(text)

# ==================== ЗАПУСК ====================
async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
