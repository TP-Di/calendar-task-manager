"""
Утренний брифинг — cron-задача, отправляет сводку всем пользователям из whitelist.
"""

import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot

from app.config import config
import app.services.calendar as cal
import app.services.tasks as tasks_svc

logger = logging.getLogger(__name__)


def _format_event_line(event: dict) -> str:
    """Форматирует одну строку события."""
    start = event.get("start", "")
    title = event.get("title", "")
    desc = event.get("description", "")

    # Время — только HH:MM если содержит T
    time_part = ""
    if "T" in start:
        try:
            dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
            time_part = dt.strftime("%H:%M")
        except Exception:
            time_part = start

    tags = ""
    if "[HARD]" in desc:
        tags = " 🔒"
    elif "[SOFT]" in desc:
        tags = " 🔄"

    if time_part:
        return f"  • {time_part} — {title}{tags}"
    return f"  • {title}{tags}"


def _format_task_line(task: dict, now: datetime) -> str:
    """Форматирует одну строку задачи."""
    title = task.get("title", "")
    due = task.get("due", "")

    overdue_mark = ""
    due_str = ""
    if due:
        try:
            due_dt = datetime.fromisoformat(due.replace("Z", "+00:00"))
            if due_dt < now:
                overdue_mark = "⚠️ "
            else:
                due_str = f" (до {due_dt.strftime('%d.%m %H:%M')})"
        except Exception:
            due_str = f" (до {due})"

    return f"  {overdue_mark}• {title}{due_str}"


async def build_briefing_text() -> str:
    """Формирует текст утреннего брифинга."""
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = today_start + timedelta(days=1)
    tomorrow_end = today_start + timedelta(days=2)

    lines = ["📅 *Утренний брифинг*\n"]

    # --- События сегодня ---
    try:
        today_events = await cal.get_events(
            today_start.isoformat(), today_end.isoformat()
        )
    except Exception as e:
        logger.error("Ошибка получения событий (сегодня): %s", e)
        today_events = []

    lines.append(f"*Сегодня, {now.strftime('%d.%m.%Y')}:*")
    if today_events:
        for ev in today_events:
            lines.append(_format_event_line(ev))
    else:
        lines.append("  Нет событий")

    # --- События завтра ---
    try:
        tomorrow_events = await cal.get_events(
            today_end.isoformat(), tomorrow_end.isoformat()
        )
    except Exception as e:
        logger.error("Ошибка получения событий (завтра): %s", e)
        tomorrow_events = []

    tomorrow_dt = today_start + timedelta(days=1)
    lines.append(f"\n*Завтра, {tomorrow_dt.strftime('%d.%m.%Y')}:*")
    if tomorrow_events:
        for ev in tomorrow_events:
            lines.append(_format_event_line(ev))
    else:
        lines.append("  Нет событий")

    # --- Активные задачи ---
    try:
        active_tasks = await tasks_svc.get_tasks()
    except Exception as e:
        logger.error("Ошибка получения задач: %s", e)
        active_tasks = []

    # Разделяем просроченные и остальные
    overdue = []
    normal = []
    for t in active_tasks:
        due = t.get("due", "")
        if due:
            try:
                due_dt = datetime.fromisoformat(due.replace("Z", "+00:00"))
                if due_dt < now:
                    overdue.append(t)
                    continue
            except Exception:
                pass
        normal.append(t)

    lines.append("\n*Задачи:*")
    if not active_tasks:
        lines.append("  Нет активных задач")
    else:
        # Просроченные в топе
        for t in overdue:
            lines.append(_format_task_line(t, now))
        for t in normal[:10]:  # Не более 10 задач
            lines.append(_format_task_line(t, now))
        if len(normal) > 10:
            lines.append(f"  ... и ещё {len(normal) - 10} задач")

    # --- Ближайшие дедлайны (события с ключевыми словами) ---
    try:
        week_events = await cal.get_events(
            today_start.isoformat(),
            (today_start + timedelta(days=7)).isoformat(),
        )
        deadlines = [
            ev for ev in week_events
            if any(
                kw in ev.get("title", "").lower() or kw in ev.get("description", "").lower()
                for kw in ["дедлайн", "deadline", "экзамен", "exam", "ielts", "сдача"]
            )
        ]
    except Exception as e:
        logger.error("Ошибка получения дедлайнов: %s", e)
        deadlines = []

    if deadlines:
        lines.append("\n*Ближайшие дедлайны и экзамены:*")
        for ev in deadlines[:5]:
            lines.append(_format_event_line(ev))

    return "\n".join(lines)


async def send_briefing(bot: Bot) -> None:
    """Отправляет брифинг всем пользователям из whitelist."""
    text = await build_briefing_text()

    for user_id in config.ALLOWED_IDS:
        try:
            await bot.send_message(
                user_id,
                text,
                parse_mode="Markdown",
            )
            logger.info("Брифинг отправлен: %s", user_id)
        except Exception as e:
            logger.error("Ошибка отправки брифинга пользователю %s: %s", user_id, e)


async def send_weekly_retro(bot: Bot) -> None:
    """
    Воскресный ретро-брифинг (20:00).
    Показывает что было сделано за неделю.
    """
    now = datetime.now(timezone.utc)
    week_start = (now - timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)

    lines = ["📊 *Недельный ретро*\n"]

    try:
        week_events = await cal.get_events(week_start.isoformat(), now.isoformat())
        lines.append(f"*Событий за неделю:* {len(week_events)}")
    except Exception as e:
        logger.error("Ошибка получения событий для ретро: %s", e)
        lines.append("Не удалось загрузить события")

    try:
        active_tasks = await tasks_svc.get_tasks()
        lines.append(f"*Активных задач сейчас:* {len(active_tasks)}")
    except Exception as e:
        logger.error("Ошибка получения задач для ретро: %s", e)

    lines.append("\nХорошей недели! 🚀")
    text = "\n".join(lines)

    for user_id in config.ALLOWED_IDS:
        try:
            await bot.send_message(user_id, text, parse_mode="Markdown")
        except Exception as e:
            logger.error("Ошибка отправки ретро пользователю %s: %s", user_id, e)
