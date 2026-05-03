"""
Утренний брифинг — cron-задача, отправляет сводку всем пользователям из whitelist.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot

from app.config import config
import app.services.calendar as cal
import app.services.tasks as tasks_svc

# Семафор для рассылки нескольким пользователям — не превышаем Telegram rate limit
_SEND_SEM = asyncio.Semaphore(10)

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
    week_end = today_start + timedelta(days=7)

    lines = ["📅 *Утренний брифинг*\n"]

    # M7: один get_events за всю неделю + локальная фильтрация по дате,
    # вместо трёх отдельных API-запросов (сегодня/завтра/неделя).
    try:
        week_events = await cal.get_events(today_start.isoformat(), week_end.isoformat())
    except Exception as e:
        logger.error("Ошибка получения событий (неделя): %s", e)
        week_events = []

    def _ev_local_dt(ev):
        s = ev.get("start", "")
        if "T" not in s:
            return None
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(ZoneInfo(config.TIMEZONE))
        except Exception:
            return None

    today_events = [e for e in week_events if (d := _ev_local_dt(e)) and today_start <= d < today_end]
    tomorrow_events = [e for e in week_events if (d := _ev_local_dt(e)) and today_end <= d < tomorrow_end]

    lines.append(f"*Сегодня, {now.strftime('%d.%m.%Y')}:*")
    if today_events:
        for ev in today_events:
            lines.append(_format_event_line(ev))
    else:
        lines.append("  Нет событий")

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

    # --- Ближайшие дедлайны (фильтр по уже полученным week_events) ---
    deadlines = [
        ev for ev in week_events
        if any(
            kw in ev.get("title", "").lower() or kw in ev.get("description", "").lower()
            for kw in ["дедлайн", "deadline", "экзамен", "exam", "ielts", "сдача"]
        )
    ]

    if deadlines:
        lines.append("\n*Ближайшие дедлайны и экзамены:*")
        for ev in deadlines[:5]:
            lines.append(_format_event_line(ev))

    return "\n".join(lines)


async def send_briefing(bot: Bot) -> None:
    """Отправляет брифинг всем пользователям из whitelist параллельно (с семафором)."""
    text = await build_briefing_text()

    async def _send_one(uid: int) -> None:
        async with _SEND_SEM:
            try:
                await bot.send_message(uid, text, parse_mode="Markdown")
                logger.info("Брифинг отправлен: %s", uid)
            except Exception as e:
                logger.error("Ошибка отправки брифинга пользователю %s: %s", uid, e)

    await asyncio.gather(*[_send_one(u) for u in config.ALLOWED_IDS], return_exceptions=True)


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

        async def _send_photo(uid: int) -> None:
            async with _SEND_SEM:
                # BufferedInputFile создаём для каждого получателя — file_id не reusable до первой загрузки
                photo = BufferedInputFile(img_bytes, filename="retro.png")
                try:
                    await bot.send_photo(uid, photo, caption=caption, parse_mode="Markdown")
                except Exception as e:
                    logger.error("Ошибка отправки ретро пользователю %s: %s", uid, e)

        await asyncio.gather(*[_send_photo(u) for u in config.ALLOWED_IDS], return_exceptions=True)
    except Exception as e:
        logger.error("Ошибка генерации heatmap для ретро: %s", e)

        # Fallback: только текст
        async def _send_text(uid: int) -> None:
            async with _SEND_SEM:
                try:
                    await bot.send_message(uid, caption, parse_mode="Markdown")
                except Exception as ex:
                    logger.error("Ошибка отправки текстового ретро %s: %s", uid, ex)

        await asyncio.gather(*[_send_text(u) for u in config.ALLOWED_IDS], return_exceptions=True)
