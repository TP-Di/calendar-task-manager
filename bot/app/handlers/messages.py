"""
Обработчик обычных сообщений → агент.
Также обрабатывает inline-кнопки подтверждения и snooze.
"""

import asyncio
import json
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.services.agent import execute_pending_tool, run_agent
import app.services.tasks as tasks_svc

logger = logging.getLogger(__name__)
router = Router()

# Хранилище ожидающих подтверждения tool calls: user_id -> pending_data
# В production лучше хранить в Redis или БД, здесь используем in-memory
_pending_confirmations: dict[int, dict] = {}


def _make_confirm_keyboard(action_description: str) -> InlineKeyboardMarkup:
    """Создаёт inline клавиатуру для подтверждения действия."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да", callback_data="confirm:yes"),
                InlineKeyboardButton(text="❌ Нет", callback_data="confirm:no"),
            ]
        ]
    )


async def handle_agent_response(
    message: Message, response: str, user_id: int
) -> None:
    """
    Разбирает ответ агента:
    - Если PENDING_TOOL — показывает подтверждение
    - Иначе — отправляет текст пользователю
    """
    if response.startswith("PENDING_TOOL::"):
        # Парсим pending данные
        json_str = response[len("PENDING_TOOL::"):]
        try:
            pending = json.loads(json_str)
        except json.JSONDecodeError:
            await message.answer("❌ Внутренняя ошибка агента")
            return

        # Сохраняем pending для последующего подтверждения
        _pending_confirmations[user_id] = pending

        # Формируем описание действия (батч или одиночное)
        tools = pending.get("tools") or []
        if not tools and pending.get("tool_name"):
            tools = [{"tool_name": pending["tool_name"], "tool_args": pending["tool_args"]}]

        if len(tools) == 1:
            description = _describe_tool_action(tools[0]["tool_name"], tools[0]["tool_args"])
        else:
            parts = [_describe_tool_action(t["tool_name"], t["tool_args"]) for t in tools]
            description = "\n\n".join(f"*{i+1}.* {p}" for i, p in enumerate(parts))

        try:
            await message.answer(
                f"🔔 *Подтверждение действия:*\n\n{description}\n\nВыполнить?",
                parse_mode="Markdown",
                reply_markup=_make_confirm_keyboard(description),
            )
        except Exception:
            await message.answer(
                f"🔔 Подтверждение действия:\n\n{description}\n\nВыполнить?",
                reply_markup=_make_confirm_keyboard(description),
            )
    else:
        # Обычный ответ агента
        try:
            await message.answer(response, parse_mode="Markdown")
        except Exception:
            # Если Markdown не парсится — отправляем как plain text
            await message.answer(response, parse_mode=None)


def _describe_bulk_create(events: list) -> str:
    """Формирует читаемое описание пачки событий для bulk_create_events.
    Каждое событие — отдельная строка (без группировки по времени),
    чтобы события с одинаковым временем в разные дни не сливались."""

    def _fmt_dt(iso: str) -> str:
        """DD.MM HH:MM из ISO-строки."""
        try:
            parts = iso[:10].split("-")
            return f"{parts[2]}.{parts[1]} {iso[11:16]}"
        except Exception:
            return iso[:16]

    total = len(events)
    noun = "событие" if total == 1 else ("события" if total < 5 else "событий")
    lines = [f"Создать *{total} {noun}* в Google Calendar:"]

    for ev in events:
        title = ev.get("title", "?")
        start = ev.get("start", "")
        end = ev.get("end", "")
        rrule = ev.get("recurrence", [])
        reminder = ev.get("reminder_minutes")
        desc = ev.get("description", "")

        end_time = end[11:16] if len(end) >= 16 else end
        reminder_str = f", 🔔{reminder}м" if reminder is not None else ""
        desc_str = f", {desc}" if desc else ""

        if rrule:
            # Используем () вместо [] чтобы не конфликтовать с Markdown-ссылками
            rule_str = rrule[0].replace("RRULE:", "")
            lines.append(f"• *{title}* с {_fmt_dt(start)} до {end_time} ({rule_str}){desc_str}{reminder_str}")
        else:
            lines.append(f"• *{title}* {_fmt_dt(start)}–{end_time}{desc_str}{reminder_str}")

    return "\n".join(lines)


_MONTHS_RU = [
    "янв", "фев", "мар", "апр", "мая", "июн",
    "июл", "авг", "сен", "окт", "ноя", "дек",
]


def _fmt_iso(iso: str) -> str:
    """ISO datetime → '24 мар 16:00' (или '24 мар' если время 00:00)."""
    try:
        date = iso[:10]
        time = iso[11:16]
        _, month, day = date.split("-")
        m = _MONTHS_RU[int(month) - 1]
        return f"{int(day)} {m} {time}" if time and time != "00:00" else f"{int(day)} {m}"
    except Exception:
        return iso[:16].replace("T", " ")


def _fmt_fields(fields: dict) -> str:
    """Словарь изменений → читаемый список строк."""
    lines = []
    for key, val in fields.items():
        label = {"title": "название", "start": "начало", "end": "конец",
                 "description": "описание", "due": "дедлайн",
                 "start_time": "начало", "end_time": "конец"}.get(key, key)
        formatted = _fmt_iso(str(val)) if key in ("start", "end", "due", "start_time", "end_time") else str(val)
        lines.append(f"  • {label}: {formatted}")
    return "\n".join(lines)


def _describe_tool_action(tool_name: str, tool_args: dict) -> str:
    """Формирует читаемое описание предстоящего действия."""
    if tool_name == "bulk_create_events":
        return _describe_bulk_create(tool_args.get("events", []))

    if tool_name == "update_event":
        fields = tool_args.get("fields", {})
        name = tool_args.get("event_title") or tool_args.get("event_id", "")
        orig = (
            f" ({_fmt_iso(tool_args['event_start'])})"
            if tool_args.get("event_start") else ""
        )
        return f"Обновить событие: *{name}*{orig}\n{_fmt_fields(fields)}"

    if tool_name == "delete_event":
        name = tool_args.get("event_title") or tool_args.get("event_id", "")
        orig = (
            f" ({_fmt_iso(tool_args['event_start'])})"
            if tool_args.get("event_start") else ""
        )
        return f"Удалить событие: *{name}*{orig}"

    if tool_name == "create_event":
        start = _fmt_iso(tool_args.get("start", ""))
        end_time = tool_args.get("end", "")[11:16]
        lines = [f"Создать событие: *{tool_args.get('title', '')}*",
                 f"Время: {start} – {end_time}"]
        if tool_args.get("recurrence"):
            rule = tool_args["recurrence"][0].replace("RRULE:", "")
            lines.append(f"Повторение: {rule}")
        if tool_args.get("reminder_minutes") is not None:
            lines.append(f"Напоминание: за {tool_args['reminder_minutes']} мин")
        if tool_args.get("description"):
            lines.append(f"Описание: {tool_args['description']}")
        return "\n".join(lines)

    if tool_name == "create_task":
        title = tool_args.get("title", "")
        lines = [f"Создать задачу: *{title}*"]
        if tool_args.get("start_time"):
            end_t = tool_args.get("end_time", "")
            sep = f" – {_fmt_iso(end_t)}" if end_t else ""
            lines.append(f"📅 Блок в календаре: {_fmt_iso(tool_args['start_time'])}{sep}")
        if tool_args.get("due"):
            lines.append(f"✅ Дедлайн: {_fmt_iso(tool_args['due'])}")
        elif not tool_args.get("start_time"):
            pass  # нет ни времени ни дедлайна — ок
        if tool_args.get("description"):
            lines.append(f"Описание: {tool_args['description']}")
        return "\n".join(lines)

    if tool_name in ("complete_task", "delete_task"):
        name = tool_args.get("task_title") or tool_args.get("task_id", "")
        verb = "Отметить выполненной" if tool_name == "complete_task" else "Удалить задачу"
        return f"{verb}: *{name}*"

    if tool_name == "update_task":
        name = tool_args.get("task_title") or tool_args.get("task_id", "")
        fields = tool_args.get("fields", {})
        return f"Обновить задачу: *{name}*\n{_fmt_fields(fields)}"

    return f"Выполнить: {tool_name}({tool_args})"


@router.callback_query(F.data.startswith("confirm:"))
async def handle_confirmation(callback: CallbackQuery) -> None:
    """Обрабатывает нажатие кнопок Да/Нет подтверждения."""
    user_id = callback.from_user.id
    answer = callback.data.split(":")[1]

    await callback.answer()

    if answer == "no":
        # Удаляем pending и отменяем
        _pending_confirmations.pop(user_id, None)
        try:
            await callback.message.edit_text(
                callback.message.text + "\n\n❌ Отменено.",
                reply_markup=None,
            )
        except Exception:
            pass
        return

    # Подтверждено — выполняем
    pending = _pending_confirmations.pop(user_id, None)
    if not pending:
        await callback.message.edit_text(
            "❌ Сессия подтверждения истекла. Повторите запрос.",
            reply_markup=None,
        )
        return

    try:
        await callback.message.edit_text(
            callback.message.text + "\n\n⏳ Выполняю...",
            reply_markup=None,
        )
    except Exception:
        pass

    try:
        result = await execute_pending_tool(pending)
        try:
            await callback.message.answer(result, parse_mode="Markdown")
        except Exception:
            await callback.message.answer(result, parse_mode=None)
    except Exception as e:
        logger.error("Ошибка выполнения подтверждённого действия: %s", e)
        await callback.message.answer(
            f"❌ Ошибка при выполнении: {e}"
        )


@router.callback_query(F.data.startswith("snooze:"))
async def handle_snooze(callback: CallbackQuery) -> None:
    """Обрабатывает snooze напоминания."""
    from datetime import datetime, timedelta, timezone
    import app.services.tasks as tasks_svc

    await callback.answer("Откладываю...")

    parts = callback.data.split(":")
    if len(parts) < 3:
        return

    task_id = parts[1]
    snooze_value = parts[2]

    now = datetime.now(timezone.utc)
    if snooze_value == "30":
        new_due = now + timedelta(minutes=30)
    elif snooze_value == "60":
        new_due = now + timedelta(hours=1)
    elif snooze_value == "tomorrow":
        tomorrow = now + timedelta(days=1)
        new_due = tomorrow.replace(hour=9, minute=0, second=0, microsecond=0)
    else:
        return

    try:
        await tasks_svc.update_task(task_id, {"due": new_due.isoformat()})
        time_str = new_due.strftime("%d.%m %H:%M")
        await callback.message.edit_text(
            callback.message.text + f"\n\n⏰ *Отложено до {time_str} UTC*",
            parse_mode="Markdown",
            reply_markup=None,
        )
    except Exception as e:
        logger.error("Ошибка snooze задачи %s: %s", task_id, e)
        await callback.message.answer(f"❌ Ошибка при откладывании: {e}")


@router.message()
async def handle_text_message(message: Message) -> None:
    """Обрабатывает обычные текстовые сообщения через агента."""
    if not message.text:
        return

    user_id = message.from_user.id
    user_text = message.text.strip()

    if not user_text:
        return

    # Если было ожидающее подтверждение — отменяем, чтобы не мешалось
    if user_id in _pending_confirmations:
        _pending_confirmations.pop(user_id)

    # Показываем индикатор обработки
    thinking_msg = await message.answer("🤔 Думаю...")

    try:
        response = await asyncio.wait_for(run_agent(user_id, user_text), timeout=90.0)
    except asyncio.TimeoutError:
        logger.error("Таймаут агента для user_id=%s", user_id)
        await thinking_msg.delete()
        await message.answer("⏱ Запрос занял слишком долго. Попробуй ещё раз или /clear для сброса истории.")
        return
    except Exception as e:
        logger.error("Ошибка агента для user_id=%s: %s", user_id, e)
        await thinking_msg.delete()
        await message.answer(
            f"❌ Произошла ошибка при обработке запроса: {e}\n\nПопробуй ещё раз или /clear для сброса истории."
        )
        return

    # Удаляем сообщение "думаю"
    try:
        await thinking_msg.delete()
    except Exception:
        pass

    await handle_agent_response(message, response, user_id)
