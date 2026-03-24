"""
AI агент на базе Groq API с tool calling.
Системный промпт, цикл tool calling, история диалога.
"""

import json
import logging
from datetime import datetime, timezone, timedelta

from groq import AsyncGroq

from app.config import config
from app.db.database import add_message, get_history
from app.tools.definitions import TOOLS
import app.services.calendar as cal
import app.services.tasks as tasks_svc

logger = logging.getLogger(__name__)


def _is_placeholder(value: str) -> bool:
    """Возвращает True, если значение выглядит как плейсхолдер, а не реальный ID."""
    v = (value or "").strip()
    # <task_id>, <id>, ...
    if v.startswith("<") and v.endswith(">"):
        return True
    # ${get_tasks()[0].id}, ${task_id}, ...
    if v.startswith("${") and v.endswith("}"):
        return True
    # {task_id}, {id}
    if v.startswith("{") and v.endswith("}"):
        return True
    return False


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
8. Ближайшие 7 дней: {week_preview}
9. Все времена от пользователя — в его локальной зоне {timezone}. Передавай их в инструменты КАК ЕСТЬ, без конвертации в UTC.
9. Если в сообщении пользователя есть ссылки (URL начинающиеся с http:// или https://) — автоматически включай их в поле description при создании или редактировании событий и задач. Не спрашивай подтверждения — просто добавь ссылку в description.

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
- Передавай только `task_title` — система сама найдёт задачу по названию.
- `task_id` передавать НЕ НУЖНО. Если известен из get_tasks — можешь передать, иначе — пропусти.
- Пример: `{{"task_title": "Изучить Airflow", "fields": {{...}}}}`
- НИКОГДА не придумывай task_id самостоятельно — только из реального результата get_tasks.

## Распознавание намерений для задач и событий:
- "выполни задачу X", "сделал X", "отметь X выполненной", "X готово" → `complete_task`
- "удали задачу X", "убери задачу X" → `delete_task`
- "перенеси задачу X", "измени дедлайн X", "отложи задачу X" → `update_task`
- "перенеси событие X", "отложи встречу X", "сдвинь событие X" → `update_event` (сначала get_events)
- "добавь задачу X", "создай задачу X", "поставь задачу X" → `create_task`
- НИКОГДА не вызывай `create_task` если пользователь просит отметить задачу выполненной.

## Разрешение неоднозначности (нет слова "задача" или "событие"):
- Если пользователь говорит "перенеси X", "удали X", "выполни X" без уточнения типа —
  сначала вызови get_tasks И get_events параллельно, чтобы найти X.
- Если X найден только в Tasks → действуй только на задачу.
- Если X найден только в Calendar → действуй только на событие.
- Если X найден в обоих (📋 блок) → действуй на оба объекта.

## Задачи с блоком в календаре (📋 события):
- Задачи, созданные с start_time+end_time, имеют ДВА объекта: 📋 событие в Calendar И задачу в Tasks.
- При "перенеси X" где X видно в расписании как 📋 событие — вызывай ОБА: `update_event` (после get_events) И `update_task`.
- При "удали X" где X — 📋 событие-задача — вызывай ОБА: `delete_event` И `delete_task`.
- Признак задачи-блока в расписании: название события начинается с 📋 или совпадает с именем активной задачи.
- НЕЛЬЗЯ обновлять только одно из двух — всегда синхронизируй оба объекта.

## При выполнении задачи (complete_task) с 📋 блоком в календаре:
После `complete_task` проверь, есть ли у задачи связанное 📋 событие (по совпадению названия).
Если есть — определи позицию события относительно текущего момента (now = текущее UTC время):
- **Идёт прямо сейчас** (event.start <= now <= event.end): вызови `update_event` с `fields.end = now` (обрезать событие по факту завершения).
- **В будущем** (event.start > now): вызови `delete_event` (задача выполнена, блок времени больше не нужен).
- **В прошлом** (event.end < now): не трогай событие, оно уже завершено — пусть остаётся как история.

## Планирование задач по времени:
- Когда пользователь просит "поставить задачу в свободное время" — сначала вызови get_events на нужный день, найди свободные слоты, затем вызови create_task с полями start_time и end_time.
- Если start_time + end_time указаны, система автоматически создаст и блок в Google Calendar (📋 событие с тегом SOFT), и задачу в Google Tasks.
- "Свободное время до 19:30" = нет событий в этот период. start_time = конец последнего события (или сейчас), end_time = min(start + нужные часы, 19:30).
- Если задача не вмещается — создай несколько задач-блоков (несколько вызовов create_task).
- При просьбе "сократить задачу на X часов" → update_task с новым end_time в fields + update_event с новым end.
- При просьбе "создать такую же на оставшееся время" → get_events, create_task с новым слотом.

## Разрешение относительных дат:
Всегда переводи относительные даты в конкретный ISO-формат ПЕРЕД вызовом tool.
Используй поле "Ближайшие 7 дней" как шпаргалку — там уже указаны даты на неделю вперёд.

| Фраза | Правило |
|---|---|
| "сегодня" | текущая дата |
| "завтра" | текущая дата + 1 день |
| "послезавтра" | текущая дата + 2 дня |
| "в субботу", "в пятницу" и т.д. | ближайший такой день недели (включая сегодня, если сегодня он) |
| "на следующей неделе в субботу" | суббота СЛЕДУЮЩЕЙ календарной недели (пн–вс) |
| "через неделю" | текущая дата + 7 дней |
| "через 2 недели" | текущая дата + 14 дней |
| "в конце недели" | ближайшая пятница или суббота (смотри по контексту) |
| "в начале следующей недели" | ближайший понедельник следующей недели |
| "через N дней" | текущая дата + N дней |

Важно: "в субботу" = ближайшая суббота от сегодня. Если сегодня суббота — это сегодня. Если уже прошла — следующая.

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
    now = datetime.now(tz)
    weekdays_ru = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    weekdays_short = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
    wd = now.weekday()  # 0=Monday

    # Ближайшие 7 дней с датами для подсказки
    week_dates = []
    for i in range(7):
        d = now + timedelta(days=i)
        label = "сегодня" if i == 0 else ("завтра" if i == 1 else weekdays_ru[d.weekday()])
        week_dates.append(f"{weekdays_short[d.weekday()]} {d.strftime('%d.%m')} ({label})")

    current_time_str = (
        f"{now.strftime('%Y-%m-%d %H:%M')}, "
        f"{weekdays_ru[wd]} ({weekdays_short[wd]})"
    )
    week_preview = ", ".join(week_dates)
    return SYSTEM_PROMPT.format(
        current_time=current_time_str,
        timezone=config.TIMEZONE,
        week_preview=week_preview,
    )


async def _create_task_dispatch(args: dict) -> dict:
    """
    Если переданы start_time + end_time — создаёт календарный блок (📋 событие SOFT)
    И параллельно задачу в Google Tasks с дедлайном.
    Если только due — создаёт только задачу.
    """
    results: dict = {}
    if args.get("start_time") and args.get("end_time"):
        event = await cal.create_event(
            title=f"📋 {args['title']}",
            start=args["start_time"],
            end=args["end_time"],
            tag="SOFT",
            description=args.get("description", ""),
        )
        results["event"] = event

    task = await tasks_svc.create_task(
        args["title"],
        args.get("due", ""),
        args.get("description", ""),
    )
    results["task"] = task
    return results


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
    "create_task": _create_task_dispatch,
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
    client = AsyncGroq(api_key=config.GROQ_API_KEY, timeout=30.0)

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
                _eid = tool_args.get("event_id", "")
                if tool_name in ("update_event", "delete_event") and (not _eid or _is_placeholder(_eid)):
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

                # task_id для task-операций опционален — lookup по task_title
                # происходит в execute_pending_tool через _resolve_task_id.
                # Проверяем только что task_title не пустой.
                if tool_name in ("complete_task", "delete_task", "update_task"):
                    _tid = tool_args.get("task_id", "")
                    _ttitle = (tool_args.get("task_title") or "").strip()
                    if _is_placeholder(_tid) and not _ttitle:
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": json.dumps({
                                "error": "task_title не указан. Передай название задачи в поле task_title."
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
        r = result if isinstance(result, dict) else {}
        task = r.get("task", r)  # backwards compat if no event
        title = task.get("title", "")
        if r.get("event"):
            ev = r["event"]
            start = ev.get("start", "")[:16].replace("T", " ")
            end = ev.get("end", "")[:16].replace("T", " ")
            return f"✅ Задача создана: *{title}*\n✅ Блок в календаре: {start} – {end[11:]}"
        return f"✅ Задача создана: *{title}*"

    if tool_name == "complete_task":
        return "✅ Задача отмечена выполненной."

    if tool_name == "delete_task":
        return "✅ Задача удалена."

    if tool_name == "update_task":
        return "✅ Задача обновлена."

    return "✅ Действие выполнено."


async def _resolve_task_id(tool_args: dict) -> tuple[dict, str | None]:
    """
    Если task_id отсутствует или является плейсхолдером — ищет задачу по task_title.
    Возвращает (обновлённые args, сообщение об ошибке или None).
    """
    task_id = tool_args.get("task_id", "")
    if task_id and not _is_placeholder(task_id):
        return tool_args, None

    task_title = (tool_args.get("task_title") or "").strip()
    if not task_title:
        return tool_args, "❌ Не указано название задачи. Повторите запрос."

    try:
        tasks = await tasks_svc.get_tasks()
    except Exception as e:
        return tool_args, f"❌ Ошибка получения задач: {e}"

    title_lower = task_title.lower()
    matches = [t for t in tasks if title_lower in t.get("title", "").lower()]
    if not matches:
        return tool_args, f"❌ Задача «{task_title}» не найдена среди активных."

    # Точное совпадение предпочтительнее частичного
    exact = [t for t in matches if t.get("title", "").lower() == title_lower]
    found = exact[0] if exact else matches[0]
    return {**tool_args, "task_id": found["id"]}, None


async def execute_pending_tool(pending_data: dict) -> str:
    """
    Выполняет отложенный tool call после подтверждения пользователем.
    Возвращает форматированный результат без лишнего вызова LLM.
    """
    tool_name = pending_data["tool_name"]
    tool_args = pending_data["tool_args"]
    user_id = pending_data["user_id"]

    # Защита event_id
    _eid = tool_args.get("event_id", "")
    if tool_name in ("update_event", "delete_event") and (not _eid or _is_placeholder(_eid)):
        error_text = "❌ event_id некорректный — повторите запрос, я запрошу события автоматически."
        await add_message(user_id, "assistant", error_text)
        return error_text

    # Для task-операций разрешаем task_id по названию задачи
    if tool_name in ("complete_task", "delete_task", "update_task"):
        tool_args, err = await _resolve_task_id(tool_args)
        if err:
            await add_message(user_id, "assistant", err)
            return err

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
