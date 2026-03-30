"""
Сервис Google Calendar API — чтение и запись событий.

Credentials передаются через переменную окружения GOOGLE_CREDENTIALS_JSON
(содержимое credentials.json в виде строки).
Токен хранится в GOOGLE_TOKEN_JSON (env) или в файле GOOGLE_TOKEN_PATH.
"""

import json
import logging
import os
from typing import Any

from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError
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


class TokenExpiredError(Exception):
    """Google OAuth токен истёк или был отозван. Требуется повторная авторизация."""


# In-memory кэш токена — обновляется после refresh или reauth, живёт весь процесс
_credentials_cache: "Credentials | None" = None


def _load_token() -> "Credentials | None":
    """
    Загружает OAuth2 токен.
    Приоритет: файл (может быть обновлён reauth) → GOOGLE_TOKEN_JSON (env, статичный).
    """
    # Файл приоритетнее — он обновляется при reauth внутри процесса
    if os.path.exists(config.GOOGLE_TOKEN_PATH):
        try:
            return Credentials.from_authorized_user_file(config.GOOGLE_TOKEN_PATH, SCOPES)
        except Exception as e:
            logger.warning("Не удалось загрузить токен из файла: %s", e)

    # Fallback: env-переменная (начальная настройка)
    if config.GOOGLE_TOKEN_JSON:
        try:
            return Credentials.from_authorized_user_info(
                json.loads(config.GOOGLE_TOKEN_JSON), SCOPES
            )
        except Exception as e:
            logger.warning("Не удалось загрузить токен из GOOGLE_TOKEN_JSON: %s", e)

    return None


def _save_token(creds: "Credentials") -> None:
    """
    Сохраняет токен:
    1. В файл GOOGLE_TOKEN_PATH
    2. Обновляет GOOGLE_TOKEN_JSON в .env (если файл существует)
    3. Обновляет config.GOOGLE_TOKEN_JSON в памяти
    """
    token_json = creds.to_json()

    # 1. Файл
    try:
        os.makedirs(os.path.dirname(config.GOOGLE_TOKEN_PATH), exist_ok=True)
        with open(config.GOOGLE_TOKEN_PATH, "w") as f:
            f.write(token_json)
        logger.debug("Токен сохранён в файл: %s", config.GOOGLE_TOKEN_PATH)
    except Exception as e:
        logger.error("Ошибка сохранения токена в файл: %s", e)

    # 2. .env файл — обновляем или добавляем GOOGLE_TOKEN_JSON
    _update_env_file("GOOGLE_TOKEN_JSON", token_json)

    # 3. Config в памяти — чтобы текущий процесс тоже видел новый токен
    config.GOOGLE_TOKEN_JSON = token_json


def _update_env_file(key: str, value: str) -> None:
    """Обновляет или добавляет переменную в .env файл."""
    # Ищем .env рядом с корнем проекта (bot/.env или на уровень выше)
    candidates = [
        os.path.join(os.path.dirname(__file__), "..", "..", ".env"),  # bot/.env
        os.path.join(os.path.dirname(__file__), "..", "..", "..", ".env"),  # ../.env
    ]
    env_path = next((p for p in candidates if os.path.exists(os.path.realpath(p))), None)

    if not env_path:
        logger.debug("Файл .env не найден, пропускаем обновление %s", key)
        return

    env_path = os.path.realpath(env_path)

    try:
        with open(env_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        # Экранируем значение — токен JSON на одну строку без переносов
        safe_value = value.replace("\n", "").replace("\r", "")
        new_line = f"{key}={safe_value}\n"

        # Заменяем существующую строку или добавляем в конец
        replaced = False
        for i, line in enumerate(lines):
            if line.startswith(f"{key}=") or line.startswith(f"{key} ="):
                lines[i] = new_line
                replaced = True
                break

        if not replaced:
            lines.append(new_line)

        with open(env_path, "w", encoding="utf-8") as f:
            f.writelines(lines)

        logger.info("Обновлён %s в %s", key, env_path)
    except Exception as e:
        logger.error("Ошибка обновления .env файла: %s", e)


def _get_credentials() -> "Credentials":
    """Возвращает действующий OAuth2 токен. Использует in-memory кэш."""
    global _credentials_cache

    if not config.GOOGLE_CREDENTIALS_JSON:
        raise RuntimeError(
            "GOOGLE_CREDENTIALS_JSON не задан. "
            "Вставьте содержимое credentials.json в переменную окружения."
        )

    # Кэш валиден — используем сразу
    if _credentials_cache and _credentials_cache.valid:
        return _credentials_cache

    # Кэш протух — пробуем refresh в памяти
    if _credentials_cache and _credentials_cache.expired and _credentials_cache.refresh_token:
        try:
            _credentials_cache.refresh(Request())
            _save_token(_credentials_cache)
            return _credentials_cache
        except RefreshError as e:
            logger.error("Не удалось обновить кэшированный токен: %s", e)
            _credentials_cache = None
            raise TokenExpiredError() from e

    # Кэша нет — грузим из хранилища
    creds = _load_token()

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                _save_token(creds)
            except RefreshError as e:
                logger.error("Не удалось обновить токен: %s", e)
                raise TokenExpiredError() from e
        else:
            raise TokenExpiredError()

    _credentials_cache = creds
    return creds


# ---------- Повторная авторизация через Telegram ----------

_pending_auth_flow: InstalledAppFlow | None = None


def get_auth_url() -> str:
    """
    Генерирует OAuth URL для повторной авторизации.
    Сохраняет flow в _pending_auth_flow для последующего обмена кода на токен.
    """
    global _pending_auth_flow
    client_config = json.loads(config.GOOGLE_CREDENTIALS_JSON)
    flow = InstalledAppFlow.from_client_config(
        client_config, SCOPES, redirect_uri="urn:ietf:wg:oauth:2.0:oob"
    )
    auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent")
    _pending_auth_flow = flow
    return auth_url


def complete_auth(code: str) -> None:
    """
    Завершает OAuth flow, принимая код авторизации от пользователя.
    Сохраняет новый токен в файл и обновляет in-memory кэш.
    """
    global _pending_auth_flow, _credentials_cache
    if _pending_auth_flow is None:
        raise RuntimeError("Нет активного flow. Сначала вызови get_auth_url().")
    _pending_auth_flow.fetch_token(code=code.strip())
    creds = _pending_auth_flow.credentials
    _save_token(creds)
    _credentials_cache = creds          # ← сразу доступен без перезапуска
    _pending_auth_flow = None
    logger.info("OAuth повторная авторизация выполнена успешно.")


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
    recurrence: list[str] | None = None,
    reminder_minutes: int | None = None,
) -> dict:
    """Создаёт событие в основном календаре (поддерживает RRULE и кастомные напоминания)."""
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
                "start": {"dateTime": start, "timeZone": config.TIMEZONE},
                "end": {"dateTime": end, "timeZone": config.TIMEZONE},
            }
            if recurrence:
                body["recurrence"] = recurrence
            if reminder_minutes is not None:
                body["reminders"] = {
                    "useDefault": False,
                    "overrides": [{"method": "popup", "minutes": int(reminder_minutes)}],
                }
            logger.debug("create_event body: %s", body)
            event = service.events().insert(calendarId="primary", body=body).execute()
            logger.info("Создано событие: %s (%s)", title, event.get("id"))
            return _format_event(event)
        except HttpError as e:
            logger.error("Ошибка Calendar API (create_event): %s", e)
            raise

    return await asyncio.to_thread(_create)


async def bulk_create_events(events: list[dict]) -> list[dict]:
    """Создаёт несколько событий. Автоматически группирует еженедельные
    повторения в одно recurring-событие с RRULE вместо N отдельных."""
    from datetime import datetime as _dt

    def _time_of_day(iso: str) -> str:
        """Возвращает HH:MM из ISO-строки."""
        try:
            return iso.split("T")[1][:5]
        except Exception:
            return iso

    # Если событие уже имеет recurrence — не трогаем
    plain, with_rrule = [], []
    for ev in events:
        if ev.get("recurrence"):
            with_rrule.append(ev)
        else:
            plain.append(ev)

    # Группируем plain-события по (title, start_time, end_time, description, tag)
    groups: dict[tuple, list[dict]] = {}
    for ev in plain:
        key = (
            ev["title"],
            _time_of_day(ev["start"]),
            _time_of_day(ev["end"]),
            ev.get("description", ""),
            ev.get("tag", ""),
        )
        groups.setdefault(key, []).append(ev)

    collapsed: list[dict] = list(with_rrule)
    for group in groups.values():
        if len(group) < 2:
            collapsed.extend(group)
            continue

        # Сортируем по дате начала
        group.sort(key=lambda e: e["start"])

        # Проверяем, все ли интервалы ровно 7 дней
        all_weekly = True
        for i in range(1, len(group)):
            try:
                d1 = _dt.fromisoformat(group[i - 1]["start"].replace("Z", "+00:00"))
                d2 = _dt.fromisoformat(group[i]["start"].replace("Z", "+00:00"))
                if (d2 - d1).days != 7:
                    all_weekly = False
                    break
            except Exception:
                all_weekly = False
                break

        if all_weekly:
            first = dict(group[0])
            first["recurrence"] = [f"RRULE:FREQ=WEEKLY;COUNT={len(group)}"]
            logger.info(
                "Авто-RRULE: '%s' ×%d → одно повторяющееся событие",
                first["title"], len(group),
            )
            collapsed.append(first)
        else:
            collapsed.extend(group)

    results = []
    for ev in collapsed:
        result = await create_event(
            ev["title"],
            ev["start"],
            ev["end"],
            ev.get("description", ""),
            ev.get("tag", ""),
            ev.get("recurrence"),
            ev.get("reminder_minutes"),
        )
        results.append(result)
    return results


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
                event["start"] = {"dateTime": fields["start"], "timeZone": config.TIMEZONE}
            if "end" in fields:
                event["end"] = {"dateTime": fields["end"], "timeZone": config.TIMEZONE}

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


async def find_events_by_title(title: str, date_from: str, date_to: str) -> list[dict]:
    """
    Ищет будущие события, у которых summary точно совпадает с title.
    Использует параметр q (полнотекстовый поиск) для предфильтрации.
    """
    import asyncio

    def _fetch():
        try:
            service = _build_service()
            time_min = _to_utc_iso(date_from)
            time_max = _to_utc_iso(date_to)
            result = (
                service.events()
                .list(
                    calendarId="primary",
                    q=title,
                    timeMin=time_min,
                    timeMax=time_max,
                    singleEvents=True,
                    orderBy="startTime",
                    maxResults=50,
                )
                .execute()
            )
            items = result.get("items", [])
            return [
                _format_event(e) for e in items
                if e.get("summary", "") == title
            ]
        except HttpError as e:
            logger.error("Ошибка Calendar API (find_events_by_title): %s", e)
            raise

    return await asyncio.to_thread(_fetch)


def _to_utc_iso(dt_str: str) -> str:
    """
    Приводит строку даты к формату UTC ISO 8601 с Z на конце.
    Naive строки (без timezone) считаются локальным временем пользователя
    (config.TIMEZONE) и конвертируются в UTC.
    """
    import zoneinfo
    from datetime import datetime, timezone

    # Уже содержит timezone — возвращаем как есть
    if dt_str.endswith("Z") or "+" in dt_str[10:]:
        return dt_str

    if len(dt_str) == 19:  # YYYY-MM-DDTHH:MM:SS
        try:
            tz = zoneinfo.ZoneInfo(config.TIMEZONE)
            local_dt = datetime.fromisoformat(dt_str).replace(tzinfo=tz)
            return local_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            return dt_str + "Z"  # fallback: старое поведение

    return dt_str
