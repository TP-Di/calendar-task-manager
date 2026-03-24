"""
Сервис напоминаний — периодические проверки дедлайнов задач.
Тихие часы, эскалация, snooze через inline кнопки.
"""

import logging
import zoneinfo
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import config
import app.services.calendar as cal_svc
import app.services.tasks as tasks_svc

logger = logging.getLogger(__name__)


def _is_quiet_hours() -> bool:
    """Проверяет, находимся ли мы в тихих часах (в локальной временной зоне)."""
    try:
        tz = zoneinfo.ZoneInfo(config.TIMEZONE)
    except Exception:
        tz = timezone.utc
    now = datetime.now(tz)
    hour = now.hour
    start = config.QUIET_HOUR_START
    end = config.QUIET_HOUR_END

    if start > end:
        # Ночной период переходит через полночь (например, 23:00–06:00)
        return hour >= start or hour < end
    else:
        return start <= hour < end


def _make_snooze_keyboard(task_id: str) -> InlineKeyboardMarkup:
    """Создаёт inline клавиатуру для snooze напоминания."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⏱ 30 мин",
                    callback_data=f"snooze:{task_id}:30",
                ),
                InlineKeyboardButton(
                    text="⏰ 1 час",
                    callback_data=f"snooze:{task_id}:60",
                ),
                InlineKeyboardButton(
                    text="📅 До завтра",
                    callback_data=f"snooze:{task_id}:tomorrow",
                ),
            ]
        ]
    )


async def check_and_send_reminders(bot: Bot) -> None:
    """
    Проверяет задачи и отправляет напоминания.
    Логика эскалации:
    - за 24 часа до дедлайна
    - за 3 часа до дедлайна
    - за 1 час до дедлайна
    - просроченные — каждый интервал проверки
    """
    if _is_quiet_hours():
        logger.debug("Тихие часы, напоминания пропущены")
        return

    now = datetime.now(zoneinfo.ZoneInfo(config.TIMEZONE))

    try:
        active_tasks = await tasks_svc.get_tasks()
    except Exception as e:
        logger.error("Ошибка получения задач для напоминаний: %s", e)
        return

    for task in active_tasks:
        due_str = task.get("due", "")
        if not due_str:
            continue

        try:
            due_dt = datetime.fromisoformat(due_str.replace("Z", "+00:00"))
        except Exception:
            continue

        delta = due_dt - now
        total_seconds = delta.total_seconds()

        # Определяем уровень эскалации
        if total_seconds < 0:
            # Просрочено
            urgency = "⚠️ ПРОСРОЧЕНО"
            should_remind = True
        elif total_seconds <= 3600:  # За 1 час
            urgency = "🔴 Осталось менее 1 часа"
            should_remind = True
        elif total_seconds <= 10800:  # За 3 часа
            urgency = "🟠 Осталось менее 3 часов"
            should_remind = _check_escalation_window(total_seconds, 3600, 10800)
        elif total_seconds <= 86400:  # За 24 часа
            urgency = "🟡 Завтра дедлайн"
            should_remind = _check_escalation_window(total_seconds, 10800, 86400)
        else:
            continue

        if not should_remind:
            continue

        title = task.get("title", "Задача")
        task_id = task.get("id", "")

        if total_seconds < 0:
            time_text = f"просрочено {_format_delta(abs(total_seconds))} назад"
        else:
            time_text = f"через {_format_delta(total_seconds)}"

        text = (
            f"{urgency}\n"
            f"📋 *{title}*\n"
            f"Дедлайн: {due_dt.astimezone(zoneinfo.ZoneInfo(config.TIMEZONE)).strftime('%d.%m.%Y %H:%M')} ({time_text})"
        )

        keyboard = _make_snooze_keyboard(task_id) if task_id else None

        for user_id in config.ALLOWED_IDS:
            try:
                await bot.send_message(
                    user_id,
                    text,
                    parse_mode="Markdown",
                    reply_markup=keyboard,
                )
            except Exception as e:
                logger.error(
                    "Ошибка отправки напоминания пользователю %s: %s", user_id, e
                )


def _check_escalation_window(
    total_seconds: float, window_start: float, window_end: float
) -> bool:
    """
    Определяет, нужно ли отправлять напоминание в данном окне.
    Отправляем один раз при входе в окно (в течение одного интервала проверки).
    """
    interval_seconds = config.REMINDER_INTERVAL_HOURS * 3600
    # Напоминаем если вошли в окно в течение последнего интервала
    return total_seconds <= window_end and total_seconds > (window_end - interval_seconds)


def _format_delta(seconds: float) -> str:
    """Форматирует количество секунд в читаемую строку."""
    seconds = int(abs(seconds))
    if seconds < 3600:
        mins = seconds // 60
        return f"{mins} мин"
    elif seconds < 86400:
        hours = seconds // 3600
        mins = (seconds % 3600) // 60
        if mins:
            return f"{hours}ч {mins}мин"
        return f"{hours}ч"
    else:
        days = seconds // 86400
        hours = (seconds % 86400) // 3600
        if hours:
            return f"{days}д {hours}ч"
        return f"{days}д"


async def sync_completed_tasks(bot) -> None:
    """
    Каждые 15 минут проверяет недавно выполненные задачи и удаляет
    связанные с ними события-блоки в Google Calendar (📋 <название>).
    """
    now = datetime.now(timezone.utc)
    # Ищем чуть шире интервала (20 мин) чтобы не промахнуться из-за drift
    try:
        completed = await tasks_svc.get_recently_completed_tasks(minutes=20)
    except Exception as e:
        logger.error("sync_completed_tasks: ошибка получения задач: %s", e)
        return

    if not completed:
        return

    # Ищем события в окне ±30 дней от сейчас (прошедшие блоки нас не интересуют)
    date_from = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    date_to = (now + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")

    deleted_titles: list[str] = []

    for task in completed:
        title = task.get("title", "").strip()
        if not title:
            continue
        event_title = f"📋 {title}"

        try:
            events = await cal_svc.find_events_by_title(event_title, date_from, date_to)
        except Exception as e:
            logger.error("sync_completed_tasks: ошибка поиска событий для '%s': %s", title, e)
            continue

        for event in events:
            try:
                await cal_svc.delete_event(event["id"])
                logger.info("sync_completed_tasks: удалено событие '%s' (task: '%s')", event_title, title)
            except Exception as e:
                logger.error("sync_completed_tasks: ошибка удаления события '%s': %s", event_title, e)

        if events:
            deleted_titles.append(title)

    if deleted_titles:
        lines = ["✅ *Синхронизация:* задачи выполнены, блоки в календаре удалены:"]
        for t in deleted_titles:
            lines.append(f"  • {t}")
        text = "\n".join(lines)
        for user_id in config.ALLOWED_IDS:
            try:
                await bot.send_message(user_id, text, parse_mode="Markdown")
            except Exception as e:
                logger.error("sync_completed_tasks: ошибка отправки уведомления: %s", e)
