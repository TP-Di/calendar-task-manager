"""
CLI-тестер агента без Telegram.
Запуск: cd bot && python test_cli.py

Позволяет:
  - Отправлять сообщения агенту
  - Видеть сырой tool_call JSON
  - Подтверждать / отменять действия (y/n)
  - Реально записывать в Google Calendar
"""

import asyncio
import json
import os
import sys

# UTF-8 на Windows (иначе кириллица и спецсимволы ломают cp1251)
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
if sys.stdin.encoding and sys.stdin.encoding.lower() != "utf-8":
    sys.stdin.reconfigure(encoding="utf-8")

# Чтобы импорты работали из любого места
sys.path.insert(0, os.path.dirname(__file__))

from app.db.database import init_db, clear_history
from app.services.agent import run_agent, execute_pending_tool

TEST_USER_ID = 999_999  # фиктивный user_id, не пересекается с реальным

CYAN  = "\033[96m"
GREEN = "\033[92m"
YELLOW= "\033[93m"
RED   = "\033[91m"
BOLD  = "\033[1m"
RESET = "\033[0m"

def banner(text: str, color: str = CYAN) -> None:
    width = 70
    print(f"\n{color}{BOLD}{'─'*width}{RESET}")
    print(f"{color}{BOLD}  {text}{RESET}")
    print(f"{color}{BOLD}{'─'*width}{RESET}")

def fmt_json(obj) -> str:
    """Pretty-print dict/list."""
    try:
        return json.dumps(obj, ensure_ascii=False, indent=2)
    except Exception:
        return str(obj)

async def handle_pending(response: str) -> str:
    """
    Разбирает PENDING_TOOL, показывает детали и спрашивает y/n.
    Возвращает результат или сообщение об отмене.
    """
    json_str = response[len("PENDING_TOOL::"):]
    try:
        pending = json.loads(json_str)
    except json.JSONDecodeError as e:
        return f"{RED}❌ Не удалось разобрать PENDING_TOOL: {e}{RESET}"

    tool_name = pending.get("tool_name", "?")
    tool_args = pending.get("tool_args", {})

    banner(f"TOOL CALL: {tool_name}", YELLOW)

    # Показываем аргументы красиво
    print(f"{YELLOW}Аргументы:{RESET}")
    print(fmt_json(tool_args))

    # Для bulk_create показываем сводку
    if tool_name == "bulk_create_events":
        events = tool_args.get("events", [])
        print(f"\n{YELLOW}Итого событий: {len(events)}{RESET}")
        for i, ev in enumerate(events, 1):
            rrule = ev.get("recurrence", ["—"])[0] if ev.get("recurrence") else "—"
            reminder = ev.get("reminder_minutes", "—")
            print(
                f"  {i}. {ev.get('title','?'):10s} "
                f"{ev.get('start','')[:16]}  "
                f"→ {ev.get('end','')[11:16]}  "
                f"desc={ev.get('description',''):6s}  "
                f"tag={ev.get('tag',''):20s}  "
                f"rrule={rrule}  🔔{reminder}м"
            )

    # Подтверждение
    print(f"\n{BOLD}Выполнить? [y/n/inspect]{RESET} ", end="", flush=True)
    ans = input().strip().lower()

    if ans == "inspect":
        print(f"\n{CYAN}Полный pending JSON:{RESET}")
        # Не печатаем messages (слишком длинно), только ключевые поля
        safe = {k: v for k, v in pending.items() if k != "messages"}
        print(fmt_json(safe))
        print(f"\n{BOLD}Выполнить? [y/n]{RESET} ", end="", flush=True)
        ans = input().strip().lower()

    if ans != "y":
        return f"{RED}❌ Отменено пользователем.{RESET}"

    print(f"{CYAN}⏳ Выполняю...{RESET}")
    try:
        result = await execute_pending_tool(pending)
        return f"{GREEN}{result}{RESET}"
    except Exception as e:
        return f"{RED}❌ Ошибка при выполнении: {e}{RESET}"


async def chat_loop() -> None:
    await init_db()

    banner("CLI ТЕСТ АГЕНТА (без Telegram)", GREEN)
    print(f"user_id={TEST_USER_ID}  |  /clear — сброс истории  |  /quit — выход")
    print(f"Реальные вызовы Google Calendar ВКЛЮЧЕНЫ.\n")

    while True:
        try:
            print(f"{BOLD}You> {RESET}", end="", flush=True)
            user_input = input().strip()
        except (EOFError, KeyboardInterrupt):
            print("\nВыход.")
            break

        if not user_input:
            continue

        if user_input == "/quit":
            break

        if user_input == "/clear":
            await clear_history(TEST_USER_ID)
            print(f"{GREEN}История очищена.{RESET}")
            continue

        print(f"{CYAN}🤔 Думаю...{RESET}")
        try:
            response = await run_agent(TEST_USER_ID, user_input)
        except Exception as e:
            print(f"{RED}❌ Ошибка агента: {e}{RESET}")
            continue

        if response.startswith("PENDING_TOOL::"):
            result = await handle_pending(response)
            banner("РЕЗУЛЬТАТ", GREEN)
            print(result)
        else:
            banner("ОТВЕТ АГЕНТА", CYAN)
            print(response)

        print()  # пустая строка между턴ами


if __name__ == "__main__":
    asyncio.run(chat_loop())
