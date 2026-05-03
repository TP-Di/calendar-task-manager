"""
Сервис Google Calendar API — чтение и запись событий.

Credentials передаются через переменную окружения GOOGLE_CREDENTIALS_JSON
(содержимое credentials.json в виде строки).
Токен хранится в GOOGLE_TOKEN_JSON (env) или в файле GOOGLE_TOKEN_PATH.
"""

import asyncio
import concurrent.futures
import json
import logging
import os
import threading
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


# Bounded executor for all Google API sync calls — prevents unbounded thread growth
_google_api_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=10,
    thread_name_prefix="google_api",
)

# In-memory кэш токена — защищён threading.Lock (вызывается из executor-тредов)
_credentials_cache: "Credentials | None" = None
_credentials_lock = threading.Lock()

# Lock для записи в data/runtime.env — есть параллельные writer'ы:
#   - _save_token (executor-тред после refresh)
#   - settings._apply (event-loop, через asyncio.to_thread)
_env_lock = threading.Lock()


async def _google_run(fn, max_attempts: int = 3, base_delay: float = 1.0):
    """
    Runs sync fn in executor with async-sleep retry.
    - 429/500/503 → exponential backoff (asyncio.sleep, не блокирует event loop)
    - 401/403/410 → не ретраит (non-recoverable)
    """
    loop = asyncio.get_running_loop()
    for attempt in range(max_attempts):
        try:
            return await loop.run_in_executor(_google_api_executor, fn)
        except HttpError as exc:
            status = int(exc.resp.status) if hasattr(exc, "resp") else 0
            if status in (401, 403, 410):
                raise  # non-recoverable: токен отозван, доступ запрещён и т.п.
            if status in (429, 500, 503) and attempt < max_attempts - 1:
                delay = base_delay * (2 ** attempt)
                logger.warning(
                    "Google API HTTP %d — retry %d/%d in %.1fs",
                    status, attempt + 1, max_attempts - 1, delay,
                )
                await asyncio.sleep(delay)
            else:
                raise


def _load_token() -> "Credentials | None":
    """
    Загружает OAuth2 токен.
    Приоритет: файл (может быть обновлён reauth) → GOOGLE_TOKEN_JSON (env, статичный).
    """
    if os.path.exists(config.GOOGLE_TOKEN_PATH):
        try:
            return Credentials.from_authorized_user_file(config.GOOGLE_TOKEN_PATH, SCOPES)
        except Exception as e:
            logger.warning("Не удалось загрузить токен из файла: %s", e)

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
    2. Обновляет GOOGLE_TOKEN_JSON в runtime.env
    3. Обновляет config.GOOGLE_TOKEN_JSON в памяти
    """
    token_json = creds.to_json()

    try:
        os.makedirs(os.path.dirname(config.GOOGLE_TOKEN_PATH), exist_ok=True)
        with open(config.GOOGLE_TOKEN_PATH, "w") as f:
            f.write(token_json)
        logger.debug("Токен сохранён в файл: %s", config.GOOGLE_TOKEN_PATH)
    except Exception as e:
        logger.error("Ошибка сохранения токена в файл: %s", e)

    _update_env_file("GOOGLE_TOKEN_JSON", token_json)
    config.GOOGLE_TOKEN_JSON = token_json


def _update_env_file(key: str, value: str) -> None:
    """
    Сохраняет ключ в персистентные .env-файлы:
    1. data/runtime.env (Docker volume — основное хранилище)
    2. .env (если writable — переживает rebuild без volume)

    Атомарно: пишем во временный файл, потом os.replace.
    Thread-safe: серилизуем через _env_lock.
    """
    targets = [os.path.join("data", "runtime.env")]
    # .env в корне (опционально) — если writable, дублируем для переживания rebuild
    if os.path.exists(".env") and os.access(".env", os.W_OK):
        targets.append(".env")

    # Sanitize: strip newlines, escape backslashes then double-quotes
    safe = value.replace("\n", "").replace("\r", "")
    safe = safe.replace("\\", "\\\\").replace('"', '\\"')
    new_line = f'{key}="{safe}"\n'

    with _env_lock:
        for target in targets:
            try:
                target_dir = os.path.dirname(target)
                if target_dir:
                    os.makedirs(target_dir, exist_ok=True)

                lines: list[str] = []
                if os.path.exists(target):
                    with open(target, "r", encoding="utf-8") as f:
                        lines = f.readlines()

                replaced = False
                for i, line in enumerate(lines):
                    if line.startswith(f"{key}=") or line.startswith(f"{key} ="):
                        lines[i] = new_line
                        replaced = True
                        break
                if not replaced:
                    lines.append(new_line)

                tmp_path = target + ".tmp"
                with open(tmp_path, "w", encoding="utf-8") as f:
                    f.writelines(lines)
                os.replace(tmp_path, target)

                logger.info("Обновлён %s в %s", key, target)
            except Exception as e:
                logger.error("Ошибка обновления %s: %s", target, e)


def _get_credentials() -> "Credentials":
    """Возвращает действующий OAuth2 токен. Использует in-memory кэш.
    Thread-safe: вызывается из нескольких executor-тредов одновременно.
    """
    global _credentials_cache

    if not config.GOOGLE_CREDENTIALS_JSON:
        raise RuntimeError(
            "GOOGLE_CREDENTIALS_JSON не задан. "
            "Вставьте содержимое credentials.json в переменную окружения."
        )

    with _credentials_lock:
        if _credentials_cache and _credentials_cache.valid:
            return _credentials_cache

        if _credentials_cache and _credentials_cache.expired and _credentials_cache.refresh_token:
            try:
                _credentials_cache.refresh(Request())
                _save_token(_credentials_cache)
                return _credentials_cache
            except RefreshError as e:
                logger.error("Не удалось обновить кэшированный токен: %s", e)
                _credentials_cache = None
                raise TokenExpiredError() from e

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

# Per-user flows to prevent race conditions when multiple users reauth simultaneously
_pending_auth_flows: dict[int, InstalledAppFlow] = {}


def get_auth_url(user_id: int) -> str:
    """
    Генерирует OAuth URL для повторной авторизации конкретного пользователя.
    Сохраняет flow в _pending_auth_flows[user_id] для последующего обмена кода на токен.
    """
    client_config = json.loads(config.GOOGLE_CREDENTIALS_JSON)
    flow = InstalledAppFlow.from_client_config(
        client_config, SCOPES, redirect_uri="urn:ietf:wg:oauth:2.0:oob"
    )
    auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent")
    _pending_auth_flows[user_id] = flow
    return auth_url


def complete_auth(code: str, user_id: int) -> None:
    """
    Завершает OAuth flow, принимая код авторизации от пользователя.
    Сохраняет новый токен в файл и обновляет in-memory кэш.
    """
    global _credentials_cache
    flow = _pending_auth_flows.pop(user_id, None)
    if flow is None:
        raise RuntimeError("Нет активного flow. Сначала вызови get_auth_url().")
    flow.fetch_token(code=code.strip())
    creds = flow.credentials
    _save_token(creds)
    _credentials_cache = creds
    logger.info("OAuth повторная авторизация выполнена успешно для user_id=%d.", user_id)


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
    """Получает события из основного календаря за период [date_from, date_to]."""
    def _fetch():
        service = _build_service()
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

    return await _google_run(_fetch)


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
    full_description = description
    if tag:
        full_description = f"[{tag}]\n{description}".strip()

    def _create():
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

    return await _google_run(_create)


async def bulk_create_events(events: list[dict]) -> list[dict]:
    """Создаёт несколько событий. Группирует еженедельные повторения в RRULE."""
    from datetime import datetime as _dt

    def _time_of_day(iso: str) -> str:
        try:
            return iso.split("T")[1][:5]
        except Exception:
            return iso

    plain, with_rrule = [], []
    for ev in events:
        if ev.get("recurrence"):
            with_rrule.append(ev)
        else:
            plain.append(ev)

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

        group.sort(key=lambda e: e["start"])

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
    """Обновляет поля существующего события через PATCH (только изменённые поля)."""
    def _patch():
        service = _build_service()
        patch_body: dict = {}
        if "title" in fields:
            patch_body["summary"] = fields["title"]
        if "description" in fields:
            patch_body["description"] = fields["description"]
        if "start" in fields:
            patch_body["start"] = {"dateTime": fields["start"], "timeZone": config.TIMEZONE}
        if "end" in fields:
            patch_body["end"] = {"dateTime": fields["end"], "timeZone": config.TIMEZONE}

        updated = (
            service.events()
            .patch(calendarId="primary", eventId=event_id, body=patch_body)
            .execute()
        )
        logger.info("Обновлено событие: %s", event_id)
        return _format_event(updated)

    return await _google_run(_patch)


async def delete_event(event_id: str) -> dict:
    """Удаляет событие из календаря."""
    def _delete():
        service = _build_service()
        service.events().delete(calendarId="primary", eventId=event_id).execute()
        logger.info("Удалено событие: %s", event_id)
        return {"status": "deleted", "event_id": event_id}

    return await _google_run(_delete)


async def find_events_by_title(title: str, date_from: str, date_to: str) -> list[dict]:
    """Ищет будущие события с точным совпадением summary == title."""
    def _fetch():
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

    return await _google_run(_fetch)


def _to_utc_iso(dt_str: str) -> str:
    """
    Приводит строку даты к формату UTC ISO 8601 с Z на конце.
    Naive строки (без timezone) считаются локальным временем пользователя
    (config.TIMEZONE) и конвертируются в UTC.
    """
    import zoneinfo
    from datetime import datetime, timezone

    if dt_str.endswith("Z") or "+" in dt_str[10:]:
        return dt_str

    if len(dt_str) == 19:  # YYYY-MM-DDTHH:MM:SS
        try:
            tz = zoneinfo.ZoneInfo(config.TIMEZONE)
            local_dt = datetime.fromisoformat(dt_str).replace(tzinfo=tz)
            return local_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            return dt_str + "Z"

    return dt_str
