"""
Обработчик обычных сообщений → агент.
Также обрабатывает inline-кнопки подтверждения, snooze и grid выбора слотов.
"""

import asyncio
import copy
import json
import logging
import re
import zoneinfo as _zi
from datetime import datetime as _dt, date as _date, timedelta

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.services.agent import execute_pending_tool, run_agent
from app.services.calendar import TokenExpiredError
from app.handlers.commands import send_token_expired
import app.services.calendar as cal_svc
import app.services.tasks as tasks_svc
from app.services import reschedule as reschedule_svc

logger = logging.getLogger(__name__)
router = Router()

# Хранилище ожидающих подтверждения tool calls: user_id -> pending_data
_pending_confirmations: dict[int, dict] = {}

# Хранилище grid-сессий выбора слота: user_id -> session
_grid_sessions: dict[int, dict] = {}

# ─── Grid constants ────────────────────────────────────────────────────────────
_GRID_START = 9    # 09:00
_GRID_SLOTS = 24   # 24 × 30 мин = 9:00–21:00
_GRID_COLS  = 4    # 4 кнопки в ряду → 6 рядов

_WEEKDAYS_RU = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


# ─── Timezone helpers ──────────────────────────────────────────────────────────

def _app_tz() -> _zi.ZoneInfo:
    from app.config import config
    return _zi.ZoneInfo(config.TIMEZONE)


def _parse_iso_dt(s: str) -> _dt:
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = _dt.fromisoformat(s)
    except ValueError:
        dt = _dt.fromisoformat(s[:19])
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_zi.ZoneInfo("UTC"))
    return dt


def _to_local_hhmm(iso: str) -> str:
    """ISO datetime → HH:MM в часовом поясе приложения."""
    try:
        return _parse_iso_dt(iso).astimezone(_app_tz()).strftime("%H:%M")
    except Exception:
        return iso[11:16] if len(iso) >= 16 else "??"


def _to_local_date(iso: str) -> str:
    """ISO datetime → YYYY-MM-DD в часовом поясе приложения."""
    try:
        return _parse_iso_dt(iso).astimezone(_app_tz()).strftime("%Y-%m-%d")
    except Exception:
        return iso[:10]


def _make_local_iso(date_str: str, hhmm: str) -> str:
    """YYYY-MM-DD + HH:MM → ISO datetime в часовом поясе приложения."""
    tz = _app_tz()
    y, mo, d = int(date_str[:4]), int(date_str[5:7]), int(date_str[8:10])
    h, m = int(hhmm[:2]), int(hhmm[3:5])
    return _dt(y, mo, d, h, m, tzinfo=tz).isoformat()


def _date_label(date_str: str) -> str:
    d = _date.fromisoformat(date_str)
    wd = _WEEKDAYS_RU[d.weekday()]
    return f"{d.day:02d}.{d.month:02d} {wd}"


# ─── Grid slot helpers ─────────────────────────────────────────────────────────

def _slot_to_hhmm(idx: int) -> str:
    total = _GRID_START * 60 + idx * 30
    return f"{total // 60:02d}:{total % 60:02d}"


def _hhmm_to_slot_floor(hhmm: str) -> int:
    h, m = int(hhmm[:2]), int(hhmm[3:5])
    return max(0, min(_GRID_SLOTS - 1, (h * 60 + m - _GRID_START * 60) // 30))


def _hhmm_to_slot_ceil_excl(hhmm: str) -> int:
    h, m = int(hhmm[:2]), int(hhmm[3:5])
    raw = h * 60 + m - _GRID_START * 60
    return max(0, min(_GRID_SLOTS, (raw + 29) // 30))


def _events_to_slot_types(events: list[dict]) -> dict[int, str]:
    """Возвращает {slot_idx: 'hard'|'soft'} — времена конвертируются в локальный tz."""
    result: dict[int, str] = {}
    for ev in events:
        s_iso = ev.get("start", "")
        e_iso = ev.get("end", "")
        if not s_iso or not e_iso:
            continue
        s_hhmm = _to_local_hhmm(s_iso)
        e_hhmm = _to_local_hhmm(e_iso)
        hard = reschedule_svc.is_hard(ev)
        s_slot = _hhmm_to_slot_floor(s_hhmm)
        e_slot = _hhmm_to_slot_ceil_excl(e_hhmm)
        for idx in range(s_slot, e_slot):
            if 0 <= idx < _GRID_SLOTS:
                if result.get(idx) != "hard":
                    result[idx] = "hard" if hard else "soft"
    return result


def _initial_selection(tool_args: dict) -> set[int]:
    """Начальная выборка слотов из tool_args (в локальном времени)."""
    start_iso = tool_args.get("start_time") or tool_args.get("start", "")
    end_iso   = tool_args.get("end_time")   or tool_args.get("end",   "")
    if not start_iso or not end_iso:
        return set()
    s = _hhmm_to_slot_floor(_to_local_hhmm(start_iso))
    e = _hhmm_to_slot_ceil_excl(_to_local_hhmm(end_iso))
    return set(range(s, e))


# ─── Grid keyboard builder ─────────────────────────────────────────────────────

def _build_grid_keyboard(
    selected: set[int],
    slot_types: dict[int, str],
    date_str: str,
) -> InlineKeyboardMarkup:
    rows = []

    # Ряд навигации по датам
    prev_d = (_date.fromisoformat(date_str) - timedelta(days=1)).strftime("%d.%m")
    next_d = (_date.fromisoformat(date_str) + timedelta(days=1)).strftime("%d.%m")
    rows.append([
        InlineKeyboardButton(text=f"◀ {prev_d}", callback_data="grid_day:prev"),
        InlineKeyboardButton(text=f"📅 {_date_label(date_str)}", callback_data="grid_day:cur"),
        InlineKeyboardButton(text=f"{next_d} ▶", callback_data="grid_day:next"),
    ])

    # Слоты: 4 per row × 6 rows
    for row in range(_GRID_SLOTS // _GRID_COLS):
        btns = []
        for col in range(_GRID_COLS):
            idx = row * _GRID_COLS + col
            stype = slot_types.get(idx)
            if idx in selected:
                emoji = "🔵" if stype == "soft" else "🟢"
            elif stype == "hard":
                emoji = "🔴"
            elif stype == "soft":
                emoji = "🟡"
            else:
                emoji = "⬜"
            btns.append(InlineKeyboardButton(
                text=f"{emoji}{_slot_to_hhmm(idx)}",
                callback_data=f"st:{idx}",
            ))
        rows.append(btns)

    rows.append([
        InlineKeyboardButton(text="✅ Подтвердить", callback_data="grid_confirm"),
        InlineKeyboardButton(text="❌ Отмена",      callback_data="grid_cancel"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _grid_msg_text(task_title: str, date_str: str) -> str:
    d = _date.fromisoformat(date_str)
    wd = _WEEKDAYS_RU[d.weekday()]
    date_label = f"{d.day:02d}.{d.month:02d} ({wd})"
    return (
        f"📅 Выбери слоты для *{task_title}*\n"
        f"📆 {date_label}\n\n"
        "⬜ свободно  🟡 можно сдвинуть  🔴 нельзя трогать\n"
        "🟢 выбрано (свободный)  🔵 выбрано (сдвинет событие)"
    )


def _contiguous_groups(selected: set[int]) -> list[list[int]]:
    """Разбивает выбранные слоты на несмежные группы."""
    if not selected:
        return []
    slots = sorted(selected)
    groups: list[list[int]] = [[slots[0]]]
    for s in slots[1:]:
        if s == groups[-1][-1] + 1:
            groups[-1].append(s)
        else:
            groups.append([s])
    return groups


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _timed_tool(tools: list[dict]) -> tuple[str, dict, str, str] | None:
    for t in tools:
        name, args = t["tool_name"], t["tool_args"]
        if name == "create_task" and args.get("start_time") and args.get("end_time"):
            return name, args, args["start_time"], args["end_time"]
        if name == "create_event" and args.get("start") and args.get("end"):
            return name, args, args["start"], args["end"]
    return None


def _make_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да",  callback_data="confirm:yes"),
        InlineKeyboardButton(text="❌ Нет", callback_data="confirm:no"),
    ]])


async def _fetch_day_events(date_str: str) -> list[dict]:
    tz = _app_tz()
    y, mo, d = int(date_str[:4]), int(date_str[5:7]), int(date_str[8:10])
    day_start = _dt(y, mo, d,  0,  0, 0, tzinfo=tz)
    day_end   = _dt(y, mo, d, 23, 59, 59, tzinfo=tz)
    return await cal_svc.get_events(day_start.isoformat(), day_end.isoformat())


# ─── Main agent response handler ──────────────────────────────────────────────

async def handle_agent_response(message: Message, response: str, user_id: int) -> None:
    if not response.startswith("PENDING_TOOL::"):
        try:
            await message.answer(response, parse_mode="Markdown")
        except Exception:
            await message.answer(response, parse_mode=None)
        return

    json_str = response[len("PENDING_TOOL::"):]
    try:
        pending = json.loads(json_str)
    except json.JSONDecodeError:
        await message.answer("❌ Внутренняя ошибка агента")
        return

    _pending_confirmations[user_id] = pending

    tools: list[dict] = pending.get("tools") or []
    if not tools and pending.get("tool_name"):
        tools = [{"tool_name": pending["tool_name"], "tool_args": pending["tool_args"]}]

    if len(tools) == 1:
        description = _describe_tool_action(tools[0]["tool_name"], tools[0]["tool_args"])
    else:
        parts = [_describe_tool_action(t["tool_name"], t["tool_args"]) for t in tools]
        description = "\n\n".join(f"*{i+1}.* {p}" for i, p in enumerate(parts))

    timed = _timed_tool(tools)
    if timed:
        tool_name, tool_args, start_iso, end_iso = timed
        try:
            events = await cal_svc.get_events(start_iso, end_iso)
        except Exception:
            events = []

        soft_events = [e for e in events if not reschedule_svc.is_hard(e)]
        hard_events = [e for e in events if reschedule_svc.is_hard(e)]

        if soft_events:
            date_str   = _to_local_date(start_iso)
            slot_types = _events_to_slot_types(events)
            selected   = _initial_selection(tool_args)
            task_title = tool_args.get("title", "задача")

            _grid_sessions[user_id] = {
                "pending":    pending,
                "events":     events,
                "selected":   selected,
                "slot_types": slot_types,
                "date":       date_str,
                "task_title": task_title,
                "duration":   len(selected),  # сохраняем длительность для навигации
            }
            _pending_confirmations.pop(user_id, None)

            text = _grid_msg_text(task_title, date_str)
            kb   = _build_grid_keyboard(selected, slot_types, date_str)
            try:
                await message.answer(text, parse_mode="Markdown", reply_markup=kb)
            except Exception:
                await message.answer(text, reply_markup=kb)
            return

        if hard_events:
            lines = ["\n\n⚠️ *Пересечение с HARD-событиями (не будут тронуты):*"]
            for ev in hard_events:
                s, e = ev.get("start", ""), ev.get("end", "")
                t_str = f" {_to_local_hhmm(s)}–{_to_local_hhmm(e)}" if s and e else ""
                lines.append(f"• {ev.get('title', '?')}{t_str}")
            description += "\n".join(lines)

    try:
        await message.answer(
            f"🔔 *Подтверждение действия:*\n\n{description}\n\nВыполнить?",
            parse_mode="Markdown",
            reply_markup=_make_confirm_keyboard(),
        )
    except Exception:
        await message.answer(
            f"🔔 Подтверждение действия:\n\n{description}\n\nВыполнить?",
            reply_markup=_make_confirm_keyboard(),
        )


# ─── Grid callbacks ────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("st:"))
async def handle_slot_toggle(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    session = _grid_sessions.get(user_id)
    if not session:
        await callback.answer("Сессия истекла. Повтори запрос.", show_alert=True)
        return

    idx   = int(callback.data.split(":")[1])
    stype = session["slot_types"].get(idx)

    if stype == "hard":
        await callback.answer("Этот слот занят и не может быть изменён.", show_alert=True)
        return

    selected: set[int] = session["selected"]
    if idx in selected:
        selected.discard(idx)
    else:
        selected.add(idx)

    await callback.answer()
    try:
        await callback.message.edit_reply_markup(
            reply_markup=_build_grid_keyboard(selected, session["slot_types"], session["date"])
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("grid_day:"))
async def handle_grid_day_nav(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    session = _grid_sessions.get(user_id)
    if not session:
        await callback.answer("Сессия истекла. Повтори запрос.", show_alert=True)
        return

    direction = callback.data.split(":")[1]
    if direction == "cur":
        await callback.answer()
        return

    current  = _date.fromisoformat(session["date"])
    new_date = current + timedelta(days=-1 if direction == "prev" else 1)
    new_date_str = new_date.isoformat()

    await callback.answer()

    try:
        events = await _fetch_day_events(new_date_str)
    except Exception as _e:
        logger.warning("Не удалось загрузить события для %s: %s", new_date_str, _e)
        events = []

    slot_types = _events_to_slot_types(events)

    # Сохраняем те же индексы слотов (то же время, другой день)
    session["date"]       = new_date_str
    session["events"]     = events
    session["slot_types"] = slot_types

    try:
        await callback.message.edit_text(
            _grid_msg_text(session["task_title"], new_date_str),
            parse_mode="Markdown",
            reply_markup=_build_grid_keyboard(session["selected"], slot_types, new_date_str),
        )
    except Exception:
        try:
            await callback.message.edit_reply_markup(
                reply_markup=_build_grid_keyboard(session["selected"], slot_types, new_date_str)
            )
        except Exception:
            pass


@router.callback_query(F.data == "grid_confirm")
async def handle_grid_confirm(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    session = _grid_sessions.pop(user_id, None)
    if not session:
        await callback.answer("Сессия истекла. Повтори запрос.", show_alert=True)
        return

    selected: set[int] = session["selected"]
    if not selected:
        await callback.answer("Выбери хотя бы один слот.", show_alert=True)
        _grid_sessions[user_id] = session
        return

    try:
        await callback.answer()
    except Exception as _e:
        logger.warning("callback.answer failed: %s", _e)

    try:
        await callback.message.edit_text(
            (callback.message.text or "") + "\n\n⏳ Выполняю...",
            reply_markup=None,
        )
    except Exception:
        pass

    async def _send_result(text: str) -> None:
        try:
            await callback.message.answer(text, parse_mode="Markdown")
        except Exception:
            try:
                await callback.message.answer(text, parse_mode=None)
            except Exception as _e:
                logger.error("Не удалось отправить результат: %s | текст: %s", _e, text[:200])

    try:
        date = session["date"]
        groups = _contiguous_groups(selected)
        logger.info("grid_confirm: user=%s date=%s groups=%s", user_id, date, groups)

        result_lines: list[str] = []
        rescheduled_ids: set[str] = set()

        orig_pending = session["pending"]
        orig_tools: list[dict] = orig_pending.get("tools") or []
        if not orig_tools and orig_pending.get("tool_name"):
            orig_tools = [{"tool_name": orig_pending["tool_name"],
                           "tool_args": orig_pending["tool_args"]}]

        if not orig_tools:
            logger.error("grid_confirm: orig_tools is empty, pending=%s", orig_pending)
            await _send_result("❌ Внутренняя ошибка: не найдены инструменты в сессии.")
            return

        soft_events = [e for e in session["events"] if not reschedule_svc.is_hard(e)]

        for g_idx, group in enumerate(groups):
            new_start_iso = _make_local_iso(date, _slot_to_hhmm(group[0]))
            new_end_iso   = _make_local_iso(date, _slot_to_hhmm(group[-1] + 1))
            logger.info("group %d: %s – %s", g_idx, new_start_iso, new_end_iso)

            # Перепланирование SOFT конфликтов для этой группы
            if soft_events:
                try:
                    actions = reschedule_svc.compute_reschedule(new_start_iso, new_end_iso, soft_events)
                except Exception as _re:
                    logger.error("compute_reschedule failed: %s", _re)
                    actions = []
                for action in actions:
                    ev    = action["event"]
                    ev_id = ev.get("id", "")
                    if ev_id in rescheduled_ids:
                        continue
                    rescheduled_ids.add(ev_id)
                    title = ev.get("title", "?")
                    try:
                        if action["type"] == "update":
                            ns, ne = action["new_start"], action["new_end"]
                            await cal_svc.update_event(ev_id, {"start": ns, "end": ne})
                            result_lines.append(
                                f"🔄 *{title}* → {_to_local_hhmm(ns)}–{_to_local_hhmm(ne)}"
                            )
                        elif action["type"] == "split":
                            p1s, p1e = action["part1_start"], action["part1_end"]
                            p2s, p2e = action["part2_start"], action["part2_end"]
                            await cal_svc.update_event(ev_id, {"start": p1s, "end": p1e})
                            await cal_svc.create_event(
                                title=title, start=p2s, end=p2e,
                                description=ev.get("description", ""),
                            )
                            result_lines.append(
                                f"✂️ *{title}* → {_to_local_hhmm(p1s)}–{_to_local_hhmm(p1e)}"
                                f" и {_to_local_hhmm(p2s)}–{_to_local_hhmm(p2e)}"
                            )
                    except Exception as e:
                        logger.error("Ошибка перепланирования '%s': %s", title, e)
                        result_lines.append(f"⚠️ Не удалось перенести *{title}*: {e}")

            # Создаём задачу/событие для этой группы
            pending = copy.deepcopy(orig_pending)
            mod_tools = copy.deepcopy(orig_tools)

            suffix = f" {g_idx + 1}" if len(groups) > 1 else ""
            for t in mod_tools:
                if t["tool_name"] == "create_task":
                    t["tool_args"]["start_time"] = new_start_iso
                    t["tool_args"]["end_time"]   = new_end_iso
                    if suffix:
                        t["tool_args"]["title"] = t["tool_args"].get("title", "") + suffix
                elif t["tool_name"] == "create_event":
                    t["tool_args"]["start"] = new_start_iso
                    t["tool_args"]["end"]   = new_end_iso
                    if suffix:
                        t["tool_args"]["title"] = t["tool_args"].get("title", "") + suffix

            if pending.get("tools"):
                pending["tools"] = mod_tools
            else:
                pending["tool_name"] = mod_tools[0]["tool_name"]
                pending["tool_args"] = mod_tools[0]["tool_args"]

            logger.info("executing pending tools: %s", [t["tool_name"] for t in mod_tools])
            try:
                task_result = await execute_pending_tool(pending)
                logger.info("task_result: %s", task_result[:100])
                result_lines.append(task_result)
            except Exception as e:
                logger.error("Ошибка создания задачи (группа %d): %s", g_idx, e)
                result_lines.append(f"❌ Ошибка создания задачи: {e}")

        final = "\n".join(result_lines) or "✅ Готово."
        await _send_result(final)

    except Exception as _master_err:
        logger.exception("Неожиданная ошибка в grid_confirm: %s", _master_err)
        await _send_result(f"❌ Внутренняя ошибка: {_master_err}")


@router.callback_query(F.data == "grid_cancel")
async def handle_grid_cancel(callback: CallbackQuery) -> None:
    _grid_sessions.pop(callback.from_user.id, None)
    await callback.answer()
    try:
        await callback.message.edit_text(
            callback.message.text + "\n\n❌ Отменено.",
            reply_markup=None,
        )
    except Exception:
        pass


# ─── Standard confirm callback ─────────────────────────────────────────────────

@router.callback_query(F.data.startswith("confirm:"))
async def handle_confirmation(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    answer  = callback.data.split(":")[1]

    await callback.answer()

    if answer == "no":
        _pending_confirmations.pop(user_id, None)
        try:
            await callback.message.edit_text(
                callback.message.text + "\n\n❌ Отменено.",
                reply_markup=None,
            )
        except Exception:
            pass
        return

    pending = _pending_confirmations.pop(user_id, None)
    if not pending:
        await callback.message.edit_text(
            "❌ Сессия подтверждения истекла. Повторите запрос.",
            reply_markup=None,
        )
        return

    try:
        await callback.message.edit_text(
            callback.message.text + "\n\n⏳ Выполняю...",
            reply_markup=None,
        )
    except Exception:
        pass

    try:
        result = await execute_pending_tool(pending)
        try:
            await callback.message.answer(result, parse_mode="Markdown")
        except Exception:
            await callback.message.answer(result, parse_mode=None)
    except Exception as e:
        logger.error("Ошибка выполнения подтверждённого действия: %s", e)
        if isinstance(e, TokenExpiredError) or "invalid_grant" in str(e):
            await send_token_expired(callback.message)
        else:
            await callback.message.answer(f"❌ Ошибка при выполнении: {e}")


# ─── Snooze callback ───────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("snooze:"))
async def handle_snooze(callback: CallbackQuery) -> None:
    from datetime import datetime, timedelta

    await callback.answer("Откладываю...")

    parts = callback.data.split(":")
    if len(parts) < 3:
        return

    task_id      = parts[1]
    snooze_value = parts[2]

    now = datetime.now(_app_tz())
    if snooze_value == "30":
        new_due = now + timedelta(minutes=30)
    elif snooze_value == "60":
        new_due = now + timedelta(hours=1)
    elif snooze_value == "tomorrow":
        tomorrow = now + timedelta(days=1)
        new_due  = tomorrow.replace(hour=9, minute=0, second=0, microsecond=0)
    else:
        return

    try:
        await tasks_svc.update_task(task_id, {"due": new_due.isoformat()})
        time_str = new_due.strftime("%d.%m %H:%M")
        await callback.message.edit_text(
            callback.message.text + f"\n\n⏰ *Отложено до {time_str}*",
            parse_mode="Markdown",
            reply_markup=None,
        )
    except Exception as e:
        logger.error("Ошибка snooze задачи %s: %s", task_id, e)
        await callback.message.answer(f"❌ Ошибка при откладывании: {e}")


# ─── Text message handler ──────────────────────────────────────────────────────

@router.message()
async def handle_text_message(message: Message) -> None:
    if not message.text:
        return

    user_id   = message.from_user.id
    user_text = message.text.strip()
    if not user_text:
        return

    _pending_confirmations.pop(user_id, None)
    _grid_sessions.pop(user_id, None)

    thinking_msg = await message.answer("🤔 Думаю...")

    try:
        response = await asyncio.wait_for(run_agent(user_id, user_text), timeout=90.0)
    except asyncio.TimeoutError:
        logger.error("Таймаут агента для user_id=%s", user_id)
        await thinking_msg.delete()
        await message.answer("⏱ Запрос занял слишком долго. Попробуй ещё раз или /clear для сброса истории.")
        return
    except Exception as e:
        logger.error("Ошибка агента для user_id=%s: %s", user_id, e)
        await thinking_msg.delete()
        if isinstance(e, TokenExpiredError) or "invalid_grant" in str(e):
            await send_token_expired(message)
        else:
            await message.answer(
                f"❌ Произошла ошибка при обработке запроса: {e}\n\nПопробуй ещё раз или /clear для сброса истории."
            )
        return

    try:
        await thinking_msg.delete()
    except Exception:
        pass

    await handle_agent_response(message, response, user_id)


# ─── Describe helpers ──────────────────────────────────────────────────────────

_MONTHS_RU = [
    "янв", "фев", "мар", "апр", "мая", "июн",
    "июл", "авг", "сен", "окт", "ноя", "дек",
]
_WEEKDAYS_RU = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def _fmt_iso(iso: str) -> str:
    try:
        from datetime import date as _date
        date_str, time = iso[:10], iso[11:16]
        year, month, day = date_str.split("-")
        m   = _MONTHS_RU[int(month) - 1]
        wd  = _WEEKDAYS_RU[_date(int(year), int(month), int(day)).weekday()]
        if time and time != "00:00":
            return f"{int(day)} {m} ({wd}) {time}"
        return f"{int(day)} {m} ({wd})"
    except Exception:
        return iso[:16].replace("T", " ")


def _fmt_fields(fields: dict) -> str:
    lines = []
    for key, val in fields.items():
        label = {"title": "название", "start": "начало", "end": "конец",
                 "description": "описание", "due": "дедлайн",
                 "start_time": "начало", "end_time": "конец"}.get(key, key)
        formatted = _fmt_iso(str(val)) if key in ("start", "end", "due", "start_time", "end_time") else str(val)
        lines.append(f"  • {label}: {formatted}")
    return "\n".join(lines)


def _describe_bulk_create(events: list) -> str:
    def _fmt_dt(iso: str) -> str:
        try:
            parts = iso[:10].split("-")
            return f"{parts[2]}.{parts[1]} {iso[11:16]}"
        except Exception:
            return iso[:16]

    total = len(events)
    noun = "событие" if total == 1 else ("события" if total < 5 else "событий")
    lines = [f"Создать *{total} {noun}* в Google Calendar:"]
    for ev in events:
        title    = ev.get("title", "?")
        start    = ev.get("start", "")
        end      = ev.get("end", "")
        rrule    = ev.get("recurrence", [])
        reminder = ev.get("reminder_minutes")
        desc     = ev.get("description", "")

        end_time     = end[11:16] if len(end) >= 16 else end
        reminder_str = f", 🔔{reminder}м" if reminder is not None else ""
        desc_str     = f", {desc}" if desc else ""

        if rrule:
            rule_str = rrule[0].replace("RRULE:", "")
            lines.append(f"• *{title}* с {_fmt_dt(start)} до {end_time} ({rule_str}){desc_str}{reminder_str}")
        else:
            lines.append(f"• *{title}* {_fmt_dt(start)}–{end_time}{desc_str}{reminder_str}")
    return "\n".join(lines)


def _describe_tool_action(tool_name: str, tool_args: dict) -> str:
    if tool_name == "bulk_create_events":
        return _describe_bulk_create(tool_args.get("events", []))

    _VERBS = {
        "create_event":   "Создать событие",
        "update_event":   "Обновить событие",
        "delete_event":   "Удалить событие",
        "create_task":    "Создать задачу",
        "complete_task":  "Отметить выполненной",
        "delete_task":    "Удалить задачу",
        "update_task":    "Обновить задачу",
    }
    verb = _VERBS.get(tool_name, f"Выполнить: {tool_name}")

    # Изменяемые поля лежат в "fields" для update_*, иначе — в корне args
    fields = tool_args.get("fields", {})

    def _get(*keys):
        for k in keys:
            v = tool_args.get(k) or fields.get(k)
            if v:
                return v
        return None

    is_event = tool_name in ("create_event", "update_event", "delete_event")
    is_task  = tool_name in ("create_task", "update_task", "delete_task", "complete_task")

    event_title = _get("title", "event_title") if is_event else None
    task_title  = _get("task_title", "title")  if is_task  else None

    start    = _get("start", "start_time")
    end      = _get("end",   "end_time")
    due      = _get("due")
    event_id = _get("event_id")
    task_id  = _get("task_id")
    desc     = _get("description")

    # Первая строка — глагол + главное имя
    primary = event_title or task_title or ""
    lines   = [f"{verb}: *{primary}*"]

    # Порядок: событие / задача / начало / конец / дедлайн / IDs / описание
    if event_title and task_title:          # create_task создаёт и событие и задачу
        lines.append(f"Событие: {event_title}")
        lines.append(f"Задача: {task_title}")
    if start:
        lines.append(f"Начало: {_fmt_iso(start)}")
    if end:
        lines.append(f"Конец: {_fmt_iso(end)}")
    if due:
        lines.append(f"Дедлайн: {_fmt_iso(due)}")
    ids = []
    if event_id:
        ids.append(f"event {event_id}")
    if task_id:
        ids.append(f"task {task_id}")
    if ids:
        lines.append(f"ID: {', '.join(ids)}")
    if desc:
        lines.append(f"Описание: {desc}")

    # Доп. поля только для create_event
    if tool_name == "create_event":
        if tool_args.get("recurrence"):
            lines.append(f"Повторение: {tool_args['recurrence'][0].replace('RRULE:', '')}")
        if tool_args.get("reminder_minutes") is not None:
            lines.append(f"Напоминание: за {tool_args['reminder_minutes']} мин")

    return "\n".join(lines)
