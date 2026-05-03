"""
Интерактивное меню настроек бота через inline-кнопки (/settings).
"""

import json
import logging
import zoneinfo

from aiogram import F, Router
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.config import config
from app.services.calendar import _update_env_file

logger = logging.getLogger(__name__)
router = Router()

# user_id → имя поля, которое ждём вводом текста
_settings_sessions: dict[int, str] = {}

# ──────────────────────────────────────────────────────────────────────────────
# Модели по провайдерам (порядок: index 0..3 → используется в callback)
# ──────────────────────────────────────────────────────────────────────────────
_GROQ_MODELS = [
    "meta-llama/llama-4-maverick-17b-128e-instruct",
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
    "mixtral-8x7b-32768",
]
_GOOGLE_MODELS = [
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
    "gemini-1.5-pro",
    "gemini-1.5-flash",
]
# короткие метки для кнопок
_GROQ_LABELS = [
    "llama-4-maverick-17b",
    "llama-3.3-70b",
    "llama-3.1-8b",
    "mixtral-8x7b",
]
_GOOGLE_LABELS = [
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
    "gemini-1.5-pro",
    "gemini-1.5-flash",
]

_TIMEZONES = [
    "Europe/Moscow",
    "Asia/Almaty",
    "Asia/Tashkent",
    "Europe/Kiev",
    "UTC",
]

_BRIEFING_TIMES = ["06:00", "07:00", "08:00", "09:00", "10:00"]
_REMINDER_HOURS = [1, 2, 3, 6, 12]

# ─── Visualization presets ────────────────────────────────────────────────────
_FREE_WINDOW_TODAY = [0.5, 1.0, 1.5, 2.0]
_FREE_WINDOW_HEATMAP = [1.0, 2.0, 3.0, 4.0]
_URGENT_DAYS = [1, 2, 3]
_WARM_DAYS = [5, 7, 10, 14]
_WORK_HOURS_WEEK = [40, 50, 60, 70, 80]


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _mask_key(key: str) -> str:
    """Маскирует API ключ, оставляя первые и последние символы."""
    if not key:
        return "(не задан) ⚠️"
    if len(key) <= 8:
        return "***  ✅"
    return f"{key[:4]}...{key[-4:]}  ✅"


def _current_model() -> str:
    provider = config.LLM_PROVIDER.lower()
    if provider == "google":
        return config.GOOGLE_AI_MODEL
    return config.GROQ_MODEL


def _check(cond: bool) -> str:
    return "✅ " if cond else ""


def _apply(key: str, value) -> None:
    """Обновляет config в памяти и записывает в .env."""
    setattr(config, key, value)
    _update_env_file(key, str(value))


# ──────────────────────────────────────────────────────────────────────────────
# Keyboard builders
# ──────────────────────────────────────────────────────────────────────────────

def _main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🤖 Провайдер & Модель", callback_data="settings:ai"),
            InlineKeyboardButton(text="🔑 API ключи",          callback_data="settings:keys"),
        ],
        [
            InlineKeyboardButton(text="⏰ Часы дня",   callback_data="settings:hours"),
            InlineKeyboardButton(text="📅 Расписание", callback_data="settings:schedule"),
        ],
        [
            InlineKeyboardButton(text="🌍 Временная зона", callback_data="settings:tz"),
            InlineKeyboardButton(text="📊 Уровень лога",   callback_data="settings:log"),
        ],
        [
            InlineKeyboardButton(text="🎨 Визуализация",   callback_data="settings:viz"),
        ],
    ])


def _ai_kb() -> InlineKeyboardMarkup:
    provider = config.LLM_PROVIDER.lower()
    rows = []
    # Provider row
    rows.append([
        InlineKeyboardButton(text=f"{_check(provider == 'groq')}Groq",         callback_data="settings:provider:groq"),
        InlineKeyboardButton(text=f"{_check(provider == 'google')}Google AI",  callback_data="settings:provider:google"),
    ])
    # Model rows (2 per row)
    if provider == "google":
        current = config.GOOGLE_AI_MODEL
        models, labels = _GOOGLE_MODELS, _GOOGLE_LABELS
    else:
        current = config.GROQ_MODEL
        models, labels = _GROQ_MODELS, _GROQ_LABELS

    model_buttons = [
        InlineKeyboardButton(
            text=f"{_check(models[i] == current)}{labels[i]}",
            callback_data=f"settings:model:{i}",
        )
        for i in range(len(models))
    ]
    # 2 per row
    for i in range(0, len(model_buttons), 2):
        rows.append(model_buttons[i:i + 2])
    rows.append([InlineKeyboardButton(text="✏️ Своя модель", callback_data="settings:model:custom")])
    rows.append([InlineKeyboardButton(text="← Назад", callback_data="settings:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _keys_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Обновить Groq ключ",      callback_data="settings:key:GROQ_API_KEY"),
            InlineKeyboardButton(text="Обновить Google AI ключ", callback_data="settings:key:GOOGLE_AI_KEY"),
        ],
        [
            InlineKeyboardButton(text="📋 Обновить Google Calendar creds", callback_data="settings:key:GOOGLE_CREDENTIALS_JSON"),
        ],
        [InlineKeyboardButton(text="← Назад", callback_data="settings:home")],
    ])


def _hours_kb() -> InlineKeyboardMarkup:
    ws = config.WORK_HOUR_START
    we = config.WORK_HOUR_END
    ss = config.SLEEP_HOUR_START
    se = config.SLEEP_HOUR_END

    def btn(text, cb): return InlineKeyboardButton(text=text, callback_data=cb)
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("🟢 Работа с:", "noop"), btn(f"[−] {ws:02d}:00 [+]", "noop")],
        [
            btn("−", "settings:work_start:dec"),
            btn(f"{ws:02d}:00", "noop"),
            btn("+", "settings:work_start:inc"),
            btn("до", "noop"),
            btn("−", "settings:work_end:dec"),
            btn(f"{we:02d}:00", "noop"),
            btn("+", "settings:work_end:inc"),
        ],
        [btn("🔴 Сон с:", "noop")],
        [
            btn("−", "settings:sleep_start:dec"),
            btn(f"{ss:02d}:00", "noop"),
            btn("+", "settings:sleep_start:inc"),
            btn("до", "noop"),
            btn("−", "settings:sleep_end:dec"),
            btn(f"{se:02d}:00", "noop"),
            btn("+", "settings:sleep_end:inc"),
        ],
        [btn("← Назад", "settings:home")],
    ])


def _schedule_kb() -> InlineKeyboardMarkup:
    rows = []
    # Briefing row
    briefing_row = [
        InlineKeyboardButton(
            text=f"{_check(config.BRIEFING_TIME == t)}{t}",
            callback_data=f"settings:briefing:{t}",
        )
        for t in _BRIEFING_TIMES
    ]
    rows.append(briefing_row)
    # Reminder row
    reminder_row = [
        InlineKeyboardButton(
            text=f"{_check(config.REMINDER_INTERVAL_HOURS == n)}{n}ч",
            callback_data=f"settings:reminder:{n}",
        )
        for n in _REMINDER_HOURS
    ]
    rows.append(reminder_row)
    rows.append([InlineKeyboardButton(text="← Назад", callback_data="settings:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _tz_kb() -> InlineKeyboardMarkup:
    tz_buttons = [
        InlineKeyboardButton(
            text=f"{_check(config.TIMEZONE == tz)}{tz}",
            callback_data=f"settings:tz:{tz}",
        )
        for tz in _TIMEZONES
    ]
    rows = []
    for i in range(0, len(tz_buttons), 2):
        rows.append(tz_buttons[i:i + 2])
    rows.append([InlineKeyboardButton(text="✏️ Ввести вручную", callback_data="settings:tz:custom")])
    rows.append([InlineKeyboardButton(text="← Назад", callback_data="settings:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _log_kb() -> InlineKeyboardMarkup:
    levels = ["DEBUG", "INFO", "WARNING", "ERROR"]
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=f"{_check(config.LOG_LEVEL == lv)}{lv}",
                callback_data=f"settings:log:{lv}",
            )
            for lv in levels
        ],
        [InlineKeyboardButton(text="← Назад", callback_data="settings:home")],
    ])


def _viz_kb() -> InlineKeyboardMarkup:
    rows = []
    rows.append([InlineKeyboardButton(text="🟢 Свободное окно (Сегодня):", callback_data="noop")])
    rows.append([
        InlineKeyboardButton(
            text=f"{_check(abs(config.MIN_FREE_WINDOW_TODAY_HOURS - h) < 0.01)}{h:g}ч",
            callback_data=f"settings:viz_today:{h}",
        )
        for h in _FREE_WINDOW_TODAY
    ])
    rows.append([InlineKeyboardButton(text="📅 Свободное окно (хитмап):", callback_data="noop")])
    rows.append([
        InlineKeyboardButton(
            text=f"{_check(abs(config.MIN_FREE_WINDOW_HOURS - h) < 0.01)}{h:g}ч",
            callback_data=f"settings:viz_heatmap:{h}",
        )
        for h in _FREE_WINDOW_HEATMAP
    ])
    rows.append([InlineKeyboardButton(text="🔴 Срочно (≤Nд):", callback_data="noop")])
    rows.append([
        InlineKeyboardButton(
            text=f"{_check(config.URGENT_TASK_DAYS == n)}{n}д",
            callback_data=f"settings:viz_urgent:{n}",
        )
        for n in _URGENT_DAYS
    ])
    rows.append([InlineKeyboardButton(text="🟡 Тёплая задача (≤Nд):", callback_data="noop")])
    rows.append([
        InlineKeyboardButton(
            text=f"{_check(config.WARM_TASK_DAYS == n)}{n}д",
            callback_data=f"settings:viz_warm:{n}",
        )
        for n in _WARM_DAYS
    ])
    rows.append([InlineKeyboardButton(text="📊 Активных часов в неделю:", callback_data="noop")])
    rows.append([
        InlineKeyboardButton(
            text=f"{_check(config.WORK_HOURS_PER_WEEK == n)}{n}ч",
            callback_data=f"settings:viz_workhrs:{n}",
        )
        for n in _WORK_HOURS_WEEK
    ])
    rows.append([InlineKeyboardButton(text="✏️ Изменить рутинные паттерны", callback_data="settings:viz_routine_edit")])
    rows.append([InlineKeyboardButton(text="← Назад", callback_data="settings:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ──────────────────────────────────────────────────────────────────────────────
# Text builders
# ──────────────────────────────────────────────────────────────────────────────

def _main_text() -> str:
    provider = config.LLM_PROVIDER.lower()
    model = _current_model()
    # short model label
    short_model = model.split("/")[-1] if "/" in model else model
    ws, we = config.WORK_HOUR_START, config.WORK_HOUR_END
    ss, se = config.SLEEP_HOUR_START, config.SLEEP_HOUR_END
    return (
        "⚙️ *Настройки бота*\n\n"
        f"*LLM:* `{provider}` / `{short_model}`\n"
        f"*Часы:* `{ws}:00–{we}:00`  ·  Сон `{ss}:00–{se}:00`\n"
        f"*Зона:* `{config.TIMEZONE}`  ·  Брифинг `{config.BRIEFING_TIME}`\n"
        f"*Напоминания:* каждые `{config.REMINDER_INTERVAL_HOURS}` ч"
    )


def _ai_text() -> str:
    provider = config.LLM_PROVIDER.lower()
    model = _current_model()
    short = model.split("/")[-1] if "/" in model else model
    return (
        "🤖 *Провайдер & Модель*\n\n"
        f"*Текущий:* `{provider}` / `{short}`\n\n"
        "Выбери провайдера и модель:"
    )


def _keys_text() -> str:
    groq_masked   = _mask_key(config.GROQ_API_KEY)
    google_masked = _mask_key(config.GOOGLE_AI_KEY)
    gcal_status   = "задан ✅" if config.GOOGLE_CREDENTIALS_JSON else "(не задан) ⚠️"
    return (
        "🔑 *API ключи*\n\n"
        f"*Groq API key:* `{groq_masked}`\n"
        f"*Google AI key:* `{google_masked}`\n"
        f"*Google Calendar creds:* `{gcal_status}`"
    )


def _hours_text() -> str:
    ws, we = config.WORK_HOUR_START, config.WORK_HOUR_END
    ss, se = config.SLEEP_HOUR_START, config.SLEEP_HOUR_END
    non_morning = f"{se:02d}:00–{ws:02d}:00" if se < ws else "—"
    non_evening = f"{we:02d}:00–{ss:02d}:00"
    return (
        "⏰ *Часы дня*\n\n"
        f"🟢 Рабочее: `{ws:02d}:00–{we:02d}:00`\n"
        f"🟡 Нерабочее: `{non_morning}` и `{non_evening}`\n"
        f"🔴 Сон: `{ss:02d}:00–{se:02d}:00`"
    )


def _schedule_text() -> str:
    return (
        "📅 *Расписание*\n\n"
        f"*Утренний брифинг:* `{config.BRIEFING_TIME}`\n"
        f"*Напоминания:* каждые `{config.REMINDER_INTERVAL_HOURS}` ч\n\n"
        "Выбери время брифинга и интервал напоминаний:"
    )


def _tz_text() -> str:
    return (
        "🌍 *Временная зона*\n\n"
        f"*Текущая:* `{config.TIMEZONE}`\n\n"
        "Выбери зону или введи вручную (IANA, например `Asia/Almaty`):"
    )


def _log_text() -> str:
    return f"📊 *Уровень логирования*\n\nТекущий: `{config.LOG_LEVEL}`"


def _viz_text() -> str:
    routine = config.ROUTINE_PATTERNS or "(пусто)"
    return (
        "🎨 *Визуализация*\n\n"
        f"*Свободное окно — Сегодня:* `≥ {config.MIN_FREE_WINDOW_TODAY_HOURS:g}ч`\n"
        f"*Свободное окно — хитмап:* `≥ {config.MIN_FREE_WINDOW_HOURS:g}ч`\n"
        f"*🔴 Срочно:* `≤ {config.URGENT_TASK_DAYS}д`  ·  *🟡 Тёплая:* `≤ {config.WARM_TASK_DAYS}д`\n"
        f"*Часов в неделю:* `{config.WORK_HOURS_PER_WEEK}ч` (для % загрузки)\n"
        f"*Рутина (regex CSV):* `{routine}`"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Public entry point
# ──────────────────────────────────────────────────────────────────────────────

async def send_settings_menu(target: "Message | CallbackQuery") -> None:
    """Отправляет или редактирует сообщение с главным меню настроек."""
    text = _main_text()
    kb = _main_kb()
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
        await target.answer()
    else:
        await target.answer(text, reply_markup=kb, parse_mode="Markdown")


# ──────────────────────────────────────────────────────────────────────────────
# /settings command (local shortcut — also called from commands.py)
# ──────────────────────────────────────────────────────────────────────────────

# ──────────────────────────────────────────────────────────────────────────────
# Callback: navigation (home + submenus)
# ──────────────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "settings:home")
async def cb_home(callback: CallbackQuery) -> None:
    await callback.message.edit_text(_main_text(), reply_markup=_main_kb(), parse_mode="Markdown")
    await callback.answer()


@router.callback_query(F.data == "settings:ai")
async def cb_ai(callback: CallbackQuery) -> None:
    await callback.message.edit_text(_ai_text(), reply_markup=_ai_kb(), parse_mode="Markdown")
    await callback.answer()


@router.callback_query(F.data == "settings:keys")
async def cb_keys(callback: CallbackQuery) -> None:
    await callback.message.edit_text(_keys_text(), reply_markup=_keys_kb(), parse_mode="Markdown")
    await callback.answer()


@router.callback_query(F.data == "settings:hours")
async def cb_hours(callback: CallbackQuery) -> None:
    await callback.message.edit_text(_hours_text(), reply_markup=_hours_kb(), parse_mode="Markdown")
    await callback.answer()


@router.callback_query(F.data == "settings:schedule")
async def cb_schedule(callback: CallbackQuery) -> None:
    await callback.message.edit_text(_schedule_text(), reply_markup=_schedule_kb(), parse_mode="Markdown")
    await callback.answer()


@router.callback_query(F.data == "settings:tz")
async def cb_tz(callback: CallbackQuery) -> None:
    await callback.message.edit_text(_tz_text(), reply_markup=_tz_kb(), parse_mode="Markdown")
    await callback.answer()


@router.callback_query(F.data == "settings:log")
async def cb_log(callback: CallbackQuery) -> None:
    await callback.message.edit_text(_log_text(), reply_markup=_log_kb(), parse_mode="Markdown")
    await callback.answer()


@router.callback_query(F.data == "settings:viz")
async def cb_viz(callback: CallbackQuery) -> None:
    await callback.message.edit_text(_viz_text(), reply_markup=_viz_kb(), parse_mode="Markdown")
    await callback.answer()


@router.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery) -> None:
    await callback.answer()


# ──────────────────────────────────────────────────────────────────────────────
# Callback: visualization sub-options
# ──────────────────────────────────────────────────────────────────────────────

_VIZ_FIELDS = {
    "viz_today":   ("MIN_FREE_WINDOW_TODAY_HOURS", float, "Свободное окно (Сегодня)"),
    "viz_heatmap": ("MIN_FREE_WINDOW_HOURS",       float, "Свободное окно (хитмап)"),
    "viz_urgent":  ("URGENT_TASK_DAYS",            int,   "Срочно"),
    "viz_warm":    ("WARM_TASK_DAYS",              int,   "Тёплая"),
    "viz_workhrs": ("WORK_HOURS_PER_WEEK",         int,   "Часов в неделю"),
}


@router.callback_query(F.data.startswith("settings:viz_") & ~F.data.in_({"settings:viz_routine_edit"}))
async def cb_viz_set(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer()
        return
    field_key, raw = parts[1], parts[2]
    spec = _VIZ_FIELDS.get(field_key)
    if not spec:
        await callback.answer()
        return
    config_key, caster, label = spec
    try:
        value = caster(raw)
    except ValueError:
        await callback.answer("Ошибка значения")
        return
    _apply(config_key, value)
    await callback.message.edit_text(_viz_text(), reply_markup=_viz_kb(), parse_mode="Markdown")
    await callback.answer(f"{label}: {value}")


@router.callback_query(F.data == "settings:viz_routine_edit")
async def cb_viz_routine(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    _settings_sessions[uid] = "ROUTINE_PATTERNS"
    current = config.ROUTINE_PATTERNS or "(пусто)"
    await callback.message.edit_text(
        "✏️ Отправь список рутинных паттернов через запятую (regex поддерживается).\n"
        f"Текущие: `{current}`\n\n"
        "Пример: `Дорога,Coffee,Standup`",
        parse_mode="Markdown",
    )
    await callback.answer()


# ──────────────────────────────────────────────────────────────────────────────
# Callback: provider switch
# ──────────────────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("settings:provider:"))
async def cb_provider(callback: CallbackQuery) -> None:
    provider = callback.data.split(":")[2]
    if provider not in ("groq", "google"):
        await callback.answer("Неизвестный провайдер")
        return
    _apply("LLM_PROVIDER", provider)
    await callback.message.edit_text(_ai_text(), reply_markup=_ai_kb(), parse_mode="Markdown")
    await callback.answer(f"Провайдер: {provider}")


# ──────────────────────────────────────────────────────────────────────────────
# Callback: model selection
# ──────────────────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("settings:model:"))
async def cb_model(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    val = callback.data[len("settings:model:"):]
    if val == "custom":
        _settings_sessions[uid] = "MODEL_CUSTOM"
        await callback.message.edit_text(
            "✏️ Отправь название модели следующим сообщением:",
            parse_mode="Markdown",
        )
        await callback.answer()
        return
    try:
        idx = int(val)
    except ValueError:
        await callback.answer("Ошибка")
        return
    provider = config.LLM_PROVIDER.lower()
    models = _GOOGLE_MODELS if provider == "google" else _GROQ_MODELS
    if idx < 0 or idx >= len(models):
        await callback.answer("Неверный индекс")
        return
    model_name = models[idx]
    key = "GOOGLE_AI_MODEL" if provider == "google" else "GROQ_MODEL"
    _apply(key, model_name)
    await callback.message.edit_text(_ai_text(), reply_markup=_ai_kb(), parse_mode="Markdown")
    short = model_name.split("/")[-1] if "/" in model_name else model_name
    await callback.answer(f"Модель: {short}")


# ──────────────────────────────────────────────────────────────────────────────
# Callback: API key update (request text input)
# ──────────────────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("settings:key:"))
async def cb_key(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    field = callback.data[len("settings:key:"):]
    _FIELD_LABELS = {
        "GROQ_API_KEY":            "Groq API ключ",
        "GOOGLE_AI_KEY":           "Google AI ключ",
        "GOOGLE_CREDENTIALS_JSON": "Google Calendar credentials\n_(содержимое credentials\\.json одной строкой)_",
    }
    if field not in _FIELD_LABELS:
        await callback.answer("Неизвестное поле")
        return
    _settings_sessions[uid] = field
    label = _FIELD_LABELS[field]
    await callback.message.edit_text(
        f"✏️ Отправь *{label}* следующим сообщением:",
        parse_mode="MarkdownV2",
    )
    await callback.answer()


# ──────────────────────────────────────────────────────────────────────────────
# Callback: hour +/-
# ──────────────────────────────────────────────────────────────────────────────

_HOUR_FIELDS = {
    "work_start":  "WORK_HOUR_START",
    "work_end":    "WORK_HOUR_END",
    "sleep_start": "SLEEP_HOUR_START",
    "sleep_end":   "SLEEP_HOUR_END",
}


@router.callback_query(F.data.startswith("settings:work_") | F.data.startswith("settings:sleep_"))
async def cb_hour(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")  # ["settings", "work_start", "inc"]
    if len(parts) != 3:
        await callback.answer()
        return
    field_key = parts[1]   # work_start / work_end / sleep_start / sleep_end
    direction = parts[2]   # inc / dec
    config_key = _HOUR_FIELDS.get(field_key)
    if not config_key:
        await callback.answer()
        return
    current = getattr(config, config_key)
    new_val = (current + (1 if direction == "inc" else -1)) % 24
    _apply(config_key, new_val)
    await callback.message.edit_text(_hours_text(), reply_markup=_hours_kb(), parse_mode="Markdown")
    await callback.answer(f"{config_key}: {new_val:02d}:00")


# ──────────────────────────────────────────────────────────────────────────────
# Callback: briefing time
# ──────────────────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("settings:briefing:"))
async def cb_briefing(callback: CallbackQuery) -> None:
    val = callback.data[len("settings:briefing:"):]
    _apply("BRIEFING_TIME", val)
    from app.services.scheduler_ref import reschedule_briefing
    reschedule_briefing(val, config.TIMEZONE)
    await callback.message.edit_text(_schedule_text(), reply_markup=_schedule_kb(), parse_mode="Markdown")
    await callback.answer(f"Брифинг: {val}")


# ──────────────────────────────────────────────────────────────────────────────
# Callback: reminder interval
# ──────────────────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("settings:reminder:"))
async def cb_reminder(callback: CallbackQuery) -> None:
    val = callback.data[len("settings:reminder:"):]
    try:
        hours = int(val)
    except ValueError:
        await callback.answer("Ошибка")
        return
    _apply("REMINDER_INTERVAL_HOURS", hours)
    await callback.message.edit_text(_schedule_text(), reply_markup=_schedule_kb(), parse_mode="Markdown")
    await callback.answer(f"Напоминания: каждые {hours} ч")


# ──────────────────────────────────────────────────────────────────────────────
# Callback: timezone
# ──────────────────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("settings:tz:"))
async def cb_tz_set(callback: CallbackQuery) -> None:
    uid = callback.from_user.id
    val = callback.data[len("settings:tz:"):]
    if val == "custom":
        _settings_sessions[uid] = "TIMEZONE"
        await callback.message.edit_text(
            "✏️ Отправь IANA-название временной зоны (например `Asia/Almaty`):",
            parse_mode="Markdown",
        )
        await callback.answer()
        return
    # Validate
    try:
        zoneinfo.ZoneInfo(val)
    except Exception:
        await callback.answer(f"Неверная зона: {val}")
        return
    _apply("TIMEZONE", val)
    from app.services.scheduler_ref import reschedule_briefing
    reschedule_briefing(config.BRIEFING_TIME, val)
    await callback.message.edit_text(_tz_text(), reply_markup=_tz_kb(), parse_mode="Markdown")
    await callback.answer(f"Зона: {val}")


# ──────────────────────────────────────────────────────────────────────────────
# Callback: log level
# ──────────────────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("settings:log:"))
async def cb_log_set(callback: CallbackQuery) -> None:
    level = callback.data[len("settings:log:"):].upper()
    if level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        await callback.answer("Неверный уровень")
        return
    _apply("LOG_LEVEL", level)
    import logging as _logging
    _logging.getLogger().setLevel(level)
    await callback.message.edit_text(_log_text(), reply_markup=_log_kb(), parse_mode="Markdown")
    await callback.answer(f"Лог: {level}")


# ──────────────────────────────────────────────────────────────────────────────
# Text handler: catches API key / model / timezone input when session is active
# ──────────────────────────────────────────────────────────────────────────────

@router.message(lambda msg: msg.from_user is not None and msg.from_user.id in _settings_sessions)
async def handle_settings_text(message: Message) -> None:
    uid = message.from_user.id
    field = _settings_sessions.pop(uid, None)
    if field is None:
        return  # Not waiting — pass to next router

    value = (message.text or "").strip()

    if field == "TIMEZONE":
        try:
            zoneinfo.ZoneInfo(value)
        except Exception:
            await message.answer(f"❌ Неверная временная зона: `{value}`\nПример: `Asia/Almaty`", parse_mode="Markdown")
            return
        _apply("TIMEZONE", value)
        from app.services.scheduler_ref import reschedule_briefing
        reschedule_briefing(config.BRIEFING_TIME, value)
        await message.answer(f"✅ Временная зона: `{value}`", parse_mode="Markdown")
        await message.answer(_tz_text(), reply_markup=_tz_kb(), parse_mode="Markdown")

    elif field == "MODEL_CUSTOM":
        provider = config.LLM_PROVIDER.lower()
        key = "GOOGLE_AI_MODEL" if provider == "google" else "GROQ_MODEL"
        _apply(key, value)
        short = value.split("/")[-1] if "/" in value else value
        await message.answer(f"✅ Модель: `{short}`", parse_mode="Markdown")
        await message.answer(_ai_text(), reply_markup=_ai_kb(), parse_mode="Markdown")

    elif field in ("GROQ_API_KEY", "GOOGLE_AI_KEY"):
        _apply(field, value)
        label = "Groq API ключ" if field == "GROQ_API_KEY" else "Google AI ключ"
        await message.answer(f"✅ {label} сохранён", parse_mode="Markdown")
        await message.answer(_keys_text(), reply_markup=_keys_kb(), parse_mode="Markdown")

    elif field == "ROUTINE_PATTERNS":
        # Validate that each pattern is a valid regex (or empty)
        import re as _re
        bad = []
        for p in [x.strip() for x in value.split(",") if x.strip()]:
            try:
                _re.compile(p)
            except _re.error as exc:
                bad.append(f"{p}: {exc}")
        if bad:
            await message.answer(
                "❌ Невалидные regex:\n" + "\n".join(f"  • `{b}`" for b in bad),
                parse_mode="Markdown",
            )
            return
        _apply("ROUTINE_PATTERNS", value)
        await message.answer(f"✅ Рутинные паттерны: `{value or '(пусто)'}`", parse_mode="Markdown")
        await message.answer(_viz_text(), reply_markup=_viz_kb(), parse_mode="Markdown")

    elif field == "GOOGLE_CREDENTIALS_JSON":
        try:
            parsed = json.loads(value)
            inner = parsed.get("installed") or parsed.get("web")
            if not inner or not inner.get("client_id") or not inner.get("client_secret"):
                raise ValueError("не найдены client_id или client_secret")
        except Exception as exc:
            await message.answer(
                f"❌ Неверный формат: `{exc}`\n\n"
                "Вставь содержимое файла `credentials.json` целиком одной строкой.",
                parse_mode="Markdown",
            )
            return
        _apply("GOOGLE_CREDENTIALS_JSON", value)
        await message.answer(
            "✅ Google Calendar credentials сохранены.\n\n"
            "Выполни `/reauth` чтобы авторизоваться в Google Calendar.",
            parse_mode="Markdown",
        )
        await message.answer(_keys_text(), reply_markup=_keys_kb(), parse_mode="Markdown")

    else:
        await message.answer(f"❌ Неизвестное поле: {field}")
