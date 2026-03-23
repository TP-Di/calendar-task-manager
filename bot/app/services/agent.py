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
1. Для ИЗМЕНЕНИЙ (create/update/delete/bulk_create) — сразу вызывай нужный tool. Система автоматически покажет пользователю диалог подтверждения. НЕ описывай список в тексте, НЕ добавляй "Подтвердить? (Да/Нет)" — просто вызови tool.
2. Для чтения (get_events, get_tasks) — выполняй сразу без подтверждения.
3. Если есть конфликт — предложи перепланирование снизу вверх по приоритетам. [HARD] не трогать никогда.
4. Если запрос непонятен — честно скажи об этом, не угадывай.
5. Отвечай кратко и по делу.
6. Текущее время: {current_time} (временная зона пользователя: {timezone})
7. Все времена от пользователя — в его локальной зоне {timezone}. Передавай их в инструменты КАК ЕСТЬ, без конвертации в UTC.

## Работа с расписанием:
- Когда пользователь присылает недельное расписание (дни недели → занятия/встречи), используй `bulk_create_events`.
- Для повторяющихся событий ВСЕГДА используй поле `recurrence` с RRULE вместо создания N отдельных событий. Например: `"recurrence": ["RRULE:FREQ=WEEKLY;COUNT=9"]` для 9 еженедельных повторений. Дата `start` — первое вхождение.
- Если задана конечная дата вместо количества: `"recurrence": ["RRULE:FREQ=WEEKLY;UNTIL=20260515T235959Z"]`.
- Каждый уникальный день/время — одно событие с recurrence. Не дублируй события отдельными записями на каждую неделю.
- Для университетских занятий ставь тег PRIORITY:бакалавр и добавляй аудиторию в description.
- Не жди команд — понимай намерение из контекста. Фразы "добавь это", "перенеси X на час позже", "удали все пятничные занятия" — выполняй через нужный tool без лишних уточнений.

## Формат ответа:
- При предложении изменений используй чёткий список: что/когда/куда
- Для подтверждения добавляй в конце: "Подтвердить? (Да/Нет)"
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
    ),
    "update_event": lambda args: cal.update_event(args["event_id"], args["fields"]),
    "delete_event": lambda args: cal.delete_event(args["event_id"]),
    "bulk_create_events": lambda args: cal.bulk_create_events(args["events"]),
    "get_tasks": lambda args: tasks_svc.get_tasks(),
    "create_task": lambda args: tasks_svc.create_task(
        args["title"],
        args.get("due", ""),
        args.get("description", ""),
    ),
    "complete_task": lambda args: tasks_svc.complete_task(args["task_id"]),
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
                pending = {
                    "tool_name": tool_name,
                    "tool_args": tool_args,
                    "tool_call_id": tool_call.id,
                    "messages": messages,
                    "user_id": user_id,
                }
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


async def execute_pending_tool(pending_data: dict) -> str:
    """
    Выполняет отложенный tool call после подтверждения пользователем.
    Продолжает tool calling loop.
    """
    client = AsyncGroq(api_key=config.GROQ_API_KEY)

    tool_name = pending_data["tool_name"]
    tool_args = pending_data["tool_args"]
    tool_call_id = pending_data["tool_call_id"]
    messages = pending_data["messages"]
    user_id = pending_data["user_id"]

    # Выполняем tool
    if tool_name in _TOOL_DISPATCH:
        try:
            result = await _TOOL_DISPATCH[tool_name](tool_args)
            tool_result_str = json.dumps(result, ensure_ascii=False, default=str)
        except Exception as e:
            logger.error("Ошибка выполнения tool %s: %s", tool_name, e)
            tool_result_str = json.dumps({"error": str(e)}, ensure_ascii=False)
    else:
        tool_result_str = json.dumps(
            {"error": f"Неизвестный инструмент: {tool_name}"}, ensure_ascii=False
        )

    messages.append(
        {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": tool_result_str,
        }
    )

    # Продолжаем цикл для финального ответа
    for _ in range(3):
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
            logger.error("Ошибка Groq API (после подтверждения): %s", e)
            return f"Ошибка AI после выполнения действия: {e}"

        choice = response.choices[0]
        message = choice.message

        if not message.tool_calls:
            final_text = message.content or "(действие выполнено)"
            await add_message(user_id, "assistant", final_text)
            return final_text

        # Ещё tool calls — обрабатываем (только read-only на этом этапе)
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

        for tool_call in message.tool_calls:
            t_name = tool_call.function.name
            try:
                t_args = json.loads(tool_call.function.arguments)
            except json.JSONDecodeError:
                t_args = {}

            if t_name in _TOOL_DISPATCH and t_name not in CONFIRMATION_REQUIRED_TOOLS:
                try:
                    res = await _TOOL_DISPATCH[t_name](t_args)
                    res_str = json.dumps(res, ensure_ascii=False, default=str)
                except Exception as e:
                    res_str = json.dumps({"error": str(e)}, ensure_ascii=False)
            else:
                res_str = json.dumps(
                    {"error": "Требуется подтверждение для " + t_name},
                    ensure_ascii=False,
                )

            messages.append(
                {"role": "tool", "tool_call_id": tool_call.id, "content": res_str}
            )

    fallback = "Действие выполнено."
    await add_message(user_id, "assistant", fallback)
    return fallback
