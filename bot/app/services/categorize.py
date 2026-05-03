"""
Единый классификатор событий и задач для текстовых вью и хитмапа.

Категория события определяется тегом [CATEGORY:x] в description.
Subject (для палитры внутри категории) — короткий префикс из заголовка
после удаления служебных тегов.
"""

import hashlib
import re
from datetime import datetime
from typing import Iterable

CATEGORY_RE = re.compile(r"\[CATEGORY:([^\]]+)\]", re.IGNORECASE)
TAG_STRIP_RE = re.compile(r"\[(HARD|SOFT|PRIORITY:[^\]]+|DEPENDS:[^\]]+|CATEGORY:[^\]]+)\]", re.IGNORECASE)

# Эвристики для legacy-событий без [CATEGORY:x]: смотрим title+description.
# Порядок проверки = приоритет (учёба > работа > дорога > личное).
_HEURISTICS: list[tuple[str, "re.Pattern[str]"]] = [
    ("учёба", re.compile(
        r"\b(?:экзамен|зачет|зачёт|лекция|лекции|семинар|практика|лаборатор|пара|"
        r"коллоквиум|курсовая|диплом|занятие|урок|расписание|институт|вуз|"
        r"university|class|lecture|seminar|lab|exam|quiz|tutorial|"
        r"sgu|hse|mipt|spbu|msu|итмо|вшэ|мфти|мгу|нму|сколтех|skoltech)\b"
        r"|\b[A-Z]{1,4}\s?[A-Z]?\d{2,4}\b"  # Auditorium / course codes: B101, MA B101
        r"|\b(?:CD|MA|IoT|EE|CS|DSP|ML|AI|ENG|PHYS|MATH|CHEM|BIO|HIST)\b",
        re.IGNORECASE,
    )),
    ("работа", re.compile(
        r"\b(?:работа|работаю|sync|синк|standup|стендап|"
        r"митинг|meeting|zoom|зум|call|созвон|клиент|client|"
        r"проект|project|sprint|спринт|deadline|review|ревью|"
        r"daily|retro|ретро|план(?:инг|ировани)|интервью|interview|"
        r"asterium|deal|демо|demo|presentation|презентаци|"
        r"командир|тимлид|team\s?lead|all\s?hands)\b",
        re.IGNORECASE,
    )),
    ("дорога", re.compile(
        r"\b(?:дорога|путь|транспорт|метро|такси|авто|поезд|"
        r"travel|commute|drive|road|trip|flight|самолёт|самолет|"
        r"вокзал|аэропорт|airport|station|до\s+дома|до\s+работы)\b",
        re.IGNORECASE,
    )),
    ("личное", re.compile(
        r"\b(?:друзья|друг|подруг|семья|родители|мама|папа|family|parents|"
        r"обед|ужин|завтрак|lunch|dinner|breakfast|кофе|coffee|"
        r"спорт|gym|зал|тренировк|workout|run|пробежк|йога|yoga|"
        r"врач|doctor|стоматолог|массаж|salon|стрижк|"
        r"кино|cinema|movie|концерт|concert|театр|theatre|"
        r"свидание|date|романтик|"
        r"шоппинг|shopping|магазин|"
        r"отпуск|vacation|holiday|праздник|"
        r"birthday|др|день\s*рожд)\b",
        re.IGNORECASE,
    )),
]

# Порядок важен для legend / pie chart
KNOWN_CATEGORIES = ("учёба", "работа", "дорога", "личное")
UNKNOWN_CATEGORY = "unknown"

# Палитры по категориям (несколько оттенков для разных subject в одной семье).
# Совмещены с pie-chart — внешние модули должны брать цвета только отсюда.
CATEGORY_COLORS: dict[str, list[str]] = {
    "учёба":  ["#58a6ff", "#79c0ff", "#a5d6ff", "#388bfd", "#1f6feb"],   # blue family
    "работа": ["#f0883e", "#ff9e64", "#fdac54", "#db6d28", "#bd561d"],    # orange family
    "дорога": ["#8b949e", "#6e7681", "#a8b1bb"],                          # gray
    "личное": ["#bc8cff", "#d2a8ff", "#a371f7"],                          # purple
    UNKNOWN_CATEGORY: ["#525960"],                                        # neutral
}


def _strip_tags(s: str) -> str:
    """Убирает все служебные теги из строки."""
    return TAG_STRIP_RE.sub("", s or "").strip()


def _norm_category(raw: str) -> str:
    """Нормализует category-строку. Принимает кириллицу и латиницу."""
    v = (raw or "").strip().lower()
    aliases = {
        "study": "учёба", "uni": "учёба", "university": "учёба", "учеба": "учёба",
        "work": "работа", "office": "работа",
        "road": "дорога", "travel": "дорога", "commute": "дорога",
        "personal": "личное", "private": "личное",
    }
    v = aliases.get(v, v)
    return v if v in KNOWN_CATEGORIES else UNKNOWN_CATEGORY


def event_category(event: dict) -> str:
    """Возвращает одну из KNOWN_CATEGORIES или UNKNOWN_CATEGORY.

    Приоритет:
    1. Тег `[CATEGORY:x]` в description / title (явная разметка от агента).
    2. Эвристики по словам/паттернам в title+description (legacy события).
    """
    for field in ("description", "title"):
        text = event.get(field) or ""
        m = CATEGORY_RE.search(text)
        if m:
            return _norm_category(m.group(1))

    # Fallback: ищем по содержанию title + description
    haystack = f"{event.get('title', '')} {event.get('description', '')}"
    for cat_name, pattern in _HEURISTICS:
        if pattern.search(haystack):
            return cat_name
    return UNKNOWN_CATEGORY


def event_subject(event: dict) -> str:
    """Возвращает короткий идентификатор subject (CD/MA/IoT/...)
    или первые ~14 символов очищенного заголовка для подписи на хитмапе."""
    title = _strip_tags(event.get("title", ""))
    if not title:
        return ""
    # Берём первое «слово» (буквы/цифры) — обычно это код предмета вроде CD/MA.
    m = re.match(r"[A-Za-zА-Яа-я0-9]+", title)
    if m:
        return m.group(0)
    return title[:14]


def event_color(event: dict) -> str:
    """Цвет блока на хитмапе: берётся из палитры категории по хешу subject."""
    cat = event_category(event)
    palette = CATEGORY_COLORS.get(cat) or CATEGORY_COLORS[UNKNOWN_CATEGORY]
    subj = event_subject(event)
    if not subj:
        return palette[0]
    h = int(hashlib.md5(subj.encode("utf-8")).hexdigest(), 16)
    return palette[h % len(palette)]


def is_routine(event: dict, patterns: Iterable[str]) -> bool:
    """True если title или категория события соответствуют любому routine-паттерну."""
    if event_category(event) == "дорога":
        return True
    title = _strip_tags(event.get("title", ""))
    for pat in patterns:
        pat = (pat or "").strip()
        if not pat:
            continue
        try:
            if re.search(pat, title, flags=re.IGNORECASE):
                return True
        except re.error:
            if pat.lower() in title.lower():
                return True
    return False


def collapse_routines(
    events: list[dict], patterns: Iterable[str]
) -> tuple[list[dict], dict[str, int]]:
    """
    Делит события на (не-рутинные, словарь {название_паттерна: count}).
    Для текстовых вью: рутина схлопывается в одну строку с счётчиком.
    """
    pattern_list = [p.strip() for p in patterns if p.strip()]
    non_routine: list[dict] = []
    routine_counts: dict[str, int] = {}
    for ev in events:
        if not is_routine(ev, pattern_list):
            non_routine.append(ev)
            continue
        # Ключ — категория «дорога» или сам паттерн, если совпал по title
        key = "Дорога"
        cat = event_category(ev)
        if cat != "дорога":
            title = _strip_tags(ev.get("title", ""))
            for pat in pattern_list:
                try:
                    m = re.search(pat, title, flags=re.IGNORECASE)
                    if m:
                        key = m.group(0).capitalize()
                        break
                except re.error:
                    if pat.lower() in title.lower():
                        key = pat.capitalize()
                        break
        routine_counts[key] = routine_counts.get(key, 0) + 1
    return non_routine, routine_counts


def routine_emoji(name: str) -> str:
    """Иконка для свернутого паттерна. Для «Дорога» — 🚗."""
    n = name.lower()
    if "дорог" in n or "trav" in n or "commute" in n or "road" in n:
        return "🚗"
    return "🔁"


# ─── Tasks: urgency ──────────────────────────────────────────────────────────


def parse_task_due(due: str) -> datetime | None:
    """Парсит due из Google Tasks (формат RFC3339 c Z)."""
    if not due:
        return None
    try:
        s = due.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except Exception:
        return None


def task_urgency(
    task: dict, now: datetime, urgent_days: int, warm_days: int
) -> tuple[str, int]:
    """
    Возвращает (level, days_left).
    level: 'overdue' | 'burning' | 'warm' | 'calm' | 'none'
    days_left: целое (отрицательное для overdue, 0 = сегодня).
    """
    due_dt = parse_task_due(task.get("due", ""))
    if due_dt is None:
        return "none", 0

    # Сравниваем по календарным дням в часовом поясе now
    if now.tzinfo and due_dt.tzinfo is None:
        due_dt = due_dt.replace(tzinfo=now.tzinfo)
    days_left = (due_dt.date() - now.date()).days

    if days_left < 0:
        return "overdue", days_left
    if days_left <= urgent_days:
        return "burning", days_left
    if days_left <= warm_days:
        return "warm", days_left
    return "calm", days_left


def urgency_emoji(level: str) -> str:
    return {
        "overdue": "⚠️",
        "burning": "🔴",
        "warm":    "🟡",
        "calm":    "🟢",
        "none":    "⚪",
    }.get(level, "⚪")


def format_due_human(due_dt: datetime, now: datetime) -> str:
    """'сегодня' | 'завтра' | 'через 6д' | 'просрочено 2д' | 'dd.mm'."""
    if due_dt.tzinfo and now.tzinfo is None:
        now = now.replace(tzinfo=due_dt.tzinfo)
    if not due_dt.tzinfo and now.tzinfo:
        due_dt = due_dt.replace(tzinfo=now.tzinfo)
    days = (due_dt.date() - now.date()).days
    date_str = f"{due_dt.day:02d}.{due_dt.month:02d}"
    if days < 0:
        return f"{date_str} (просрочено {-days}д)"
    if days == 0:
        return f"{date_str} (сегодня)"
    if days == 1:
        return f"{date_str} (завтра)"
    if days <= 13:
        return f"{date_str} (через {days}д)"
    return date_str


# ─── Display helpers ─────────────────────────────────────────────────────────


def clean_title(title: str) -> str:
    """Заголовок без служебных тегов."""
    return _strip_tags(title) or "Без названия"


def routine_summary_line(routine_counts: dict[str, int]) -> str | None:
    """'🚗 Дорога ×2' или None если рутины не было."""
    if not routine_counts:
        return None
    parts = []
    for name, n in routine_counts.items():
        emoji = routine_emoji(name)
        parts.append(f"{emoji} {name} ×{n}" if n > 1 else f"{emoji} {name}")
    return "  " + " · ".join(parts)
