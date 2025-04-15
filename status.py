from datetime import datetime

class AnalyzerStatus:
    def __init__(self):
        self.last_check = datetime.now()
        self.processed = 0
    
    def get_status(self):
        return f"""📈 Статус анализа:
• Последняя проверка: {self.last_check.strftime('%d.%m %H:%M')}
• Обработано новостей: {self.processed}
• Активных фильтров: 3"""

@router.message(Command("status"))
async def status(message: Message):
    await message.answer(status_manager.get_status())
