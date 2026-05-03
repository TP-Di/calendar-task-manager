"""
Сервис Google Tasks API — управление задачами.
Использует тот же OAuth2 токен, что и calendar.py.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.config import config
from app.services.calendar import _get_credentials, _google_api_executor, _google_run

logger = logging.getLogger(__name__)

_DEFAULT_TASKLIST = "@default"
_WRITABLE_FIELDS = {"id", "title", "status", "due", "notes", "completed", "parent", "position"}


def _clean_task_body(task: dict) -> dict:
    """Убирает read-only поля из тела задачи перед отправкой update."""
    return {k: v for k, v in task.items() if k in _WRITABLE_FIELDS}


def _build_tasks_service():
    """Создаёт сервис Google Tasks."""
    creds = _get_credentials()
    return build("tasks", "v1", credentials=creds)


def _format_task(task: dict) -> dict:
    """Приводит задачу Google Tasks к удобному формату."""
    return {
        "id": task.get("id", ""),
        "title": task.get("title", "(без названия)"),
        "status": task.get("status", "needsAction"),
        "due": task.get("due", ""),
        "description": task.get("notes", ""),
        "completed": task.get("completed", ""),
    }


async def get_tasks() -> list[dict]:
    """Получает активные (незавершённые) задачи из Google Tasks."""
    def _fetch():
        service = _build_tasks_service()
        result = (
            service.tasks()
            .list(
                tasklist=_DEFAULT_TASKLIST,
                showCompleted=False,
                showHidden=False,
                maxResults=100,
            )
            .execute()
        )
        items = result.get("items", [])
        return [
            _format_task(t) for t in items
            if t.get("status") == "needsAction"
        ]

    return await _google_run(_fetch)


async def create_task(
    title: str, due: str = "", description: str = "",
    start_time: str = "", end_time: str = "",
) -> dict:
    """Создаёт задачу в Google Tasks. start_time/end_time хранятся в notes."""
    def _create():
        service = _build_tasks_service()
        body: dict[str, Any] = {"title": title}

        notes_parts = []
        if start_time:
            block = f"⏰ {start_time}"
            if end_time:
                block += f" – {end_time}"
            notes_parts.append(block)
        if description:
            notes_parts.append(description)
        if notes_parts:
            body["notes"] = "\n".join(notes_parts)

        if due:
            body["due"] = due if due.endswith("Z") else due + "Z"

        task = (
            service.tasks()
            .insert(tasklist=_DEFAULT_TASKLIST, body=body)
            .execute()
        )
        logger.info("Создана задача: %s (%s)", title, task.get("id"))
        return _format_task(task)

    return await _google_run(_create)


async def complete_task(task_id: str) -> dict:
    """Отмечает задачу выполненной."""
    def _complete():
        service = _build_tasks_service()
        task = (
            service.tasks()
            .get(tasklist=_DEFAULT_TASKLIST, task=task_id)
            .execute()
        )
        task["status"] = "completed"
        updated = (
            service.tasks()
            .update(tasklist=_DEFAULT_TASKLIST, task=task_id, body=_clean_task_body(task))
            .execute()
        )
        logger.info("Задача выполнена: %s", task_id)
        return _format_task(updated)

    return await _google_run(_complete)


async def delete_task(task_id: str) -> dict:
    """Удаляет задачу из Google Tasks."""
    def _delete():
        service = _build_tasks_service()
        service.tasks().delete(tasklist=_DEFAULT_TASKLIST, task=task_id).execute()
        logger.info("Удалена задача: %s", task_id)
        return {"status": "deleted", "task_id": task_id}

    return await _google_run(_delete)


async def update_task(task_id: str, fields: dict) -> dict:
    """Обновляет поля задачи."""
    def _update():
        service = _build_tasks_service()
        task = (
            service.tasks()
            .get(tasklist=_DEFAULT_TASKLIST, task=task_id)
            .execute()
        )
        if "title" in fields:
            task["title"] = fields["title"]
        if "due" in fields:
            due = fields["due"]
            if not due.endswith("Z"):
                for sign in ("+", "-"):
                    idx = due.rfind(sign, 10)
                    if idx != -1:
                        due = due[:idx]
                        break
                due += "Z"
            task["due"] = due

        if "start_time" in fields or "end_time" in fields or "description" in fields:
            current_notes = task.get("notes", "")
            if current_notes.startswith("⏰ "):
                lines = current_notes.split("\n", 1)
                old_desc = lines[1] if len(lines) > 1 else ""
                block_part = lines[0][2:].strip()
                if " – " in block_part:
                    old_start, old_end = block_part.split(" – ", 1)
                else:
                    old_start, old_end = block_part, ""
            else:
                old_start, old_end, old_desc = "", "", current_notes

            new_start = fields.get("start_time", old_start)
            new_end   = fields.get("end_time",   old_end)
            new_desc  = fields.get("description", old_desc)

            notes_parts = []
            if new_start:
                block = f"⏰ {new_start}"
                if new_end:
                    block += f" – {new_end}"
                notes_parts.append(block)
            if new_desc:
                notes_parts.append(new_desc)
            task["notes"] = "\n".join(notes_parts)

        updated = (
            service.tasks()
            .update(tasklist=_DEFAULT_TASKLIST, task=task_id, body=_clean_task_body(task))
            .execute()
        )
        logger.info("Обновлена задача: %s", task_id)
        return _format_task(updated)

    return await _google_run(_update)


async def get_recently_completed_tasks(minutes: int = 20) -> list[dict]:
    """Возвращает задачи, выполненные за последние N минут."""
    def _fetch():
        service = _build_tasks_service()
        completed_min = (
            datetime.now(timezone.utc) - timedelta(minutes=minutes)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        result = (
            service.tasks()
            .list(
                tasklist=_DEFAULT_TASKLIST,
                showCompleted=True,
                showHidden=True,
                completedMin=completed_min,
                maxResults=50,
            )
            .execute()
        )
        items = result.get("items", [])
        return [_format_task(t) for t in items if t.get("status") == "completed"]

    return await _google_run(_fetch)
