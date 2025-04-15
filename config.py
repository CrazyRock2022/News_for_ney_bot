
import os
from dotenv import load_dotenv
from aiogram.utils.i18n import I18n

class Config:
    def __init__(self):
        load_dotenv()
        self.API_TOKEN = os.getenv("API_TOKEN")
        self.OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
        self.OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
        
        # Настройки кэша
        self.CACHE_TTL = 300  # 5 минут
        
        # Локализация
        self.i18n = I18n(path="locales", default_locale="ru", domain="messages")
