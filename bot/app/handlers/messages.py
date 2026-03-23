"""
Обработчик обычных сообщений → агент.
Также обрабатывает inline-кнопки подтверждения и snooze.
"""

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

        # Формируем описание действия
        tool_name = pending.get("tool_name", "")
        tool_args = pending.get("tool_args", {})
        description = _describe_tool_action(tool_name, tool_args)

        await message.answer(
            f"🔔 *Подтверждение действия:*\n\n{description}\n\nВыполнить?",
            parse_mode="Markdown",
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
    """Формирует читаемое описание пачки событий для bulk_create_events."""
    from collections import defaultdict
    from datetime import datetime as _dt

    def _time(iso: str) -> str:
        try:
            return iso.split("T")[1][:5]
        except Exception:
            return iso

    groups: dict[tuple, list[dict]] = defaultdict(list)
    for ev in events:
        key = (ev.get("title", "?"), _time(ev.get("start", "")), _time(ev.get("end", "")))
        groups[key].append(ev)

    total_unique = len(groups)
    lines = [f"Создать *{total_unique} уникальных слота* расписания:"]
    for (title, t_start, t_end), group in groups.items():
        starts = sorted(ev.get("start", "") for ev in group)
        first_date = starts[0][:10] if starts else "?"
        rrule = group[0].get("recurrence", [])
        reminder = group[0].get("reminder_minutes")
        reminder_str = f", 🔔{reminder}м" if reminder is not None else ""
        if rrule:
            rule_str = rrule[0].replace("RRULE:", "")
            lines.append(f"• *{title}* {t_start}–{t_end}, с {first_date} [{rule_str}]{reminder_str}")
        elif len(group) > 1:
            last_date = starts[-1][:10]
            lines.append(f"• *{title}* {t_start}–{t_end}, ×{len(group)} ({first_date} – {last_date}){reminder_str}")
        else:
            lines.append(f"• *{title}* {t_start}–{t_end}, {first_date}{reminder_str}")
    return "\n".join(lines)


def _describe_tool_action(tool_name: str, tool_args: dict) -> str:
    """Формирует читаемое описание предстоящего действия."""
    if tool_name == "bulk_create_events":
        return _describe_bulk_create(tool_args.get("events", []))

    descriptions = {
        "create_event": (
            f"Создать событие: *{tool_args.get('title', '')}*\n"
            f"Начало: {tool_args.get('start', '')}\n"
            f"Конец: {tool_args.get('end', '')}"
            + (
                "\nПовторение: " + ", ".join(
                    r.replace("RRULE:", "") for r in tool_args["recurrence"]
                )
                if tool_args.get("recurrence") else ""
            )
            + (f"\nНапоминание: за {tool_args['reminder_minutes']} мин" if tool_args.get("reminder_minutes") is not None else "")
            + (f"\nОписание: {tool_args.get('description', '')}" if tool_args.get("description") else "")
        ),
        "update_event": (
            f"Обновить событие ID: `{tool_args.get('event_id', '')}`\n"
            f"Изменения: {json.dumps(tool_args.get('fields', {}), ensure_ascii=False)}"
        ),
        "delete_event": (
            f"Удалить событие ID: `{tool_args.get('event_id', '')}`"
        ),
        "create_task": (
            f"Создать задачу: *{tool_args.get('title', '')}*"
            + (f"\nДедлайн: {tool_args.get('due', '')}" if tool_args.get("due") else "")
        ),
        "complete_task": (
            f"Отметить задачу выполненной ID: `{tool_args.get('task_id', '')}`"
        ),
        "update_task": (
            f"Обновить задачу ID: `{tool_args.get('task_id', '')}`\n"
            f"Изменения: {json.dumps(tool_args.get('fields', {}), ensure_ascii=False)}"
        ),
    }
    return descriptions.get(tool_name, f"Выполнить: {tool_name}({tool_args})")


@router.callback_query(F.data.startswith("confirm:"))
async def handle_confirmation(callback: CallbackQuery) -> None:
    """Обрабатывает нажатие кнопок Да/Нет подтверждения."""
    user_id = callback.from_user.id
    answer = callback.data.split(":")[1]

    await callback.answer()

    if answer == "no":
        # Удаляем pending и отменяем
        _pending_confirmations.pop(user_id, None)
        await callback.message.edit_text(
            callback.message.text + "\n\n❌ *Отменено.*",
            parse_mode="Markdown",
            reply_markup=None,
        )
        return

    # Подтверждено — выполняем
    pending = _pending_confirmations.pop(user_id, None)
    if not pending:
        await callback.message.edit_text(
            "❌ Сессия подтверждения истекла. Повторите запрос.",
            reply_markup=None,
        )
        return

    await callback.message.edit_text(
        callback.message.text + "\n\n⏳ *Выполняю...*",
        parse_mode="Markdown",
        reply_markup=None,
    )

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
        response = await run_agent(user_id, user_text)
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
