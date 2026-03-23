"""
AI агент на базе Groq API с tool calling.
Системный промпт, цикл tool calling, история диалога.
"""

import json
import logging
from datetime import datetime, timezone

from groq import AsyncGroq

from app.config import config
from app.db.database import add_message, get_history
from app.tools.definitions import TOOLS
import app.services.calendar as cal
import app.services.tasks as tasks_svc

logger = logging.getLogger(__name__)

# Системный промпт агента
SYSTEM_PROMPT = """Ты — персональный ИИ-планировщик. Помогаешь управлять расписанием и задачами через Google Calendar и Google Tasks.

## Приоритеты (строго по убыванию):
1. Бакалавр — самый высокий
2. Работа — жёсткие временные слоты
3. Магистратура (IELTS, OMSA)
4. Проекты
5. Курсы — самый низкий

## Теги событий:
- [HARD] — НИКОГДА не двигать, не удалять
- [SOFT] — можно двигать при конфликте
- [PRIORITY:x] — приоритет из списка
- [DEPENDS:название] — зависимость от другого события/задачи

## Правила работы:
1. Для ИЗМЕНЕНИЙ (create/update/delete/bulk_create) — вызывай нужный tool. Система автоматически покажет пользователю диалог подтверждения. НЕ описывай список в тексте — просто вызови tool.
2. Для delete_event и update_event — СНАЧАЛА вызови get_events чтобы получить реальный event_id, ЗАТЕМ вызывай delete_event/update_event с этим ID. Никогда не придумывай ID.
3. Для чтения (get_events, get_tasks) — выполняй сразу без подтверждения.
4. Если есть конфликт — предложи перепланирование снизу вверх по приоритетам. [HARD] не трогать никогда.
5. Если запрос непонятен — честно скажи об этом, не угадывай.
6. Отвечай кратко и по делу.
7. Текущее время: {current_time} (временная зона пользователя: {timezone})
8. Все времена от пользователя — в его локальной зоне {timezone}. Передавай их в инструменты КАК ЕСТЬ, без конвертации в UTC.

## Работа с расписанием:
- Когда пользователь присылает недельное расписание (дни недели → занятия/встречи), используй `bulk_create_events`.
- Для повторяющихся событий ВСЕГДА используй поле `recurrence` с RRULE вместо создания N отдельных событий.
- Дата `start` — ПЕРВОЕ вхождение события (дата ближайшего такого дня недели от начала расписания).
- COUNT=N означает N ВСЕГО вхождений, считая первое. `"RRULE:FREQ=WEEKLY;COUNT=9"` — 9 занятий суммарно.
- Каждый уникальный день/время — одно событие с recurrence. Не дублируй события отдельными записями на каждую неделю.
- Для университетских занятий ставь tag="PRIORITY:бакалавр", reminder_minutes=30, аудиторию в description.
- Не жди команд — понимай намерение из контекста. Фразы "добавь это", "перенеси X на час позже", "удали все пятничные занятия" — выполняй через нужный tool без лишних уточнений.

## Формат расписания:
- Строки вида `ПРЕДМЕТ АУДИТОРИЯ ЧЧ:ММ - ЧЧ:ММ`, например `CD B101 9:30 - 11:00`:
  - ПРЕДМЕТ (первый токен: CD, MA, IoT, EE…) → поле `title`
  - АУДИТОРИЯ (второй токен: B101, B209…) → поле `description`
  - Время → поля `start`/`end` (ISO 8601 с датой соответствующего дня недели)
- MON=понедельник, TUE=вторник, WED=среда, THU=четверг, FRI=пятница
- Несколько занятий через запятую в одной строке — создавай отдельное событие на каждое

## Формат RRULE:
- UNTIL в компактном формате БЕЗ дефисов и двоеточий, с суффиксом Z: `UNTIL=20260515T235959Z`
- НЕПРАВИЛЬНО: `UNTIL=2026-05-15T23:59:59` — дефисы и двоеточия запрещены в RRULE UNTIL
- ПРАВИЛЬНО: `UNTIL=20260515T235959Z`

## При вызове update_event и delete_event:
- После get_events у тебя есть название и время события
- ВСЕГДА передавай `event_title` и `event_start` в вызов update_event / delete_event
- Пример: `{{"event_id": "abc123", "event_title": "CD", "event_start": "2026-03-23T09:30:00+05:00", "fields": {{...}}}}`

## При вызове complete_task, delete_task и update_task:
- После get_tasks у тебя есть название задачи
- ВСЕГДА передавай `task_title` в вызов complete_task / delete_task / update_task
- Пример: `{{"task_id": "abc123", "task_title": "Изучить Airflow"}}`

## Планирование задач по времени:
- Когда пользователь просит "поставить задачу в свободное время" — сначала вызови get_events на нужный день, найди свободные слоты, затем создай задачу через create_task с полями start_time и end_time.
- "Свободное время до 19:30" = нет событий в этот период. Заполни доступные часы: start_time = конец последнего события (или текущее время), end_time = 19:30 (или start + нужное кол-во часов, но не позже лимита).
- Если задача не вмещается — создай несколько задач-блоков.
- При просьбе "сократить задачу на X часов" → update_task, уменьши end_time.
- При просьбе "создать такую же на оставшееся время" → get_events для поиска следующего свободного слота, create_task.

## Дедлайн (due) задачи:
- Если пользователь не упомянул срок сдачи — ставь due = конец текущего дня (HH:MM:SS = 23:59:59).
- Если пользователь явно сказал "без дедлайна", "дедлайна нет" — НЕ передавай поле due вообще.
- due — это срок сдачи, НЕ время работы. start_time/end_time — это блок времени в расписании.

## Формат ответа:
- Для любых изменений (добавить/удалить/изменить) — ТОЛЬКО вызов tool, никакого текстового описания.
- Никогда не пиши "Мероприятия добавлены" или список дат — это делает система после реального вызова tool.
- Просроченные задачи выделяй: ⚠️
"""


def _get_system_prompt() -> str:
    """Возвращает системный промпт с текущим временем."""
    import zoneinfo
    try:
        tz = zoneinfo.ZoneInfo(config.TIMEZONE)
    except Exception:
        tz = timezone.utc
    now = datetime.now(tz).strftime("%Y-%m-%d %H:%M")
    return SYSTEM_PROMPT.format(current_time=now, timezone=config.TIMEZONE)


# Диспетчер: имя_tool → функция
_TOOL_DISPATCH = {
    "get_events": lambda args: cal.get_events(args["date_from"], args["date_to"]),
    "create_event": lambda args: cal.create_event(
        args["title"],
        args["start"],
        args["end"],
        args.get("description", ""),
        args.get("tag", ""),
        args.get("recurrence"),
        args.get("reminder_minutes"),
    ),
    "update_event": lambda args: cal.update_event(args["event_id"], args["fields"]),
    "delete_event": lambda args: cal.delete_event(args["event_id"]),
    "bulk_create_events": lambda args: cal.bulk_create_events(args["events"]),
    "get_tasks": lambda args: tasks_svc.get_tasks(),
    "create_task": lambda args: tasks_svc.create_task(
        args["title"],
        args.get("due", ""),
        args.get("description", ""),
        args.get("start_time", ""),
        args.get("end_time", ""),
    ),
    "complete_task": lambda args: tasks_svc.complete_task(args["task_id"]),
    "delete_task": lambda args: tasks_svc.delete_task(args["task_id"]),
    "update_task": lambda args: tasks_svc.update_task(args["task_id"], args["fields"]),
}

# Инструменты которые ТРЕБУЮТ подтверждения (вызывающий код должен проверять)
CONFIRMATION_REQUIRED_TOOLS = {
    "create_event",
    "bulk_create_events",
    "update_event",
    "delete_event",
    "create_task",
    "complete_task",
    "delete_task",
    "update_task",
}


async def run_agent(user_id: int, user_message: str) -> str:
    """
    Основной цикл агента:
    1. Сохраняем сообщение пользователя
    2. Собираем историю + системный промпт
    3. Отправляем в Groq, обрабатываем tool calls
    4. Возвращаем финальный текст ответа

    Если агент запрашивает модифицирующий tool — возвращает специальный
    маркер вида: PENDING_TOOL::<json> для последующего подтверждения.
    """
    client = AsyncGroq(api_key=config.GROQ_API_KEY)

    # Сохраняем сообщение пользователя в историю
    await add_message(user_id, "user", user_message)

    # Получаем историю
    history = await get_history(user_id)

    # Формируем messages для API
    messages = [{"role": "system", "content": _get_system_prompt()}] + history

    # Tool calling loop (максимум 5 итераций)
    for iteration in range(5):
        try:
            response = await client.chat.completions.create(
                model=config.GROQ_MODEL,
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
                max_tokens=4096,
                temperature=0.3,
            )
        except Exception as e:
            err_str = str(e)
            # Groq tool_use_failed: LLM сгенерировал невалидный tool call.
            # Добавляем подсказку в контекст и повторяем итерацию.
            if "tool_use_failed" in err_str or "failedgeneration" in err_str:
                logger.warning("Groq tool_use_failed на итерации %d, retry с подсказкой", iteration)
                messages.append({
                    "role": "user",
                    "content": (
                        "Предыдущий вызов инструмента не удался. "
                        "Если нужен event_id — сначала вызови get_events чтобы его найти. "
                        "Не генерируй ID самостоятельно."
                    ),
                })
                continue
            logger.error("Ошибка Groq API: %s", e)
            return f"Ошибка при обращении к AI: {e}"

        choice = response.choices[0]
        message = choice.message

        # Нет tool calls — финальный ответ
        if not message.tool_calls:
            final_text = message.content or "(нет ответа)"
            await add_message(user_id, "assistant", final_text)
            return final_text

        # Добавляем ответ ассистента с tool calls в messages
        messages.append(
            {
                "role": "assistant",
                "content": message.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in message.tool_calls
                ],
            }
        )

        # Обрабатываем каждый tool call
        for tool_call in message.tool_calls:
            tool_name = tool_call.function.name
            try:
                tool_args = json.loads(tool_call.function.arguments)
            except json.JSONDecodeError:
                tool_args = {}

            logger.info("Tool call: %s(%s)", tool_name, tool_args)

            # Если инструмент требует подтверждения — прерываем цикл
            # и возвращаем маркер для обработки в handler
            if tool_name in CONFIRMATION_REQUIRED_TOOLS:
                # Защита: update_event/delete_event без event_id → форсируем get_events
                if tool_name in ("update_event", "delete_event") and not tool_args.get("event_id"):
                    logger.warning(
                        "%s вызван без event_id, добавляем ошибку и повторяем итерацию",
                        tool_name,
                    )
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": json.dumps({
                            "error": (
                                f"event_id отсутствует. "
                                "Сначала вызови get_events чтобы найти нужное событие, "
                                "возьми его id из результата и передай в "
                                f"{tool_name}."
                            )
                        }, ensure_ascii=False),
                    })
                    break  # Выходим из inner for-loop, outer for-loop сделает retry

                # Защита: complete/delete/update_task без task_id → форсируем get_tasks
                if tool_name in ("complete_task", "delete_task", "update_task") and not tool_args.get("task_id"):
                    logger.warning(
                        "%s вызван без task_id, добавляем ошибку и повторяем итерацию",
                        tool_name,
                    )
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": json.dumps({
                            "error": (
                                f"task_id отсутствует. "
                                "Сначала вызови get_tasks чтобы найти нужную задачу, "
                                "возьми её id из результата и передай в "
                                f"{tool_name}."
                            )
                        }, ensure_ascii=False),
                    })
                    break

                pending = {
                    "tool_name": tool_name,
                    "tool_args": tool_args,
                    "tool_call_id": tool_call.id,
                    "messages": messages,
                    "user_id": user_id,
                }
                # Сохраняем нейтральный placeholder, чтобы следующий вызов LLM
                # не видел незакрытый запрос без ответа ассистента
                await add_message(
                    user_id,
                    "assistant",
                    "[Ожидаю ответа пользователя]",
                )
                return f"PENDING_TOOL::{json.dumps(pending, ensure_ascii=False)}"

            # Выполняем read-only tool
            if tool_name in _TOOL_DISPATCH:
                try:
                    result = await _TOOL_DISPATCH[tool_name](tool_args)
                    tool_result_str = json.dumps(result, ensure_ascii=False, default=str)
                except Exception as e:
                    logger.error("Ошибка выполнения tool %s: %s", tool_name, e)
                    tool_result_str = json.dumps({"error": str(e)}, ensure_ascii=False)
            else:
                tool_result_str = json.dumps(
                    {"error": f"Неизвестный инструмент: {tool_name}"},
                    ensure_ascii=False,
                )

            # Добавляем результат tool в messages
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": tool_result_str,
                }
            )

    # Превышено число итераций
    fallback = "Не удалось завершить запрос за отведённое число шагов."
    await add_message(user_id, "assistant", fallback)
    return fallback


def _format_tool_success(tool_name: str, result) -> str:
    """Формирует читаемое сообщение об успешном выполнении tool."""
    if tool_name == "bulk_create_events":
        count = len(result) if isinstance(result, list) else 1
        noun = "событие" if count == 1 else ("события" if count < 5 else "событий")
        lines = [f"✅ Создано {count} {noun} в Google Calendar:"]
        for ev in (result if isinstance(result, list) else [result]):
            title = ev.get("title", "")
            start = ev.get("start", "")[:16].replace("T", " ") if ev.get("start") else ""
            lines.append(f"• *{title}* — {start}")
        return "\n".join(lines)

    if tool_name == "create_event":
        ev = result if isinstance(result, dict) else {}
        title = ev.get("title", "")
        start = ev.get("start", "")[:16].replace("T", " ") if ev.get("start") else ""
        return f"✅ Создано: *{title}* — {start}"

    if tool_name == "delete_event":
        return "✅ Событие удалено."

    if tool_name == "update_event":
        ev = result if isinstance(result, dict) else {}
        title = ev.get("title", "")
        return f"✅ Событие обновлено: *{title}*"

    if tool_name == "create_task":
        t = result if isinstance(result, dict) else {}
        return f"✅ Задача создана: *{t.get('title', '')}*"

    if tool_name == "complete_task":
        return "✅ Задача отмечена выполненной."

    if tool_name == "delete_task":
        return "✅ Задача удалена."

    if tool_name == "update_task":
        return "✅ Задача обновлена."

    return "✅ Действие выполнено."


async def execute_pending_tool(pending_data: dict) -> str:
    """
    Выполняет отложенный tool call после подтверждения пользователем.
    Возвращает форматированный результат без лишнего вызова LLM.
    """
    tool_name = pending_data["tool_name"]
    tool_args = pending_data["tool_args"]
    user_id = pending_data["user_id"]

    # Последняя линия защиты: не выполнять update/delete с пустым ID
    if tool_name in ("update_event", "delete_event") and not tool_args.get("event_id"):
        error_text = "❌ event_id пустой — повторите запрос, я запрошу события автоматически."
        await add_message(user_id, "assistant", error_text)
        return error_text

    if tool_name in ("complete_task", "delete_task", "update_task") and not tool_args.get("task_id"):
        error_text = "❌ task_id пустой — повторите запрос, я запрошу задачи автоматически."
        await add_message(user_id, "assistant", error_text)
        return error_text

    if tool_name in _TOOL_DISPATCH:
        try:
            result = await _TOOL_DISPATCH[tool_name](tool_args)
        except Exception as e:
            logger.error("Ошибка выполнения tool %s: %s", tool_name, e)
            error_text = f"❌ Ошибка при выполнении: {e}"
            await add_message(user_id, "assistant", error_text)
            return error_text
    else:
        error_text = f"❌ Неизвестный инструмент: {tool_name}"
        await add_message(user_id, "assistant", error_text)
        return error_text

    final_text = _format_tool_success(tool_name, result)
    await add_message(user_id, "assistant", final_text)
    return final_text
