import yaml
from pathlib import Path

KEYWORDS_FILE = Path("keywords.yaml")

def load_keywords():
    return yaml.safe_load(KEYWORDS_FILE.read_text()) if KEYWORDS_FILE.exists() else []

def save_keywords(keywords: list):
    KEYWORDS_FILE.write_text(yaml.dump(keywords))

@router.message(Command("keywords"))
async def show_keywords(message: Message):
    keywords = load_keywords()
    await message.answer(f"🔍 Текущие ключи:\n{', '.join(keywords)}")
