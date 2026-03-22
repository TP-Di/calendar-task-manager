"""
Сервис Google Tasks API — управление задачами.
Использует тот же OAuth2 токен, что и calendar.py.
"""

import asyncio
import logging
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.config import config
from app.services.calendar import _get_credentials

logger = logging.getLogger(__name__)

# ID стандартного списка задач
_DEFAULT_TASKLIST = "@default"


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
        try:
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
            return [_format_task(t) for t in items]
        except HttpError as e:
            logger.error("Ошибка Tasks API (get_tasks): %s", e)
            raise

    return await asyncio.to_thread(_fetch)


async def create_task(
    title: str, due: str = "", description: str = ""
) -> dict:
    """Создаёт задачу в Google Tasks."""

    def _create():
        try:
            service = _build_tasks_service()
            body: dict[str, Any] = {"title": title}
            if description:
                body["notes"] = description
            if due:
                # Google Tasks ожидает RFC 3339
                body["due"] = due if due.endswith("Z") else due + "Z"

            task = (
                service.tasks()
                .insert(tasklist=_DEFAULT_TASKLIST, body=body)
                .execute()
            )
            logger.info("Создана задача: %s (%s)", title, task.get("id"))
            return _format_task(task)
        except HttpError as e:
            logger.error("Ошибка Tasks API (create_task): %s", e)
            raise

    return await asyncio.to_thread(_create)


async def complete_task(task_id: str) -> dict:
    """Отмечает задачу выполненной."""

    def _complete():
        try:
            service = _build_tasks_service()
            task = (
                service.tasks()
                .get(tasklist=_DEFAULT_TASKLIST, task=task_id)
                .execute()
            )
            task["status"] = "completed"
            updated = (
                service.tasks()
                .update(tasklist=_DEFAULT_TASKLIST, task=task_id, body=task)
                .execute()
            )
            logger.info("Задача выполнена: %s", task_id)
            return _format_task(updated)
        except HttpError as e:
            logger.error("Ошибка Tasks API (complete_task): %s", e)
            raise

    return await asyncio.to_thread(_complete)


async def update_task(task_id: str, fields: dict) -> dict:
    """Обновляет поля задачи."""

    def _update():
        try:
            service = _build_tasks_service()
            task = (
                service.tasks()
                .get(tasklist=_DEFAULT_TASKLIST, task=task_id)
                .execute()
            )
            if "title" in fields:
                task["title"] = fields["title"]
            if "description" in fields:
                task["notes"] = fields["description"]
            if "due" in fields:
                due = fields["due"]
                task["due"] = due if due.endswith("Z") else due + "Z"

            updated = (
                service.tasks()
                .update(tasklist=_DEFAULT_TASKLIST, task=task_id, body=task)
                .execute()
            )
            logger.info("Обновлена задача: %s", task_id)
            return _format_task(updated)
        except HttpError as e:
            logger.error("Ошибка Tasks API (update_task): %s", e)
            raise

    return await asyncio.to_thread(_update)
