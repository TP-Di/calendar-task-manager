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
    Сохраняет ключ в персистентные .env-файлы (best-effort оба, не падаем):
    1. data/runtime.env — основное хранилище (Docker volume / локальная FS)
    2. .env — переживает rebuild без volume (если bind-mounted на хост)

    Атомарно: tmp + os.replace. Thread-safe через _env_lock.
    Логирует результат каждого таргета — для диагностики см. bot_logs.
    """
    # Sanitize: strip newlines, escape backslashes then double-quotes
    safe = value.replace("\n", "").replace("\r", "")
    safe = safe.replace("\\", "\\\\").replace('"', '\\"')
    new_line = f'{key}="{safe}"\n'

    targets = [os.path.join("data", "runtime.env"), ".env"]
    results = _write_env_targets(key, new_line, targets)
    summary = ", ".join(f"{t}={'✓' if ok else '✗'}" for t, ok in results.items())
    logger.info("update_env_file %s → %s", key, summary)


def _write_env_targets(key: str, new_line: str, targets: list[str]) -> dict[str, bool]:
    """Записывает new_line во все targets. Возвращает {path: success}."""
    results: dict[str, bool] = {}
    with _env_lock:
        for target in targets:
            try:
                target_dir = os.path.dirname(target)
                if target_dir:
                    os.makedirs(target_dir, exist_ok=True)

                # Защита: если по пути директория (например, Docker создал её
                # вместо отсутствующего bind-mount source) — это не файл, пропускаем.
                if os.path.exists(target) and not os.path.isfile(target):
                    logger.warning("Пропуск %s: путь существует, но не файл", target)
                    results[target] = False
                    continue

                lines: list[str] = []
                if os.path.isfile(target):
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
                results[target] = True
            except Exception as e:
                logger.warning("Не удалось записать %s: %s", target, e)
                results[target] = False
    return results


def env_persistence_status() -> dict[str, bool]:
    """Диагностика для /settings: возвращает {target: writable}."""
    status: dict[str, bool] = {}
    for target in [os.path.join("data", "runtime.env"), ".env"]:
        try:
            target_dir = os.path.dirname(target) or "."
            # writable, если файл существует и доступен на запись, ИЛИ
            # директория существует и доступна на запись (можем создать файл).
            if os.path.isfile(target):
                status[target] = os.access(target, os.W_OK)
            elif os.path.isdir(target):
                # Это плохо — но честно показываем что это не файл
                status[target] = False
            else:
                status[target] = os.path.isdir(target_dir) and os.access(target_dir, os.W_OK)
        except Exception:
            status[target] = False
    return status


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

# Per-user flow + URL pairs. URL стабилен между повторными вызовами /reauth,
# чтобы не было state mismatch если пользователь дважды нажал кнопку.
# Запись чистится через _AUTH_FLOW_TTL секунд.
_AUTH_FLOW_TTL = 900.0  # 15 мин
_pending_auth_flows: dict[int, tuple[InstalledAppFlow, str, float]] = {}


def get_auth_url(user_id: int) -> str:
    """
    Генерирует OAuth URL для повторной авторизации конкретного пользователя.

    Идемпотентен: повторные вызовы в пределах _AUTH_FLOW_TTL возвращают тот же URL
    (тот же state) — иначе пользователь, нажавший /reauth дважды, мог получить
    state mismatch на /auth_code от первой ссылки.

    Использует http://localhost как redirect_uri — Google прекратил поддержку
    OOB-flow (urn:ietf:wg:oauth:2.0:oob) в октябре 2022. После consent браузер
    перенаправит на localhost (страница не откроется, но URL содержит ?code=...).
    """
    import time as _time
    now = _time.monotonic()

    # Если есть активный (не просроченный) flow — возвращаем его URL
    entry = _pending_auth_flows.get(user_id)
    if entry is not None:
        flow, url, started = entry
        if now - started < _AUTH_FLOW_TTL:
            return url
        # Просрочен — пересоздаём
        _pending_auth_flows.pop(user_id, None)

    client_config = json.loads(config.GOOGLE_CREDENTIALS_JSON)
    flow = InstalledAppFlow.from_client_config(
        client_config, SCOPES, redirect_uri="http://localhost"
    )
    auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent")
    _pending_auth_flows[user_id] = (flow, auth_url, now)
    return auth_url


def cancel_auth(user_id: int) -> bool:
    """Отменяет ожидающий OAuth flow. Возвращает True если был активный."""
    return _pending_auth_flows.pop(user_id, None) is not None


def complete_auth(code: str, user_id: int) -> None:
    """
    Завершает OAuth flow, принимая код авторизации от пользователя.
    Принимает как чистый код, так и полный URL вида http://localhost/?code=XXX&...

    Сохраняет новый токен в файл и обновляет in-memory кэш.
    """
    global _credentials_cache
    entry = _pending_auth_flows.pop(user_id, None)
    if entry is None:
        raise RuntimeError(
            "Нет активного auth-сеанса. Сначала вызови /reauth, "
            "перейди по ссылке и потом пришли URL обратно."
        )
    flow, _url, _started = entry

    # Если пользователь вставил полный URL — извлекаем code из query
    code = code.strip()
    if code.startswith("http://") or code.startswith("https://"):
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(code)
        codes = parse_qs(parsed.query).get("code", [])
        if not codes:
            raise RuntimeError("В URL нет параметра ?code=...")
        code = codes[0]

    flow.fetch_token(code=code)
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
