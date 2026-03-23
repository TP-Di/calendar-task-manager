"""
Обработчики команд: /start /help /status /load /done /postpone /clear
"""

import logging
from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup

from app.config import config
from app.db.database import clear_history
import app.services.calendar as cal
import app.services.tasks as tasks_svc

logger = logging.getLogger(__name__)
router = Router()

MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📊 Статус"), KeyboardButton(text="📅 Нагрузка")],
        [KeyboardButton(text="🗓 Что сегодня?"), KeyboardButton(text="📋 Задачи")],
    ],
    resize_keyboard=True,
    input_field_placeholder="Напиши или выбери действие...",
)


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    """Приветственное сообщение."""
    await message.answer(
        "👋 Привет! Я твой персональный планировщик.\n\n"
        "Просто напиши что нужно сделать — добавить занятие, перенести встречу, посмотреть расписание.",
        parse_mode=None,
        reply_markup=MAIN_KB,
    )


@router.message(F.text == "📊 Статус")
async def btn_status(message: Message) -> None:
    await cmd_status(message)


@router.message(F.text == "📅 Нагрузка")
async def btn_load(message: Message) -> None:
    await cmd_load(message)


@router.message(F.text == "🗓 Что сегодня?")
async def btn_today(message: Message) -> None:
    """Показывает события на сегодня."""
    now = datetime.now(timezone.utc)
    day_end = now.replace(hour=23, minute=59, second=59)
    try:
        events = await cal.get_events(now.isoformat(), day_end.isoformat())
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
        return
    if not events:
        await message.answer("На сегодня событий нет ✅")
        return
    lines = ["*📅 Сегодня:*"]
    for ev in events:
        start = ev.get("start", "")
        time_str = ""
        if "T" in start:
            try:
                dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
                time_str = dt.strftime("%H:%M") + " "
            except Exception:
                pass
        lines.append(f"  • {time_str}{ev.get('title', '')}")
    await message.answer("\n".join(lines), parse_mode="Markdown")


@router.message(F.text == "📋 Задачи")
async def btn_tasks(message: Message) -> None:
    """Показывает активные задачи."""
    try:
        tasks = await tasks_svc.get_tasks()
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
        return
    if not tasks:
        await message.answer("Активных задач нет ✅")
        return
    now = datetime.now(timezone.utc)
    lines = ["*📋 Активные задачи:*"]
    for t in tasks[:15]:
        due = t.get("due", "")
        due_str = ""
        prefix = "  •"
        if due:
            try:
                due_dt = datetime.fromisoformat(due.replace("Z", "+00:00"))
                due_str = f" → {due_dt.strftime('%d.%m')}"
                if due_dt < now:
                    prefix = "  ⚠️"
            except Exception:
                pass
        lines.append(f"{prefix} {t['title']}{due_str}")
    await message.answer("\n".join(lines), parse_mode="Markdown")


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    """Список доступных команд."""
    text = (
        "📖 *Справка:*\n\n"
        "Просто пиши что нужно — я пойму без команд.\n\n"
        "*Кнопки:*\n"
        "📊 Статус — задачи и события на 3 дня\n"
        "📅 Нагрузка — часы событий на неделю\n"
        "🗓 Что сегодня? — события на сегодня\n"
        "📋 Задачи — список активных задач\n\n"
        "*Команды:*\n"
        "/upload — загрузить PDF с расписанием\n"
        "/clear — сбросить историю диалога"
    )
    await message.answer(text, parse_mode="Markdown", reply_markup=MAIN_KB)


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    """Показывает активные задачи и ближайшие события."""
    user_id = message.from_user.id
    await message.answer("⏳ Загружаю данные...")

    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    three_days_later = today_start + timedelta(days=3)

    lines = ["📊 *Текущий статус*\n"]

    # Ближайшие события
    try:
        events = await cal.get_events(now.isoformat(), three_days_later.isoformat())
        lines.append("*📅 Ближайшие события (3 дня):*")
        if events:
            for ev in events[:8]:
                start = ev.get("start", "")
                time_str = ""
                if "T" in start:
                    try:
                        dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
                        time_str = dt.strftime("%d.%m %H:%M")
                    except Exception:
                        time_str = start
                title = ev.get("title", "")
                lines.append(f"  • {time_str} — {title}")
        else:
            lines.append("  Нет событий")
    except Exception as e:
        logger.error("Ошибка Calendar API (/status): %s", e)
        lines.append("  ❌ Ошибка загрузки событий")

    # Активные задачи
    lines.append("")
    try:
        tasks = await tasks_svc.get_tasks()
        lines.append("*📋 Активные задачи:*")
        if tasks:
            # Сортируем: просроченные в топе
            overdue = []
            normal = []
            for t in tasks:
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

            for t in overdue:
                lines.append(f"  ⚠️ {t['title']}")
            for t in normal[:10]:
                due = t.get("due", "")
                due_str = ""
                if due:
                    try:
                        due_dt = datetime.fromisoformat(due.replace("Z", "+00:00"))
                        due_str = f" → {due_dt.strftime('%d.%m')}"
                    except Exception:
                        pass
                lines.append(f"  • {t['title']}{due_str}")
        else:
            lines.append("  Нет активных задач ✅")
    except Exception as e:
        logger.error("Ошибка Tasks API (/status): %s", e)
        lines.append("  ❌ Ошибка загрузки задач")

    await message.answer("\n".join(lines), parse_mode="Markdown")


@router.message(Command("load"))
async def cmd_load(message: Message) -> None:
    """Показывает нагрузку (часов событий) на текущую неделю."""
    await message.answer("⏳ Считаю нагрузку...")

    now = datetime.now(timezone.utc)
    # Начало текущей недели (понедельник)
    week_start = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    week_end = week_start + timedelta(days=7)

    try:
        events = await cal.get_events(week_start.isoformat(), week_end.isoformat())
    except Exception as e:
        logger.error("Ошибка Calendar API (/load): %s", e)
        await message.answer("❌ Ошибка загрузки событий из Calendar")
        return

    # Считаем часы по дням
    days_load: dict[int, float] = {i: 0.0 for i in range(7)}
    total_hours = 0.0

    for ev in events:
        start_str = ev.get("start", "")
        end_str = ev.get("end", "")
        if "T" not in start_str or "T" not in end_str:
            continue
        try:
            start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
            duration = (end_dt - start_dt).total_seconds() / 3600
            day_idx = (start_dt.weekday())  # 0=пн
            days_load[day_idx] = days_load.get(day_idx, 0.0) + duration
            total_hours += duration
        except Exception:
            continue

    day_names = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    lines = [
        f"📊 *Нагрузка на неделю ({week_start.strftime('%d.%m')} – {(week_end - timedelta(days=1)).strftime('%d.%m')}):*\n"
    ]
    for i, name in enumerate(day_names):
        hours = days_load.get(i, 0.0)
        bar = "█" * int(hours / 2) if hours > 0 else "·"
        lines.append(f"  {name}: {hours:.1f}ч {bar}")

    lines.append(f"\n*Итого:* {total_hours:.1f}ч за неделю")
    lines.append(f"*Событий:* {len(events)}")

    await message.answer("\n".join(lines), parse_mode="Markdown")


@router.message(Command("done"))
async def cmd_done(message: Message) -> None:
    """Отмечает задачу выполненной по частичному совпадению названия."""
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer(
            "Укажи название задачи: /done Сдать отчёт"
        )
        return

    query = args[1].strip().lower()

    try:
        tasks = await tasks_svc.get_tasks()
    except Exception as e:
        logger.error("Ошибка Tasks API (/done): %s", e)
        await message.answer("❌ Ошибка загрузки задач")
        return

    matches = [t for t in tasks if query in t.get("title", "").lower()]

    if not matches:
        await message.answer(f'Задача "{args[1]}" не найдена среди активных.')
        return

    if len(matches) > 1:
        names = "\n".join(f"  • {t['title']}" for t in matches[:5])
        await message.answer(
            f"Найдено несколько задач, уточни название:\n{names}"
        )
        return

    task = matches[0]
    try:
        await tasks_svc.complete_task(task["id"])
        await message.answer(f"✅ Задача выполнена: *{task['title']}*", parse_mode="Markdown")
    except Exception as e:
        logger.error("Ошибка Tasks API (complete_task): %s", e)
        await message.answer(f"❌ Ошибка при выполнении задачи: {e}")


@router.message(Command("postpone"))
async def cmd_postpone(message: Message) -> None:
    """
    Откладывает задачу. Формат: /postpone Название задачи 2024-01-20
    Делегирует агенту для интерпретации времени.
    """
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer(
            "Укажи название и новое время: /postpone Сдать отчёт завтра в 18:00"
        )
        return

    # Делегируем агенту
    from app.services.agent import run_agent

    user_id = message.from_user.id
    prompt = f"Отложи задачу: {args[1]}"

    await message.answer("⏳ Обрабатываю запрос...")
    response = await run_agent(user_id, prompt)

    from app.handlers.messages import handle_agent_response
    await handle_agent_response(message, response, user_id)


@router.message(Command("clear"))
async def cmd_clear(message: Message) -> None:
    """Очищает историю диалога пользователя."""
    user_id = message.from_user.id
    await clear_history(user_id)
    await message.answer(
        "🗑 История диалога очищена. Начинаем с чистого листа!"
    )
