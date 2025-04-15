# bot.py (основной файл)
# -*- coding: utf-8 -*-
import os
import logging
import asyncio
from typing import Dict, List, Optional
from pathlib import Path

from aiogram import Bot, Dispatcher, Router, F
from aiogram.enums import ParseMode
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command, CommandStart
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.utils.i18n import I18n, SimpleI18nMiddleware

from config import Config
from managers import (
    StatsManager,
    SourceManager,
    KeywordManager,
    CacheManager,
    RateLimiter,
    FeedValidator
)
from keyboards import (
    main_menu_keyboard,
    filters_menu_keyboard,
    stats_pagination_keyboard,
    confirmation_keyboard
)

# Инициализация конфигурации
config = Config()

# Настройка локализации
i18n = I18n(path=Path("locales"), default_locale="ru", domain="messages")
i18n_middleware = SimpleI18nMiddleware(i18n)

# Инициализация бота и диспетчера
bot = Bot(token=config.API_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)
i18n_middleware.setup(router)

# Инициализация менеджеров
stats = StatsManager()
sources = SourceManager()
keywords = KeywordManager()
cache = CacheManager()
limiter = RateLimiter(max_requests=10, period=60)  # 10 запросов в минуту
validator = FeedValidator()

# ==================== ОБРАБОТЧИКИ С ОШИБКАМИ ====================
async def error_handler(func, *args, **kwargs):
    try:
        return await func(*args, **kwargs)
    except Exception as e:
        logger.error(f"Error in {func.__name__}: {str(e)}")
        await args[0].message.answer(config.i18n.get("error_occurred"))

# ==================== КОМАНДЫ И ХЕНДЛЕРЫ ====================
@router.message(CommandStart())
@error_handler
async def start(message: Message):
    await message.answer(config.i18n.get("main_menu"), reply_markup=main_menu_keyboard())

@router.callback_query(F.data == "filter_menu")
@error_handler
@limiter.check_limit
async def filter_menu(callback: CallbackQuery):
    await callback.message.edit_text(config.i18n.get("filter_menu"), 
                                  reply_markup=filters_menu_keyboard())

@router.callback_query(F.data.startswith("stats_page_"))
@error_handler
async def show_stats(callback: CallbackQuery):
    page = int(callback.data.split("_")[2])
    stats_data, total_pages = stats.get_paginated(page)
    
    text = config.i18n.get("stats_header")
    for source, data in stats_data.items():
        text += config.i18n.get("stats_item").format(
            source=source,
            total=data['total'],
            passed=data['passed']
        )
    
    await callback.message.edit_text(
        text,
        reply_markup=stats_pagination_keyboard(page, total_pages)
    )

# ==================== УПРАВЛЕНИЕ ИСТОЧНИКАМИ ====================
class SourceStates(StatesGroup):
    awaiting_url = State()
    confirming = State()

@router.callback_query(F.data == "add_source"))
@error_handler
async def add_source_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer(config.i18n.get("enter_source_url"))
    await state.set_state(SourceStates.awaiting_url)

@router.message(SourceStates.awaiting_url))
@error_handler
async def process_source_url(message: Message, state: FSMContext):
    if validator.is_valid_rss(message.text):
        await state.update_data(url=message.text)
        await message.answer(
            config.i18n.get("confirm_source").format(url=message.text),
            reply_markup=confirmation_keyboard()
        )
        await state.set_state(SourceStates.confirming)
    else:
        await message.answer(config.i18n.get("invalid_source"))

@router.callback_query(SourceStates.confirming, F.data.in_(["confirm_yes", "confirm_no"]))
@error_handler
async def confirm_source(callback: CallbackQuery, state: FSMContext):
    if callback.data == "confirm_yes":
        data = await state.get_data()
        sources.add_source(data['url'])
        await callback.message.answer(config.i18n.get("source_added"))
    else:
        await callback.message.answer(config.i18n.get("source_cancelled"))
    
    await state.clear()

# ==================== ЗАПУСК ====================
async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
