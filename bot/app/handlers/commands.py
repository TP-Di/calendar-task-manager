"""
Обработчики команд: /start /help /status /load /done /postpone /clear /heatmap
"""

import io
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, KeyboardButton, Message, ReplyKeyboardMarkup

from app.config import config
from app.db.database import clear_history
from app.services.calendar import TokenExpiredError
import app.services.calendar as cal
import app.services.tasks as tasks_svc

logger = logging.getLogger(__name__)
router = Router()


async def _handle_error(message: Message, e: Exception) -> None:
    """Обрабатывает ошибку: если это истёкший токен — шлёт ссылку реавторизации, иначе — безопасное сообщение."""
    if isinstance(e, TokenExpiredError) or "invalid_grant" in str(e):
        await send_token_expired(message)
    else:
        logger.error("Команда вернула ошибку: %s", e, exc_info=True)
        await message.answer("❌ Произошла ошибка. Попробуй ещё раз.")


async def send_token_expired(message: Message) -> None:
    """Отправляет сообщение с ссылкой для повторной авторизации Google."""
    try:
        user_id = message.from_user.id if message.from_user else 0
        auth_url = cal.get_auth_url(user_id)
        text = (
            "🔑 *Google токен истёк или был отозван*\n\n"
            "Для повторной авторизации:\n"
            f"1\\. Перейди по ссылке: [Авторизоваться в Google]({auth_url})\n"
            "2\\. Разреши доступ и скопируй код\n"
            "3\\. Отправь боту: `/auth_code КОД`"
        )
        await message.answer(text, parse_mode="MarkdownV2", disable_web_page_preview=True)
    except Exception as e:
        logger.error("Ошибка генерации auth URL: %s", e)
        await message.answer("❌ Google токен истёк. Переменная GOOGLE_CREDENTIALS_JSON не задана или недействительна.")

MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📊 Статус"), KeyboardButton(text="📅 Нагрузка")],
        [KeyboardButton(text="🗓 Что сегодня?"), KeyboardButton(text="📋 Задачи")],
    ],
    resize_keyboard=True,
    input_field_placeholder="Напиши или выбери действие...",
)


async def _generate_heatmap_image(
    events: list, tz_str: str, days: int = 7, week_start: "datetime | None" = None
) -> bytes:
    """Генерирует PNG: тепловая карта расписания + pie chart нагрузки по категориям."""
    import re
    from collections import defaultdict

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.patches as mpatches
    import matplotlib.pyplot as plt
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(tz_str)
    now = datetime.now(tz)
    start_day = week_start if week_start is not None else now.replace(hour=0, minute=0, second=0, microsecond=0)

    HOUR_START, HOUR_END = 6, 24
    N = HOUR_END - HOUR_START  # 18 часов

    BG, AX, GRID, TEXT = "#0d1117", "#161b22", "#30363d", "#e6edf3"
    RED_FC, RED_EC = "#f85149", "#da3633"
    YEL_FC, YEL_EC = "#e3b341", "#d29922"
    PIE_COLORS = ["#58a6ff", "#f85149", "#e3b341", "#3fb950", "#bc8cff", "#ff7b72", "#79c0ff", "#d2a8ff"]

    # --- Считаем нагрузку по дням и категориям ---
    day_hours: dict[int, float] = defaultdict(float)
    cat_hours: dict[str, float] = defaultdict(float)

    def _clean_title(t: str) -> str:
        t = re.sub(r"\[(HARD|SOFT|PRIORITY:[^\]]+|DEPENDS:[^\]]+)\]", "", t)
        return t.strip() or "Без названия"

    for ev in events:
        s, e = ev.get("start", ""), ev.get("end", "")
        if not s or "T" not in s:
            continue
        try:
            sd = datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(tz)
            ed = datetime.fromisoformat(e.replace("Z", "+00:00")).astimezone(tz)
        except Exception:
            continue
        hrs = (ed - sd).total_seconds() / 3600
        di = (sd.date() - start_day.date()).days
        if 0 <= di < days:
            day_hours[di] += hrs
        cat_hours[_clean_title(ev.get("title", ""))] += hrs

    # --- Компоновка: heatmap слева (3/4), pie справа (1/4) ---
    fig = plt.figure(figsize=(19, 10))
    gs = fig.add_gridspec(1, 2, width_ratios=[3.5, 1.2], wspace=0.06)
    ax = fig.add_subplot(gs[0])
    ax2 = fig.add_subplot(gs[1])
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(AX)
    ax2.set_facecolor(BG)

    # === HEATMAP ===
    for h in range(N + 1):
        lw = 1.0 if h % 3 == 0 else 0.35
        ax.axhline(h, color=GRID, linewidth=lw, zorder=1)
    for d in range(days + 1):
        ax.axvline(d, color=GRID, linewidth=0.9, zorder=1)

    for ev in events:
        s, e = ev.get("start", ""), ev.get("end", "")
        if not s or "T" not in s:
            continue
        try:
            sd = datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(tz)
            ed = datetime.fromisoformat(e.replace("Z", "+00:00")).astimezone(tz)
        except Exception:
            continue
        di = (sd.date() - start_day.date()).days
        if di < 0 or di >= days:
            continue
        ys = (sd.hour + sd.minute / 60) - HOUR_START
        ye = (ed.hour + ed.minute / 60) - HOUR_START
        if ye <= 0 or ys >= N:
            continue
        ys, ye = max(ys, 0.0), min(ye, float(N))
        h_rect = ye - ys
        title = ev.get("title", "")
        desc = ev.get("description", "") or ""
        is_soft = "[SOFT]" in title or "[SOFT]" in desc
        fc, ec = (YEL_FC, YEL_EC) if is_soft else (RED_FC, RED_EC)
        ax.add_patch(plt.Rectangle(
            (di + 0.06, ys), 0.88, h_rect,
            facecolor=fc, edgecolor=ec, linewidth=1.3, alpha=0.88, zorder=2,
        ))
        if h_rect >= 0.45:
            label = _clean_title(title)
            label = label[:14] + "…" if len(label) > 16 else label
            ax.text(
                di + 0.5, ys + h_rect / 2, label,
                ha="center", va="center",
                fontsize=6.5, color="#0d1117", fontweight="bold",
                zorder=3, clip_on=True,
            )

    # Нагрузка в часах над каждым днём
    for i in range(days):
        h = day_hours.get(i, 0.0)
        if h > 0:
            ax.text(
                i + 0.5, -0.55, f"{h:.1f}ч",
                ha="center", va="center",
                fontsize=8.5, color="#e3b341", fontweight="bold", zorder=5,
            )

    ax.set_ylim(N, 0)
    ax.set_xlim(0, days)
    ax.set_yticks(range(N + 1))
    ax.set_yticklabels([f"{HOUR_START + h:02d}:00" for h in range(N + 1)], fontsize=8, color=TEXT)

    day_labels = []
    for i in range(days):
        d = start_day + timedelta(days=i)
        prefix = "▶ " if d.date() == now.date() else ""
        day_labels.append(prefix + d.strftime("%a\n%d.%m"))
    ax.set_xticks([i + 0.5 for i in range(days)])
    ax.set_xticklabels(day_labels, fontsize=9, color=TEXT)
    ax.tick_params(axis="both", which="both", length=0, pad=14)
    for sp in ax.spines.values():
        sp.set_color(GRID)

    # Линия текущего времени
    today_idx = (now.date() - start_day.date()).days
    if 0 <= today_idx < days:
        cur_h = now.hour + now.minute / 60 - HOUR_START
        if 0 <= cur_h <= N:
            ax.plot(
                [today_idx, today_idx + 1], [cur_h, cur_h],
                color="#3fb950", linewidth=2.2, linestyle="--", zorder=4, alpha=0.9,
            )

    ax.legend(
        handles=[
            mpatches.Patch(facecolor=RED_FC, edgecolor=RED_EC, label="Нельзя перенести"),
            mpatches.Patch(facecolor=YEL_FC, edgecolor=YEL_EC, label="Можно перенести"),
        ],
        loc="upper right",
        facecolor="#161b22", edgecolor=GRID,
        labelcolor=TEXT, fontsize=8.5, framealpha=0.95,
    )
    ax.set_title("Расписание на неделю", color=TEXT, fontsize=13, fontweight="bold", pad=22)

    # === PIE CHART — на что трачу больше времени ===
    sorted_cats = sorted(cat_hours.items(), key=lambda x: x[1], reverse=True)
    TOP = 7
    if len(sorted_cats) > TOP:
        top = sorted_cats[:TOP]
        other = sum(h for _, h in sorted_cats[TOP:])
        if other > 0.05:
            top.append(("Другое", other))
    else:
        top = sorted_cats

    if top:
        labels = [t for t, _ in top]
        sizes = [h for _, h in top]
        total = sum(sizes)
        colors = PIE_COLORS[:len(labels)]

        wedges, texts, autotexts = ax2.pie(
            sizes,
            labels=None,
            colors=colors,
            autopct=lambda p: f"{p * total / 100:.1f}ч" if p > 4 else "",
            startangle=140,
            pctdistance=0.72,
            wedgeprops={"linewidth": 1.2, "edgecolor": BG},
        )
        for at in autotexts:
            at.set_color("#0d1117")
            at.set_fontsize(7.5)
            at.set_fontweight("bold")

        # Легенда pie
        legend_labels = [f"{l}  {h:.1f}ч" for l, h in zip(labels, sizes)]
        ax2.legend(
            wedges, legend_labels,
            loc="lower center",
            bbox_to_anchor=(0.5, -0.38),
            facecolor="#161b22", edgecolor=GRID,
            labelcolor=TEXT, fontsize=7.5,
            framealpha=0.95,
            ncol=1,
        )
        ax2.set_title(f"Нагрузка\n{total:.1f}ч итого", color=TEXT, fontsize=10, fontweight="bold", pad=8)
    else:
        ax2.text(0.5, 0.5, "Нет событий", ha="center", va="center", color=TEXT, fontsize=10)
        ax2.axis("off")

    plt.tight_layout(pad=1.5)
    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=150, bbox_inches="tight", facecolor=BG, edgecolor="none")
    plt.close(fig)
    buf.seek(0)
    return buf.read()


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    """Приветственное сообщение."""
    await message.answer(
        "👋 Привет! Я твой персональный планировщик.\n\n"
        "Просто напиши что нужно сделать — добавить занятие, перенести встречу, посмотреть расписание.",
        parse_mode=None,
        reply_markup=MAIN_KB,
    )


@router.message(F.text == "📊 Статус")
async def btn_status(message: Message) -> None:
    await cmd_status(message)


@router.message(F.text == "📅 Нагрузка")
async def btn_heatmap(message: Message) -> None:
    await cmd_heatmap(message)


@router.message(F.text == "🗓 Что сегодня?")
async def btn_today(message: Message) -> None:
    """Показывает события на сегодня."""
    now = datetime.now(ZoneInfo(config.TIMEZONE))
    day_end = now.replace(hour=23, minute=59, second=59)
    try:
        events = await cal.get_events(now.isoformat(), day_end.isoformat())
    except Exception as e:
        await _handle_error(message, e)
        return
    if not events:
        await message.answer("На сегодня событий нет ✅")
        return
    lines = ["*📅 Сегодня:*"]
    for ev in events:
        start = ev.get("start", "")
        time_str = ""
        if "T" in start:
            try:
                dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
                time_str = dt.strftime("%H:%M") + " "
            except Exception:
                pass
        lines.append(f"  • {time_str}{ev.get('title', '')}")
    await message.answer("\n".join(lines), parse_mode="Markdown")


@router.message(F.text == "📋 Задачи")
async def btn_tasks(message: Message) -> None:
    """Показывает активные задачи."""
    try:
        tasks = await tasks_svc.get_tasks()
    except Exception as e:
        await _handle_error(message, e)
        return
    if not tasks:
        await message.answer("Активных задач нет ✅")
        return
    now = datetime.now(ZoneInfo(config.TIMEZONE))
    lines = ["*📋 Активные задачи:*"]
    for t in tasks[:15]:
        due = t.get("due", "")
        due_str = ""
        prefix = "  •"
        if due:
            try:
                due_dt = datetime.fromisoformat(due.replace("Z", "+00:00"))
                due_str = f" → {due_dt.strftime('%d.%m')}"
                if due_dt < now:
                    prefix = "  ⚠️"
            except Exception:
                pass
        lines.append(f"{prefix} {t['title']}{due_str}")
    await message.answer("\n".join(lines), parse_mode="Markdown")


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    """Список доступных команд."""
    text = (
        "📖 *Справка:*\n\n"
        "Просто пиши что нужно — я пойму без команд.\n\n"
        "*Кнопки:*\n"
        "📊 Статус — задачи и события на 3 дня\n"
        "📅 Нагрузка — визуальный график расписания на неделю\n"
        "🗓 Что сегодня? — события на сегодня\n"
        "📋 Задачи — список активных задач\n\n"
        "*Команды:*\n"
        "/upload — загрузить PDF с расписанием\n"
        "/clear — сбросить историю диалога"
    )
    await message.answer(text, parse_mode="Markdown", reply_markup=MAIN_KB)


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    """Показывает активные задачи и ближайшие события."""
    user_id = message.from_user.id
    await message.answer("⏳ Загружаю данные...")

    now = datetime.now(ZoneInfo(config.TIMEZONE))
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    three_days_later = today_start + timedelta(days=3)

    lines = ["📊 *Текущий статус*\n"]

    # Ближайшие события
    try:
        events = await cal.get_events(now.isoformat(), three_days_later.isoformat())
        lines.append("*📅 Ближайшие события (3 дня):*")
        if events:
            for ev in events[:8]:
                start = ev.get("start", "")
                time_str = ""
                if "T" in start:
                    try:
                        dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
                        time_str = dt.strftime("%d.%m %H:%M")
                    except Exception:
                        time_str = start
                title = ev.get("title", "")
                lines.append(f"  • {time_str} — {title}")
        else:
            lines.append("  Нет событий")
    except Exception as e:
        logger.error("Ошибка Calendar API (/status): %s", e)
        lines.append("  ❌ Ошибка загрузки событий")

    # Активные задачи
    lines.append("")
    try:
        tasks = await tasks_svc.get_tasks()
        lines.append("*📋 Активные задачи:*")
        if tasks:
            # Сортируем: просроченные в топе
            overdue = []
            normal = []
            for t in tasks:
                due = t.get("due", "")
                if due:
                    try:
                        due_dt = datetime.fromisoformat(due.replace("Z", "+00:00"))
                        if due_dt < now:
                            overdue.append(t)
                            continue
                    except Exception:
                        pass
                normal.append(t)

            for t in overdue:
                lines.append(f"  ⚠️ {t['title']}")
            for t in normal[:10]:
                due = t.get("due", "")
                due_str = ""
                if due:
                    try:
                        due_dt = datetime.fromisoformat(due.replace("Z", "+00:00"))
                        due_str = f" → {due_dt.strftime('%d.%m')}"
                    except Exception:
                        pass
                lines.append(f"  • {t['title']}{due_str}")
        else:
            lines.append("  Нет активных задач ✅")
    except Exception as e:
        logger.error("Ошибка Tasks API (/status): %s", e)
        lines.append("  ❌ Ошибка загрузки задач")

    await message.answer("\n".join(lines), parse_mode="Markdown")


@router.message(Command("load"))
async def cmd_load(message: Message) -> None:
    """Показывает нагрузку (часов событий) на текущую неделю."""
    await message.answer("⏳ Считаю нагрузку...")

    now = datetime.now(ZoneInfo(config.TIMEZONE))
    # Начало текущей недели (понедельник)
    week_start = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    week_end = week_start + timedelta(days=7)

    try:
        events = await cal.get_events(week_start.isoformat(), week_end.isoformat())
    except Exception as e:
        logger.error("Ошибка Calendar API (/load): %s", e)
        await message.answer("❌ Ошибка загрузки событий из Calendar")
        return

    # Считаем часы по дням
    days_load: dict[int, float] = {i: 0.0 for i in range(7)}
    total_hours = 0.0

    for ev in events:
        start_str = ev.get("start", "")
        end_str = ev.get("end", "")
        if "T" not in start_str or "T" not in end_str:
            continue
        try:
            start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
            duration = (end_dt - start_dt).total_seconds() / 3600
            day_idx = (start_dt.weekday())  # 0=пн
            days_load[day_idx] = days_load.get(day_idx, 0.0) + duration
            total_hours += duration
        except Exception:
            continue

    day_names = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    lines = [
        f"📊 *Нагрузка на неделю ({week_start.strftime('%d.%m')} – {(week_end - timedelta(days=1)).strftime('%d.%m')}):*\n"
    ]
    for i, name in enumerate(day_names):
        hours = days_load.get(i, 0.0)
        bar = "█" * int(hours / 2) if hours > 0 else "·"
        lines.append(f"  {name}: {hours:.1f}ч {bar}")

    lines.append(f"\n*Итого:* {total_hours:.1f}ч за неделю")
    lines.append(f"*Событий:* {len(events)}")

    await message.answer("\n".join(lines), parse_mode="Markdown")


@router.message(Command("done"))
async def cmd_done(message: Message) -> None:
    """Отмечает задачу выполненной по частичному совпадению названия."""
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer(
            "Укажи название задачи: /done Сдать отчёт"
        )
        return

    query = args[1].strip().lower()

    try:
        tasks = await tasks_svc.get_tasks()
    except Exception as e:
        logger.error("Ошибка Tasks API (/done): %s", e)
        await message.answer("❌ Ошибка загрузки задач")
        return

    matches = [t for t in tasks if query in t.get("title", "").lower()]

    if not matches:
        await message.answer(f'Задача "{args[1]}" не найдена среди активных.')
        return

    if len(matches) > 1:
        names = "\n".join(f"  • {t['title']}" for t in matches[:5])
        await message.answer(
            f"Найдено несколько задач, уточни название:\n{names}"
        )
        return

    task = matches[0]
    try:
        await tasks_svc.complete_task(task["id"])
        await message.answer(f"✅ Задача выполнена: *{task['title']}*", parse_mode="Markdown")
    except Exception as e:
        logger.error("Ошибка Tasks API (complete_task): %s", e)
        await message.answer(f"❌ Ошибка при выполнении задачи: {e}")


@router.message(Command("postpone"))
async def cmd_postpone(message: Message) -> None:
    """
    Откладывает задачу. Формат: /postpone Название задачи 2024-01-20
    Делегирует агенту для интерпретации времени.
    """
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer(
            "Укажи название и новое время: /postpone Сдать отчёт завтра в 18:00"
        )
        return

    # Делегируем агенту
    from app.services.agent import run_agent

    user_id = message.from_user.id
    prompt = f"Отложи задачу: {args[1]}"

    await message.answer("⏳ Обрабатываю запрос...")
    response = await run_agent(user_id, prompt)

    from app.handlers.messages import handle_agent_response
    await handle_agent_response(message, response, user_id)


@router.message(Command("clear"))
async def cmd_clear(message: Message) -> None:
    """Очищает историю диалога пользователя."""
    user_id = message.from_user.id
    await clear_history(user_id)
    await message.answer(
        "🗑 История диалога очищена. Начинаем с чистого листа!"
    )


@router.message(Command("settings"))
async def cmd_settings(message: Message) -> None:
    """Открывает интерактивное меню настроек."""
    from app.handlers.settings import send_settings_menu
    await send_settings_menu(message)


@router.message(Command("reauth"))
async def cmd_reauth(message: Message) -> None:
    """Генерирует ссылку для повторной авторизации Google."""
    await send_token_expired(message)


@router.message(Command("auth_code"))
async def cmd_auth_code(message: Message) -> None:
    """Принимает код авторизации Google и сохраняет токен."""
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer("Использование: `/auth_code КОД`", parse_mode="Markdown")
        return
    code = parts[1].strip()
    user_id = message.from_user.id
    try:
        cal.complete_auth(code, user_id)
        await message.answer("✅ Авторизация выполнена успешно\\! Google Calendar и Tasks снова доступны\\.", parse_mode="MarkdownV2")
    except Exception as e:
        logger.error("Ошибка при обмене кода авторизации: %s", e)
        await message.answer("❌ Ошибка авторизации. Проверь код и попробуй снова через /reauth", parse_mode="Markdown")


@router.message(Command("heatmap"))
async def cmd_heatmap(message: Message) -> None:
    """Расписание: сегодня + 6 дней вперёд."""
    tz = ZoneInfo(config.TIMEZONE)
    now_local = datetime.now(tz)
    today = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    fetch_start = today
    fetch_end   = today + timedelta(days=7)

    await message.answer("⏳ Строю тепловую карту...")

    try:
        events = await cal.get_events(fetch_start.isoformat(), fetch_end.isoformat())
    except Exception as e:
        logger.error("Ошибка Calendar API (/heatmap): %s", e)
        await message.answer("❌ Ошибка загрузки событий из Calendar")
        return

    try:
        img_bytes = await _generate_heatmap_image(
            events, config.TIMEZONE, week_start=fetch_start
        )
    except Exception as e:
        logger.error("Ошибка генерации heatmap: %s", e)
        await message.answer(f"❌ Ошибка генерации графика: {e}")
        return

    photo = BufferedInputFile(img_bytes, filename="heatmap.png")
    date_range = f"{fetch_start.strftime('%d.%m')} – {(fetch_end - timedelta(days=1)).strftime('%d.%m')}"
    await message.answer_photo(
        photo,
        caption=f"📊 Расписание: {date_range}\n🔴 нельзя перенести · 🟡 можно перенести · — сейчас",
    )
