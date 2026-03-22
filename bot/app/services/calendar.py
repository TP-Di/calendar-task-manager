"""
Сервис Google Calendar API — чтение и запись событий.
OAuth2 токен читается из GOOGLE_TOKEN_PATH, credentials из GOOGLE_CREDENTIALS_PATH.
"""

import logging
from datetime import datetime, timezone
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.config import config

logger = logging.getLogger(__name__)

# Права доступа к Calendar API
SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/tasks",
]


def _get_credentials() -> Credentials:
    """Возвращает действующий OAuth2 токен, при необходимости обновляет."""
    import os

    creds: Credentials | None = None

    if os.path.exists(config.GOOGLE_TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(config.GOOGLE_TOKEN_PATH, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                config.GOOGLE_CREDENTIALS_PATH, SCOPES
            )
            creds = flow.run_local_server(port=0)

        with open(config.GOOGLE_TOKEN_PATH, "w") as token_file:
            token_file.write(creds.to_json())

    return creds


def _build_service():
    """Создаёт сервис Google Calendar."""
    creds = _get_credentials()
    return build("calendar", "v3", credentials=creds)


def _format_event(event: dict) -> dict:
    """Приводит событие Google Calendar к удобному формату."""
    start = event.get("start", {})
    end = event.get("end", {})
    return {
        "id": event.get("id", ""),
        "title": event.get("summary", "(без названия)"),
        "start": start.get("dateTime", start.get("date", "")),
        "end": end.get("dateTime", end.get("date", "")),
        "description": event.get("description", ""),
        "location": event.get("location", ""),
    }


async def get_events(date_from: str, date_to: str) -> list[dict]:
    """
    Получает события из основного календаря за период [date_from, date_to].
    Форматы: ISO 8601 строки.
    """
    import asyncio

    def _fetch():
        try:
            service = _build_service()
            # Убеждаемся что строки в UTC формате
            time_min = _to_utc_iso(date_from)
            time_max = _to_utc_iso(date_to)

            result = (
                service.events()
                .list(
                    calendarId="primary",
                    timeMin=time_min,
                    timeMax=time_max,
                    singleEvents=True,
                    orderBy="startTime",
                    maxResults=50,
                )
                .execute()
            )
            items = result.get("items", [])
            return [_format_event(e) for e in items]
        except HttpError as e:
            logger.error("Ошибка Calendar API (get_events): %s", e)
            raise

    return await asyncio.to_thread(_fetch)


async def create_event(
    title: str,
    start: str,
    end: str,
    description: str = "",
    tag: str = "",
) -> dict:
    """Создаёт событие в основном календаре."""
    import asyncio

    # Добавляем тег в описание
    full_description = description
    if tag:
        full_description = f"[{tag}]\n{description}".strip()

    def _create():
        try:
            service = _build_service()
            body: dict[str, Any] = {
                "summary": title,
                "description": full_description,
                "start": {"dateTime": start, "timeZone": "UTC"},
                "end": {"dateTime": end, "timeZone": "UTC"},
            }
            event = service.events().insert(calendarId="primary", body=body).execute()
            logger.info("Создано событие: %s (%s)", title, event.get("id"))
            return _format_event(event)
        except HttpError as e:
            logger.error("Ошибка Calendar API (create_event): %s", e)
            raise

    return await asyncio.to_thread(_create)


async def update_event(event_id: str, fields: dict) -> dict:
    """Обновляет поля существующего события."""
    import asyncio

    def _update():
        try:
            service = _build_service()
            # Сначала получаем текущее событие
            event = service.events().get(calendarId="primary", eventId=event_id).execute()

            if "title" in fields:
                event["summary"] = fields["title"]
            if "description" in fields:
                event["description"] = fields["description"]
            if "start" in fields:
                event["start"] = {"dateTime": fields["start"], "timeZone": "UTC"}
            if "end" in fields:
                event["end"] = {"dateTime": fields["end"], "timeZone": "UTC"}

            updated = (
                service.events()
                .update(calendarId="primary", eventId=event_id, body=event)
                .execute()
            )
            logger.info("Обновлено событие: %s", event_id)
            return _format_event(updated)
        except HttpError as e:
            logger.error("Ошибка Calendar API (update_event): %s", e)
            raise

    return await asyncio.to_thread(_update)


async def delete_event(event_id: str) -> dict:
    """Удаляет событие из календаря."""
    import asyncio

    def _delete():
        try:
            service = _build_service()
            service.events().delete(calendarId="primary", eventId=event_id).execute()
            logger.info("Удалено событие: %s", event_id)
            return {"status": "deleted", "event_id": event_id}
        except HttpError as e:
            logger.error("Ошибка Calendar API (delete_event): %s", e)
            raise

    return await asyncio.to_thread(_delete)


def _to_utc_iso(dt_str: str) -> str:
    """Приводит строку даты к формату UTC ISO 8601 с Z на конце."""
    # Если уже заканчивается на Z или содержит +, возвращаем как есть
    if dt_str.endswith("Z") or "+" in dt_str[10:]:
        return dt_str
    # Добавляем Z если нет timezone info
    if len(dt_str) == 19:  # YYYY-MM-DDTHH:MM:SS
        return dt_str + "Z"
    return dt_str
