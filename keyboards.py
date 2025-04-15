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
