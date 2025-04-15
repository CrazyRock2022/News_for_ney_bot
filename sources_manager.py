import json
from aiogram import F
from aiogram.filters import Command

@router.message(Command("add_source"))
async def add_source(message: Message):
    await message.answer("Введите URL нового RSS-источника:")

@router.message(F.text.startswith("http"))
async def process_source(message: Message):
    with open("sources.json", "r+") as f:
        sources = json.load(f)
        sources.append(message.text)
        f.seek(0)
        json.dump(sources, f)
    await message.answer("✅ Источник добавлен!")
