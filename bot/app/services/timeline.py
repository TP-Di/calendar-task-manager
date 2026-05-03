"""
Шерные хелперы для работы с расписанием:
- группировка событий по дням,
- поиск свободных окон,
- определение «сейчас идёт» / «следующее»,
- сводка по дню/неделе.

Отделено от calendar.py чтобы не цеплять google-api для текстовых вью.
"""

from datetime import date, datetime, timedelta
from typing import Iterable

from app.utils.datetime_helpers import parse_iso_dt


WEEKDAYS_SHORT_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def _to_local(iso: str, tz):
    """ISO-строка → datetime в указанном tz. None при ошибке."""
    try:
        return parse_iso_dt(iso).astimezone(tz)
    except Exception:
        return None


def event_duration_hours(event: dict, tz=None) -> float:
    """Длительность события в часах (0 если не парсится)."""
    s = event.get("start", "")
    e = event.get("end", "")
    if not s or not e:
        return 0.0
    try:
        sd = parse_iso_dt(s)
        ed = parse_iso_dt(e)
        if tz is not None:
            sd = sd.astimezone(tz)
            ed = ed.astimezone(tz)
        return max(0.0, (ed - sd).total_seconds() / 3600)
    except Exception:
        return 0.0


def group_events_by_day(events: list[dict], tz) -> list[tuple[date, list[dict]]]:
    """
    Группирует события по локальной дате начала.
    Возвращает отсортированный список [(date, [events])].
    Внутри дня события сортируются по start.
    """
    buckets: dict[date, list[dict]] = {}
    for ev in events:
        s = ev.get("start", "")
        sd = _to_local(s, tz)
        if sd is None:
            continue
        buckets.setdefault(sd.date(), []).append(ev)
    result = []
    for d in sorted(buckets.keys()):
        day_events = sorted(
            buckets[d],
            key=lambda e: _to_local(e.get("start", ""), tz) or datetime.min.replace(tzinfo=tz),
        )
        result.append((d, day_events))
    return result


def event_now(events: Iterable[dict], now: datetime) -> dict | None:
    """Возвращает событие, которое идёт прямо сейчас, или None."""
    for ev in events:
        s = ev.get("start", "")
        e = ev.get("end", "")
        try:
            sd = parse_iso_dt(s).astimezone(now.tzinfo)
            ed = parse_iso_dt(e).astimezone(now.tzinfo)
        except Exception:
            continue
        if sd <= now < ed:
            return ev
    return None


def next_event_after(
    events: Iterable[dict], now: datetime
) -> tuple[dict, timedelta] | None:
    """
    Возвращает ближайшее БУДУЩЕЕ событие и интервал до его начала.
    Если все прошли — None.
    """
    candidates: list[tuple[datetime, dict]] = []
    for ev in events:
        try:
            sd = parse_iso_dt(ev.get("start", "")).astimezone(now.tzinfo)
        except Exception:
            continue
        if sd > now:
            candidates.append((sd, ev))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    sd, ev = candidates[0]
    return ev, sd - now


def find_free_windows(
    events: list[dict],
    day_start: datetime,
    day_end: datetime,
    work_start_hour: int,
    work_end_hour: int,
    min_hours: float,
) -> list[tuple[datetime, datetime]]:
    """
    Возвращает свободные окна длительностью >= min_hours в пределах
    [day_start с рабочим часом work_start_hour .. day_end с work_end_hour].
    Всё в часовом поясе day_start.tzinfo.
    """
    tz = day_start.tzinfo
    bounds_start = day_start.replace(hour=work_start_hour, minute=0, second=0, microsecond=0)
    bounds_end = day_start.replace(hour=work_end_hour, minute=0, second=0, microsecond=0)
    if work_end_hour <= work_start_hour:
        # ночные смены — пропускаем для упрощения
        return []
    # Если день не сегодня и день_end раньше bounds_end, обрезаем
    if day_end < bounds_end:
        bounds_end = day_end
    if day_start > bounds_start:
        bounds_start = day_start

    # Собираем интервалы занятости внутри bounds
    busy: list[tuple[datetime, datetime]] = []
    for ev in events:
        try:
            sd = parse_iso_dt(ev.get("start", "")).astimezone(tz)
            ed = parse_iso_dt(ev.get("end", "")).astimezone(tz)
        except Exception:
            continue
        # Клипуем под bounds
        s = max(sd, bounds_start)
        e = min(ed, bounds_end)
        if s < e:
            busy.append((s, e))
    busy.sort(key=lambda x: x[0])
    # Объединяем пересекающиеся
    merged: list[tuple[datetime, datetime]] = []
    for s, e in busy:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))

    # Свободные = bounds минус merged
    windows: list[tuple[datetime, datetime]] = []
    cursor = bounds_start
    for s, e in merged:
        if s > cursor and (s - cursor).total_seconds() / 3600 >= min_hours:
            windows.append((cursor, s))
        cursor = max(cursor, e)
    if bounds_end > cursor and (bounds_end - cursor).total_seconds() / 3600 >= min_hours:
        windows.append((cursor, bounds_end))
    return windows


def day_label(d: date, today: date) -> str:
    """'Сегодня (04.05)' | 'Завтра (05.05)' | 'Сб 06.05'."""
    delta = (d - today).days
    if delta == 0:
        return f"Сегодня ({d.day:02d}.{d.month:02d})"
    if delta == 1:
        return f"Завтра ({d.day:02d}.{d.month:02d})"
    if delta == -1:
        return f"Вчера ({d.day:02d}.{d.month:02d})"
    return f"{WEEKDAYS_SHORT_RU[d.weekday()]} {d.day:02d}.{d.month:02d}"


def busy_hours(events: list[dict], tz) -> float:
    """Сумма длительностей событий (в часах)."""
    total = 0.0
    for ev in events:
        total += event_duration_hours(ev, tz)
    return total


def end_of_iso_week(today: date) -> date:
    """Возвращает дату ВС текущей ISO-недели."""
    # Monday=0..Sunday=6
    return today + timedelta(days=(6 - today.weekday()))


def end_of_iso_week_dt(now: datetime) -> datetime:
    """Конец ISO-недели (вс 23:59:59) в часовом поясе now."""
    sun = end_of_iso_week(now.date())
    return now.replace(year=sun.year, month=sun.month, day=sun.day,
                       hour=23, minute=59, second=59, microsecond=0)


def format_duration_short(hours: float) -> str:
    """1.5 → '1.5ч'; 0.5 → '30м'."""
    if hours >= 1.0:
        if abs(hours - round(hours)) < 0.05:
            return f"{round(hours)}ч"
        return f"{hours:.1f}ч"
    minutes = int(round(hours * 60))
    return f"{minutes}м"


def format_time_until(td: timedelta) -> str:
    """timedelta → 'через 1ч 15м' / 'через 5м' / 'через 2д 3ч'."""
    total_min = int(td.total_seconds() // 60)
    if total_min < 60:
        return f"через {total_min}м" if total_min > 0 else "сейчас"
    hours = total_min // 60
    mins = total_min % 60
    if hours < 24:
        return f"через {hours}ч {mins}м" if mins else f"через {hours}ч"
    days = hours // 24
    rem_hours = hours % 24
    return f"через {days}д {rem_hours}ч" if rem_hours else f"через {days}д"
