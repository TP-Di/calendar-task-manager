"""
Автоматический сценарный тест агента без Telegram.
Запуск: python -X utf8 test_auto.py

Сценарий:
  1. Очистить историю
  2. Отправить расписание университета → показать tool call → подтвердить
  3. [пауза 65 сек — лимит Groq]
  4. Показать события на текущую неделю (get_events, без подтверждения)
  5. [пауза 65 сек]
  6. Удалить конкретное занятие через диалог (get_events → delete_event) → отменить
  7. [пауза 65 сек]
  8. Перенести занятие на другое время → подтвердить (реальная запись в Calendar)
"""

import asyncio
import json
import os
import sys
import time

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, os.path.dirname(__file__))

from app.db.database import init_db, clear_history
from app.services.agent import run_agent, execute_pending_tool

TEST_USER_ID = 999_999

CYAN   = "\033[96m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
RESET  = "\033[0m"


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def sep(title: str = "", color: str = CYAN) -> None:
    w = 72
    line = "─" * w
    print(f"\n{color}{BOLD}{line}{RESET}")
    if title:
        print(f"{color}{BOLD}  {title}{RESET}")
        print(f"{color}{BOLD}{line}{RESET}")


def fmt_json(obj) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False, indent=2)
    except Exception:
        return str(obj)


def wait(seconds: int, reason: str = "Groq rate limit") -> None:
    sep(f"Пауза {seconds}с ({reason})", DIM)
    for i in range(seconds, 0, -5):
        print(f"  {DIM}...{i}с{RESET}", end="\r", flush=True)
        time.sleep(min(5, i))
    print(" " * 30, end="\r")


async def send(msg: str, auto_confirm: bool = False, expect_no_tool: bool = False) -> str:
    """
    Отправляет сообщение агенту.
    auto_confirm=True  → автоматически подтверждает pending tool
    auto_confirm=False → отображает детали и спрашивает [y/n]
    """
    print(f"\n{BOLD}>>> {msg}{RESET}")
    print(f"{DIM}  Вызов run_agent...{RESET}")

    response = await run_agent(TEST_USER_ID, msg)

    if not response.startswith("PENDING_TOOL::"):
        sep("ОТВЕТ АГЕНТА", CYAN)
        print(response)
        return response

    # ── Разбираем pending ──────────────────────────────────────────────────
    pending = json.loads(response[len("PENDING_TOOL::"):])
    tool_name = pending["tool_name"]
    tool_args = pending["tool_args"]

    sep(f"TOOL CALL: {tool_name}", YELLOW)
    print(f"{YELLOW}Аргументы:{RESET}")

    if tool_name == "bulk_create_events":
        events = tool_args.get("events", [])
        print(f"  Всего событий: {len(events)}")
        for i, ev in enumerate(events, 1):
            rrule = ev.get("recurrence", ["—"])[0] if ev.get("recurrence") else "—"
            rem   = ev.get("reminder_minutes", "—")
            print(
                f"  {i:2}. {ev.get('title','?'):6s}  "
                f"start={ev.get('start','')[:16]}  "
                f"end={ev.get('end','')[11:16]}  "
                f"desc={ev.get('description',''):6s}  "
                f"tag={ev.get('tag',''):22s}  "
                f"{rrule}  reminder={rem}m"
            )
    elif tool_name in ("delete_event", "update_event"):
        print(f"  event_id    = {tool_args.get('event_id','?')}")
        print(f"  event_title = {tool_args.get('event_title', '(не передан)')}")
        print(f"  event_start = {tool_args.get('event_start', '(не передан)')}")
        if tool_name == "update_event":
            print(f"  fields      = {fmt_json(tool_args.get('fields', {}))}")
    else:
        print(fmt_json(tool_args))

    # ── Подтверждение ─────────────────────────────────────────────────────
    if auto_confirm:
        print(f"\n{GREEN}  [AUTO] Подтверждаю...{RESET}")
        result = await execute_pending_tool(pending)
        sep("РЕЗУЛЬТАТ", GREEN)
        print(result)
        return result
    else:
        ans = input(f"\n  {BOLD}Подтвердить? [y/n]{RESET} ").strip().lower()
        if ans == "y":
            print(f"{CYAN}  Выполняю...{RESET}")
            result = await execute_pending_tool(pending)
            sep("РЕЗУЛЬТАТ", GREEN)
            print(result)
            return result
        else:
            sep("ОТМЕНЕНО", RED)
            print(f"{RED}  Действие отменено.{RESET}")
            return "cancelled"


# ──────────────────────────────────────────────────────────────────────────────
# Сценарии
# ──────────────────────────────────────────────────────────────────────────────

SCHEDULE_MSG = """Вот мое расписание, актуально с 23 марта до 15 мая (просто сделай 9 повторений) этого года
Университет

MON: CD B101 9:30 - 11:00
TUE: CD B209 9:30 - 11:00, MA B101 11:00 - 12:30
WED: IoT B101 9:30 - 11:00
FRI: IoT B101 9:30 - 11:00, MA B101 11:00 - 12:30, EE B101 15:00 - 17:00"""


async def scenario_1_schedule() -> None:
    sep("СЦЕНАРИЙ 1: Импорт расписания университета", GREEN)
    print(f"Автоподтверждение: {YELLOW}ДА{RESET} (реальная запись в Google Calendar)")
    await send(SCHEDULE_MSG, auto_confirm=True)


async def scenario_2_view_week() -> None:
    sep("СЦЕНАРИЙ 2: Просмотр расписания текущей недели", GREEN)
    print(f"Автоподтверждение: не нужно (read-only)")
    await send("Покажи моё расписание на эту неделю", auto_confirm=False, expect_no_tool=True)


async def scenario_3_delete_cancel() -> None:
    sep("СЦЕНАРИЙ 3: Удалить занятие CD в понедельник — ОТМЕНИТЬ", GREEN)
    print(f"Автоподтверждение: {RED}НЕТ{RESET} (тест отмены)")
    result = await send("Удали занятие CD в следующий понедельник", auto_confirm=False)
    if result == "cancelled":
        print(f"{GREEN}  OK: отмена работает корректно.{RESET}")


async def scenario_4_reschedule() -> None:
    sep("СЦЕНАРИЙ 4: Перенести EE пятница с 15:00 на 16:00 — ПОДТВЕРДИТЬ", GREEN)
    print(f"Автоподтверждение: интерактивно")
    await send(
        "Перенеси EE в ближайшую пятницу — сдвинь начало на 16:00, конец на 18:00",
        auto_confirm=False,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

async def main() -> None:
    await init_db()

    sep("АВТОТЕСТ АГЕНТА  (bot/test_auto.py)", GREEN)
    print(f"  user_id   = {TEST_USER_ID}")
    print(f"  Модель    = llama-3.3-70b-versatile (Groq)")
    print(f"  Calendar  = Google Calendar (реальные вызовы)")
    print(f"  Пауза     = 65с между запросами к Groq\n")

    # Очищаем историю чтобы тест был чистым
    await clear_history(TEST_USER_ID)
    print(f"{DIM}  История диалога очищена.{RESET}")

    # ── Сценарий 1: расписание ─────────────────────────────────────────────
    await scenario_1_schedule()

    wait(65)

    # ── Сценарий 2: просмотр ─────────────────────────────────────────────
    await scenario_2_view_week()

    wait(65)

    # ── Сценарий 3: удалить + отменить ────────────────────────────────────
    await scenario_3_delete_cancel()

    wait(65)

    # ── Сценарий 4: редактировать дату ────────────────────────────────────
    await scenario_4_reschedule()

    sep("ТЕСТ ЗАВЕРШЁН", GREEN)


if __name__ == "__main__":
    asyncio.run(main())
