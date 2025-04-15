from aiogram.utils.keyboard import InlineKeyboardBuilder

def main_menu_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="🔍 Фильтры", callback_data="filter_menu")
    builder.button(text="📊 Статистика", callback_data="show_stats")
    builder.button(text="⚙️ Настройки", callback_data="settings_menu")
    builder.adjust(2, 1)
    return builder.as_markup()

def filters_menu_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="🔑 Ключевые слова", callback_data="set_filter_keywords")
    builder.button(text="🤖 OpenAI", callback_data="set_filter_openai")
    builder.button(text="🚀 OpenRouter", callback_data="set_filter_openrouter")
    builder.button(text="◀️ Назад", callback_data="main_menu")
    builder.adjust(2, 1)
    return builder.as_markup()

def confirmation_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Да", callback_data="confirm_yes")
    builder.button(text="❌ Нет", callback_data="confirm_no")
    return builder.as_markup()
    
from aiogram.utils.keyboard import InlineKeyboardBuilder

def stats_pagination_keyboard(current_page, total_pages):
    builder = InlineKeyboardBuilder()
    
    if current_page > 1:
        builder.button(text="◀️", callback_data=f"stats_page_{current_page-1}")
    
    builder.button(text=f"{current_page}/{total_pages}", callback_data="current_page")
    
    if current_page < total_pages:
        builder.button(text="▶️", callback_data=f"stats_page_{current_page+1}")
    
    builder.button(text="🔙 Назад", callback_data="main_menu")
    builder.adjust(3, 1)
    return builder.as_markup()
