"""
Конфигурация бота — читает переменные из .env
"""

import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # Telegram
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
    ALLOWED_IDS: list[int] = [
        int(x.strip())
        for x in os.getenv("ALLOWED_IDS", "").split(",")
        if x.strip().isdigit()
    ]

    # Groq
    GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "")
    GROQ_MODEL: str = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

    # Google — содержимое credentials.json и token.json передаётся строкой JSON
    # GOOGLE_CREDENTIALS_JSON — обязательна (вставить содержимое credentials.json)
    # GOOGLE_TOKEN_JSON — опционально; если не задана, токен читается/пишется из файла
    GOOGLE_CREDENTIALS_JSON: str = os.getenv("GOOGLE_CREDENTIALS_JSON", "")
    GOOGLE_TOKEN_JSON: str = os.getenv("GOOGLE_TOKEN_JSON", "")
    # Файловый fallback для токена (используется если GOOGLE_TOKEN_JSON не задан)
    GOOGLE_TOKEN_PATH: str = os.getenv("GOOGLE_TOKEN_PATH", "data/token.json")

    # Расписание
    BRIEFING_TIME: str = os.getenv("BRIEFING_TIME", "08:00")  # HH:MM
    REMINDER_INTERVAL_HOURS: int = int(os.getenv("REMINDER_INTERVAL_HOURS", "1"))
    QUIET_HOUR_START: int = int(os.getenv("QUIET_HOUR_START", "23"))
    QUIET_HOUR_END: int = int(os.getenv("QUIET_HOUR_END", "6"))

    # База данных
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    DB_PATH: str = os.getenv("DB_PATH", "data/bot.db")

    # Максимум сообщений в истории диалога
    MAX_HISTORY: int = 20

    # Временная зона пользователя (IANA, например "Asia/Almaty", "Europe/Moscow")
    TIMEZONE: str = os.getenv("TIMEZONE", "UTC")


config = Config()
