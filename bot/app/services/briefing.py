"""
Утренний брифинг — cron-задача, отправляет сводку всем пользователям из whitelist.
"""

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

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
    now = datetime.now(ZoneInfo(config.TIMEZONE))
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
    Воскресный ретро-брифинг (вечер воскресенья по TIMEZONE).
    Отправляет heatmap прошедшей недели + текстовую статистику.
    """
    from zoneinfo import ZoneInfo
    from aiogram.types import BufferedInputFile
    from app.handlers.commands import _generate_heatmap_image

    tz = ZoneInfo(config.TIMEZONE)
    now_local = datetime.now(tz)
    # Пн–Вс этой недели
    week_start = (now_local - timedelta(days=now_local.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    week_end = week_start + timedelta(days=7)

    try:
        week_events = await cal.get_events(week_start.isoformat(), week_end.isoformat())
    except Exception as e:
        logger.error("Ошибка получения событий для ретро: %s", e)
        week_events = []

    # Статистика
    total_hours = sum(
        (
            datetime.fromisoformat(e["end"].replace("Z", "+00:00")) -
            datetime.fromisoformat(e["start"].replace("Z", "+00:00"))
        ).total_seconds() / 3600
        for e in week_events
        if e.get("start") and e.get("end") and "T" in e.get("start", "")
    )

    try:
        active_tasks = await tasks_svc.get_tasks()
        tasks_line = f"*Активных задач:* {len(active_tasks)}"
    except Exception:
        tasks_line = ""

    date_range = f"{week_start.strftime('%d.%m')} – {(week_end - timedelta(days=1)).strftime('%d.%m')}"
    caption = (
        f"📊 *Итоги недели {date_range}*\n"
        f"*Событий:* {len(week_events)}  •  *Часов:* {total_hours:.1f}ч\n"
        + (tasks_line + "\n" if tasks_line else "")
        + "\nХорошей недели! 🚀"
    )

    # Генерируем heatmap за прошедшую неделю
    try:
        img_bytes = await _generate_heatmap_image(
            week_events, config.TIMEZONE, week_start=week_start
        )
        photo = BufferedInputFile(img_bytes, filename="retro.png")
        for user_id in config.ALLOWED_IDS:
            try:
                await bot.send_photo(user_id, photo, caption=caption, parse_mode="Markdown")
            except Exception as e:
                logger.error("Ошибка отправки ретро пользователю %s: %s", user_id, e)
    except Exception as e:
        logger.error("Ошибка генерации heatmap для ретро: %s", e)
        # Fallback: только текст
        for user_id in config.ALLOWED_IDS:
            try:
                await bot.send_message(user_id, caption, parse_mode="Markdown")
            except Exception as ex:
                logger.error("Ошибка отправки текстового ретро %s: %s", user_id, ex)
