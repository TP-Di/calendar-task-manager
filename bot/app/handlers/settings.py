"""
Интерактивное меню настроек бота через inline-кнопки (/settings).
"""

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
    groq_masked = _mask_key(config.GROQ_API_KEY)
    google_masked = _mask_key(config.GOOGLE_AI_KEY)
    return (
        "🔑 *API ключи*\n\n"
        f"*Groq API key:* `{groq_masked}`\n"
        f"*Google AI key:* `{google_masked}`"
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


@router.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery) -> None:
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
    if field not in ("GROQ_API_KEY", "GOOGLE_AI_KEY"):
        await callback.answer("Неизвестное поле")
        return
    _settings_sessions[uid] = field
    label = "Groq API ключ" if field == "GROQ_API_KEY" else "Google AI ключ"
    await callback.message.edit_text(
        f"✏️ Отправь новый *{label}* следующим сообщением:",
        parse_mode="Markdown",
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

    else:
        await message.answer(f"❌ Неизвестное поле: {field}")
