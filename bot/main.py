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
from app.handlers import commands, documents, messages
from app.middleware.whitelist import WhitelistMiddleware
from app.services.briefing import send_briefing, send_weekly_retro
from app.services.reminders import check_and_send_reminders

# Настройка логирования
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


async def setup_bot_commands(bot: Bot) -> None:
    """Регистрирует команды в меню Telegram."""
    commands_list = [
        BotCommand(command="start", description="Начало работы"),
        BotCommand(command="help", description="Список команд"),
        BotCommand(command="status", description="Задачи и ближайшие события"),
        BotCommand(command="load", description="Нагрузка на неделю"),
        BotCommand(command="done", description="Отметить задачу выполненной"),
        BotCommand(command="postpone", description="Отложить задачу"),
        BotCommand(command="upload", description="Загрузить PDF с расписанием"),
        BotCommand(command="clear", description="Очистить историю диалога"),
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

    # Воскресный ретро (каждое воскресенье в 20:00 UTC)
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
    logger.info("Воскресный ретро запланирован на вс 20:00 UTC")

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
    dp.include_router(messages.router)  # Должен быть последним (ловит все сообщения)

    # Настраиваем команды бота
    await setup_bot_commands(bot)

    # Запускаем планировщик
    scheduler = AsyncIOScheduler(timezone="UTC")
    setup_scheduler(scheduler, bot)
    scheduler.start()
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
