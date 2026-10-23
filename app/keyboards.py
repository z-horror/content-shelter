from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

ROWS = [
    [("+15 мин", "sched:m:15"), ("+30 мин", "sched:m:30"), ("+1 час", "sched:m:60")],
    [("+2 часа", "sched:m:120"), ("+2,5 часа", "sched:m:150"), ("+3 часа", "sched:m:180")],
    [("+4 часа", "sched:m:240"), ("Сегодня 19:00", "sched:today19"), ("Завтра 10:00", "sched:tomorrow10")],
    [("⌨️ Другое время", "sched:manual")],
]


def schedule_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, callback_data=data) for text, data in row]
            for row in ROWS
        ]
    )
