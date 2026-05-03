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

from app.config import config, log_config_sources
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
    """Регистрирует команды в меню Telegram (только то, что нельзя через кнопки/агента)."""
    commands_list = [
        BotCommand(command="upload",   description="📎 Загрузить PDF"),
        BotCommand(command="settings", description="⚙️ Настройки"),
        BotCommand(command="reauth",   description="🔑 Переавторизация Google"),
        BotCommand(command="clear",    description="🗑 Сбросить историю"),
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


async def _start_health_server(port: int, bot: Bot) -> web.AppRunner:
    """Запускает aiohttp health check + OAuth callback сервер."""
    async def handle(_request: web.Request) -> web.Response:
        return web.Response(text="OK")

    async def oauth_callback(request: web.Request) -> web.Response:
        """
        Принимает редирект от Google после consent.
        ?code=... + ?state=user_id:csrf → находим юзера, завершаем flow,
        опционально шлём подтверждение в Telegram.
        """
        from app.services import calendar as cal

        code = request.query.get("code", "")
        state = request.query.get("state", "")
        err = request.query.get("error", "")

        if err:
            return web.Response(
                text=f"<h2>❌ Ошибка авторизации</h2><p>{err}</p>",
                content_type="text/html",
                status=400,
            )
        if not code or not state:
            return web.Response(
                text="<h2>❌ Отсутствует code или state</h2>",
                content_type="text/html",
                status=400,
            )

        user_id = cal.find_user_by_state(state)
        if user_id is None:
            return web.Response(
                text=(
                    "<h2>❌ Сессия не найдена</h2>"
                    "<p>State не совпадает с активной auth-сессией. "
                    "Запусти /reauth заново в боте.</p>"
                ),
                content_type="text/html",
                status=400,
            )

        try:
            await asyncio.to_thread(cal.complete_auth, code, user_id)
        except Exception as e:
            logger.error("OAuth callback: complete_auth failed: %s", e, exc_info=True)
            return web.Response(
                text=(
                    "<h2>❌ Не удалось завершить авторизацию</h2>"
                    f"<pre>{e}</pre>"
                    "<p>Попробуй /reauth в боте заново.</p>"
                ),
                content_type="text/html",
                status=500,
            )

        # Подтверждение в Telegram
        try:
            await bot.send_message(
                user_id,
                "✅ Авторизация Google Calendar успешна! Можешь продолжать.",
            )
        except Exception as e:
            logger.warning("Не удалось отправить confirm в Telegram: %s", e)

        return web.Response(
            text=(
                "<!doctype html><html><head><meta charset='utf-8'>"
                "<title>Готово</title></head><body style='font-family:sans-serif;padding:40px;text-align:center'>"
                "<h1>✅ Авторизация выполнена</h1>"
                "<p>Окно можно закрыть и вернуться в Telegram.</p>"
                "</body></html>"
            ),
            content_type="text/html",
        )

    app = web.Application()
    app.router.add_get("/", handle)
    app.router.add_get("/health", handle)
    app.router.add_get("/oauth/callback", oauth_callback)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    return runner


async def main() -> None:
    """Основная функция запуска бота."""
    logger.info("Запуск бота...")
    log_config_sources()

    # Диагностика persistence для credentials
    from app.services.calendar import env_persistence_status
    persist = env_persistence_status()
    for path, ok in persist.items():
        logger.info(
            "Persist target %-25s %s",
            path,
            "writable ✓" if ok else "NOT writable ✗ — изменения через /settings не сохранятся!",
        )

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

    # Health check + OAuth callback сервер (порт 8080 на DO)
    health_port = int(os.environ.get("PORT", 8080))
    health_runner = await _start_health_server(health_port, bot)
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
