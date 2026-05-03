"""
Конфигурация бота — читает переменные из .env
"""

import os
from dotenv import load_dotenv

load_dotenv()
# Runtime overrides (API keys set via /settings) are saved here and survive Docker rebuilds
# because data/ is a Docker volume. Values here take precedence over the baked-in .env.
load_dotenv("data/runtime.env", override=True)


class Config:
    VERSION: str = "1.0.0"

    # Telegram
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
    ALLOWED_IDS: list[int] = [
        int(x.strip())
        for x in os.getenv("ALLOWED_IDS", "").split(",")
        if x.strip().isdigit()
    ]
    # OWNER_ID — единственный пользователь, кому разрешено /settings и /reauth.
    # По умолчанию — первый user из ALLOWED_IDS, можно переопределить через env.
    OWNER_ID: int = (
        int(os.getenv("OWNER_ID")) if os.getenv("OWNER_ID", "").strip().isdigit()
        else (int(os.getenv("ALLOWED_IDS", "0").split(",")[0])
              if os.getenv("ALLOWED_IDS", "").split(",")[0].strip().isdigit()
              else 0)
    )

    # LLM провайдер: "groq" или "google" (валидируется в main.py при старте)
    LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "groq").lower()

    # Groq
    GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "")
    GROQ_MODEL: str = os.getenv("GROQ_MODEL", "meta-llama/llama-4-maverick-17b-128e-instruct")

    # Google AI Studio
    GOOGLE_AI_KEY: str   = os.getenv("GOOGLE_AI_KEY", "")
    GOOGLE_AI_MODEL: str = os.getenv("GOOGLE_AI_MODEL", "gemini-2.0-flash")

    # Google — содержимое credentials.json и token.json передаётся строкой JSON
    # GOOGLE_CREDENTIALS_JSON — обязательна (вставить содержимое credentials.json)
    # GOOGLE_TOKEN_JSON — опционально; если не задана, токен читается/пишется из файла
    GOOGLE_CREDENTIALS_JSON: str = os.getenv("GOOGLE_CREDENTIALS_JSON", "")
    GOOGLE_TOKEN_JSON: str = os.getenv("GOOGLE_TOKEN_JSON", "")
    # Файловый fallback для токена (используется если GOOGLE_TOKEN_JSON не задан)
    GOOGLE_TOKEN_PATH: str = os.getenv("GOOGLE_TOKEN_PATH", "data/token.json")

    # Расписание
    BRIEFING_TIME: str = os.getenv("BRIEFING_TIME", "08:00")  # HH:MM локального времени (TIMEZONE)
    REMINDER_INTERVAL_HOURS: int = int(os.getenv("REMINDER_INTERVAL_HOURS", "1"))
    # Рабочие часы (для агента и подсказок)
    WORK_HOUR_START: int = int(os.getenv("WORK_HOUR_START", "9"))
    WORK_HOUR_END:   int = int(os.getenv("WORK_HOUR_END",   "20"))
    # Часы сна — никогда не ставить встречи + тихий режим напоминаний
    SLEEP_HOUR_START: int = int(os.getenv("SLEEP_HOUR_START", "22"))
    SLEEP_HOUR_END:   int = int(os.getenv("SLEEP_HOUR_END",   "7"))

    # База данных
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    DB_PATH: str = os.getenv("DB_PATH", "data/bot.db")

    # Максимум сообщений в истории диалога
    MAX_HISTORY: int = 20

    # Временная зона пользователя (IANA, например "Asia/Almaty", "Europe/Moscow")
    TIMEZONE: str = os.getenv("TIMEZONE", "UTC")

    # ─── Визуализация (Status/Tasks/Today + хитмап) ─────────────────────────
    # Адаптивный диапазон Y для хитмапа: фактические события → clamped в [MIN, MAX]
    HEATMAP_HOUR_MIN: int = int(os.getenv("HEATMAP_HOUR_MIN", "6"))
    HEATMAP_HOUR_MAX: int = int(os.getenv("HEATMAP_HOUR_MAX", "24"))
    # Минимальный размер свободного окна
    MIN_FREE_WINDOW_HOURS:       float = float(os.getenv("MIN_FREE_WINDOW_HOURS", "2.0"))
    MIN_FREE_WINDOW_TODAY_HOURS: float = float(os.getenv("MIN_FREE_WINDOW_TODAY_HOURS", "1.0"))
    # Пороги срочности задач (дней до дедлайна)
    URGENT_TASK_DAYS: int = int(os.getenv("URGENT_TASK_DAYS", "1"))
    WARM_TASK_DAYS:   int = int(os.getenv("WARM_TASK_DAYS",   "7"))
    # Регекспы рутинных событий, которые схлопываются (через запятую)
    ROUTINE_PATTERNS: str = os.getenv("ROUTINE_PATTERNS", "Дорога")
    # Знаменатель для процентовки нагрузки на хитмапе
    WORK_HOURS_PER_WEEK: int = int(os.getenv("WORK_HOURS_PER_WEEK", "70"))


config = Config()
