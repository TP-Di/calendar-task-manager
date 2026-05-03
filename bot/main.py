"""
Точка входа бота. Инициализация, регистрация хендлеров, запуск APScheduler.
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import config
from app.db.database import backup_db, init_db
from app.db.log_handler import SqliteLogHandler
from app.handlers import commands, documents, messages, settings as settings_handler
from app.middleware.whitelist import WhitelistMiddleware
from app.services.briefing import send_briefing, send_weekly_retro
from app.services.reminders import check_and_send_reminders, sync_completed_tasks
from app.services.scheduler_ref import set_scheduler

# Настройка логирования
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
# Дополнительно пишем логи в SQLite (доступ через SSH: sqlite3 data/bot.db)
_db_log_handler = SqliteLogHandler(config.DB_PATH)
_db_log_handler.setLevel(getattr(logging, config.LOG_LEVEL.upper(), logging.INFO))
logging.getLogger().addHandler(_db_log_handler)
logger = logging.getLogger(__name__)


async def setup_bot_commands(bot: Bot) -> None:
    """Регистрирует команды в меню Telegram (порядок = порядок в меню)."""
    commands_list = [
        BotCommand(command="status",   description="📊 Сейчас + сегодня/завтра + горящие задачи"),
        BotCommand(command="heatmap",  description="📅 График недели с категориями"),
        BotCommand(command="load",     description="📂 Текстовая сводка нагрузки за неделю"),
        BotCommand(command="done",     description="✅ Отметить задачу выполненной"),
        BotCommand(command="postpone", description="⏰ Отложить задачу"),
        BotCommand(command="upload",   description="📎 Загрузить PDF с расписанием"),
        BotCommand(command="settings", description="⚙️ Настройки (LLM, ключи, визуализация)"),
        BotCommand(command="reauth",   description="🔑 Переавторизация Google Calendar"),
        BotCommand(command="clear",    description="🗑 Сбросить историю диалога"),
        BotCommand(command="help",     description="📖 Справка"),
    ]
    await bot.set_my_commands(commands_list)


def setup_scheduler(scheduler: AsyncIOScheduler, bot: Bot) -> None:
    """Настраивает cron-задачи в APScheduler."""

    # Парсим время брифинга (HH:MM)
    try:
        briefing_hour, briefing_minute = map(int, config.BRIEFING_TIME.split(":"))
    except Exception:
        briefing_hour, briefing_minute = 8, 0
        logger.warning("Неверный формат BRIEFING_TIME, используется 08:00")

    # Утренний брифинг по расписанию (время в локальной зоне пользователя)
    scheduler.add_job(
        send_briefing,
        trigger="cron",
        hour=briefing_hour,
        minute=briefing_minute,
        timezone=config.TIMEZONE,
        args=[bot],
        id="morning_briefing",
        replace_existing=True,
    )
    logger.info(
        "Брифинг запланирован на %02d:%02d %s", briefing_hour, briefing_minute, config.TIMEZONE
    )

    # Напоминания о дедлайнах (каждые N часов)
    # next_run_time — откладываем первый запуск, чтобы рестарт контейнера
    # не вызывал мгновенную отправку уведомлений
    scheduler.add_job(
        check_and_send_reminders,
        trigger="interval",
        hours=config.REMINDER_INTERVAL_HOURS,
        next_run_time=datetime.now(timezone.utc) + timedelta(hours=config.REMINDER_INTERVAL_HOURS),
        args=[bot],
        id="deadline_reminders",
        replace_existing=True,
    )
    logger.info(
        "Напоминания настроены с интервалом %d ч", config.REMINDER_INTERVAL_HOURS
    )

    # Синхронизация выполненных задач с календарём (каждые 15 минут)
    scheduler.add_job(
        sync_completed_tasks,
        trigger="interval",
        minutes=15,
        next_run_time=datetime.now(timezone.utc) + timedelta(minutes=15),
        args=[bot],
        id="task_calendar_sync",
        replace_existing=True,
    )
    logger.info("Синхронизация задач↔календарь каждые 15 мин")

    # Воскресный ретро (каждое воскресенье в 20:00 по TIMEZONE)
    scheduler.add_job(
        send_weekly_retro,
        trigger="cron",
        day_of_week="sun",
        hour=20,
        minute=0,
        args=[bot],
        id="weekly_retro",
        replace_existing=True,
    )
    logger.info("Воскресный ретро запланирован на вс 20:00 %s", config.TIMEZONE)

    # Ежедневный бэкап БД
    scheduler.add_job(
        backup_db,
        trigger="cron",
        hour=3,
        minute=0,
        id="daily_backup",
        replace_existing=True,
    )
    logger.info("Бэкап БД запланирован на 03:00 UTC ежедневно")


async def _start_health_server(port: int) -> web.AppRunner:
    """Запускает aiohttp health check сервер для DigitalOcean."""
    async def handle(_request: web.Request) -> web.Response:
        # Не раскрываем версию: probe нужен только для health check
        return web.Response(text="OK")

    app = web.Application()
    app.router.add_get("/", handle)
    app.router.add_get("/health", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    return runner


async def main() -> None:
    """Основная функция запуска бота."""
    logger.info("Запуск бота...")

    # Проверяем обязательные переменные
    if not config.BOT_TOKEN:
        raise ValueError("BOT_TOKEN не задан в .env")
    if not config.GROQ_API_KEY:
        raise ValueError("GROQ_API_KEY не задан в .env")
    if not config.ALLOWED_IDS:
        logger.warning("ALLOWED_IDS пуст — доступ заблокирован для всех!")

    # Валидация TIMEZONE — иначе APScheduler упадёт с невнятной ошибкой
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(config.TIMEZONE)
    except Exception as e:
        raise ValueError(
            f"Некорректный TIMEZONE='{config.TIMEZONE}'. "
            "Используйте IANA-имя, например 'Europe/Moscow', 'Asia/Almaty', 'UTC'."
        ) from e

    # Валидация BRIEFING_TIME формата HH:MM
    try:
        bh, bm = map(int, config.BRIEFING_TIME.split(":"))
        if not (0 <= bh < 24 and 0 <= bm < 60):
            raise ValueError
    except Exception:
        logger.warning(
            "Некорректный BRIEFING_TIME='%s' — будет использован 08:00",
            config.BRIEFING_TIME,
        )

    # Валидация LLM_PROVIDER
    if config.LLM_PROVIDER not in ("groq", "google"):
        logger.warning(
            "Неизвестный LLM_PROVIDER='%s' — fallback на 'groq'",
            config.LLM_PROVIDER,
        )
        config.LLM_PROVIDER = "groq"

    # Создаём директорию для данных если не существует
    os.makedirs(os.path.dirname(config.DB_PATH), exist_ok=True)

    # Инициализируем базу данных
    await init_db()

    # Создаём бота и диспетчер
    bot = Bot(token=config.BOT_TOKEN)
    dp = Dispatcher()

    # Middleware
    dp.update.middleware(WhitelistMiddleware())

    # Регистрируем роутеры (порядок важен)
    dp.include_router(commands.router)
    dp.include_router(documents.router)
    dp.include_router(settings_handler.router)  # До messages — перехватывает текст при ожидании ввода
    dp.include_router(messages.router)  # Должен быть последним (ловит все сообщения)

    # Настраиваем команды бота
    await setup_bot_commands(bot)

    # Запускаем планировщик с защитой от дрейфа: coalesce склеивает пропущенные
    # запуски, max_instances=1 предотвращает параллельное выполнение одного и
    # того же job (например long-running брифинг), misfire_grace_time даёт
    # 60-секундный slack на случай краткого блока event loop.
    scheduler = AsyncIOScheduler(
        timezone=config.TIMEZONE,
        job_defaults={
            "coalesce": True,
            "max_instances": 1,
            "misfire_grace_time": 60,
        },
    )
    setup_scheduler(scheduler, bot)
    scheduler.start()
    set_scheduler(scheduler)  # делает scheduler доступным для settings.py
    logger.info("APScheduler запущен")

    # Health check сервер для DigitalOcean (порт 8080)
    health_port = int(os.environ.get("PORT", 8080))
    health_runner = await _start_health_server(health_port)
    logger.info("Health check server запущен на порту %d", health_port)

    # Запускаем polling
    logger.info(
        "Бот запущен. Разрешённые пользователи: %s",
        config.ALLOWED_IDS,
    )
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await health_runner.cleanup()
        scheduler.shutdown()
        await bot.session.close()
        logger.info("Бот остановлен")


if __name__ == "__main__":
    asyncio.run(main())
