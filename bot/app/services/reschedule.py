"""
Алгоритм перепланирования конфликтующих событий.
Без LLM — только теги и временные слоты.
"""
from datetime import datetime, timedelta, timezone as _tz
from typing import Optional

ONE_HOUR = timedelta(hours=1)


def _parse(s: str) -> datetime:
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return datetime.fromisoformat(s[:19]).replace(tzinfo=_tz.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def is_hard(event: dict) -> bool:
    text = (event.get("description") or "").upper()
    return "[HARD]" in text


def _overlaps(s1: datetime, e1: datetime, s2: datetime, e2: datetime) -> bool:
    return s1 < e2 and e1 > s2


def _compute_action(
    event: dict,
    ev_s: datetime,
    ev_e: datetime,
    blk_s: datetime,
    blk_e: datetime,
) -> Optional[dict]:
    """Вычисляет действие для одного конфликтующего события."""
    dur = ev_e - ev_s

    # Тип A: событие полностью внутри блока → целиком сдвинуть после блока
    if ev_s >= blk_s and ev_e <= blk_e:
        return {"type": "update", "event": event,
                "new_start": _iso(blk_e), "new_end": _iso(blk_e + dur)}

    # Тип B: блок "откусывает" начало события
    if blk_s <= ev_s and blk_e < ev_e:
        remaining = ev_e - blk_e
        if remaining < ONE_HOUR:
            return {"type": "update", "event": event,
                    "new_start": _iso(blk_e), "new_end": _iso(blk_e + dur)}
        return {"type": "update", "event": event,
                "new_start": _iso(blk_e), "new_end": _iso(ev_e)}

    # Тип C: блок "откусывает" конец события
    if ev_s < blk_s and ev_e <= blk_e:
        remaining = blk_s - ev_s
        if remaining < ONE_HOUR:
            return {"type": "update", "event": event,
                    "new_start": _iso(blk_e), "new_end": _iso(blk_e + dur)}
        return {"type": "update", "event": event,
                "new_start": _iso(ev_s), "new_end": _iso(blk_s)}

    # Тип D: блок внутри события → разрезаем пополам
    if ev_s < blk_s and ev_e > blk_e:
        part1 = blk_s - ev_s
        part2 = ev_e - blk_e
        if part1 >= ONE_HOUR and part2 >= ONE_HOUR:
            return {"type": "split", "event": event,
                    "part1_start": _iso(ev_s), "part1_end": _iso(blk_s),
                    "part2_start": _iso(blk_e), "part2_end": _iso(ev_e)}
        if part1 >= ONE_HOUR:
            return {"type": "update", "event": event,
                    "new_start": _iso(ev_s), "new_end": _iso(blk_s)}
        if part2 >= ONE_HOUR:
            return {"type": "update", "event": event,
                    "new_start": _iso(blk_e), "new_end": _iso(ev_e)}
        # Обе части < 1ч — двигаем целиком
        return {"type": "update", "event": event,
                "new_start": _iso(blk_e), "new_end": _iso(blk_e + dur)}

    return None


def compute_reschedule(
    new_start_iso: str,
    new_end_iso: str,
    all_events: list[dict],
    max_cascade: int = 8,
) -> list[dict]:
    """
    Вычисляет список действий для разрешения конфликтов.
    Каскадирует до max_cascade уровней.

    Каждое действие:
      {"type": "update", "event": {...}, "new_start": ISO, "new_end": ISO}
      {"type": "split",  "event": {...},
       "part1_start": ISO, "part1_end": ISO,
       "part2_start": ISO, "part2_end": ISO}
    """
    # Текущее состояние событий (мутируем start/end при сдвиге)
    states: list[dict] = []
    for ev in all_events:
        try:
            states.append({
                "event": ev,
                "start": _parse(ev["start"]),
                "end": _parse(ev["end"]),
            })
        except Exception:
            pass

    actions: list[dict] = []
    processed_ids: set[str] = set()

    # Очередь блоков, которые нужно "защитить"
    queue: list[tuple[datetime, datetime]] = [
        (_parse(new_start_iso), _parse(new_end_iso))
    ]

    for _ in range(max_cascade):
        if not queue:
            break
        blk_s, blk_e = queue.pop(0)

        for st in list(states):
            ev = st["event"]
            ev_id = ev.get("id") or ev.get("title", "")
            if ev_id in processed_ids:
                continue
            if is_hard(ev):
                continue
            if not _overlaps(st["start"], st["end"], blk_s, blk_e):
                continue

            action = _compute_action(ev, st["start"], st["end"], blk_s, blk_e)
            if not action:
                continue

            processed_ids.add(ev_id)
            actions.append(action)

            if action["type"] == "update":
                # Обновляем состояние и добавляем каскадную проверку
                new_s = _parse(action["new_start"])
                new_e = _parse(action["new_end"])
                st["start"] = new_s
                st["end"] = new_e
                queue.append((new_s, new_e))
            elif action["type"] == "split":
                # split не двигает ни одну часть в новое место → каскад не нужен
                states.remove(st)

    return actions
