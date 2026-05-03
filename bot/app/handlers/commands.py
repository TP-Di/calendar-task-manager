"""
Обработчики команд: /start /help /status /load /done /postpone /clear /heatmap
"""

import asyncio
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
from app.services import categorize as cat
from app.services import timeline as tl

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
            f"1\\. Перейди по [этой ссылке]({auth_url}) и разреши доступ\\. "
            "Поставь галочки на ОБА scope: Calendar и Tasks\\.\n"
            "2\\. Браузер перебросит на `http://localhost/...` — страница НЕ откроется "
            "\\(это нормально\\)\\.\n"
            "3\\. Скопируй ВСЮ ссылку из адресной строки и пришли её боту:\n"
            "`/auth_code <ссылка>`\n\n"
            "_Если запутался \\— /auth\\_cancel сбросит сеанс, потом заново /reauth\\._"
        )
        await message.answer(text, parse_mode="MarkdownV2", disable_web_page_preview=True)
    except Exception as e:
        logger.error("Ошибка генерации auth URL: %s", e)
        await message.answer("❌ Google токен истёк. Переменная GOOGLE_CREDENTIALS_JSON не задана или недействительна.")

MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(text="📊 Статус"),
            KeyboardButton(text="🗓 Сегодня"),
            KeyboardButton(text="📅 Нагрузка"),
        ],
    ],
    resize_keyboard=True,
    input_field_placeholder="Напиши или выбери действие...",
)


async def _generate_heatmap_image(
    events: list, tz_str: str, days: int = 7,
    week_start: "datetime | None" = None,
    tasks: list | None = None,
) -> bytes:
    """Wrapper, гарантирующий закрытие matplotlib figures даже при исключении."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    try:
        return await _generate_heatmap_impl(events, tz_str, days, week_start, tasks)
    finally:
        # Закрываем ВСЕ незакрытые figures (utility cleanup) на случай если impl
        # упал между plt.figure() и plt.close().
        plt.close("all")


async def _generate_heatmap_impl(
    events: list, tz_str: str, days: int = 7,
    week_start: "datetime | None" = None,
    tasks: list | None = None,
) -> bytes:
    """
    PNG: heatmap расписания + pie нагрузки.
    - Адаптивный Y-диапазон по реальным событиям, clamped в [HEATMAP_HOUR_MIN, MAX].
    - Цвета по категории ([CATEGORY:x]); HARD → толстая рамка, SOFT → тонкая.
    - Рутина (Дорога) → тонкая полоса справа от ячейки с иконкой 🚗.
    - Свободные окна ≥ MIN_FREE_WINDOW_HOURS в рабочие часы → бледно-зелёная заливка.
    - Pie: рутина — одна категория «🚗 Дорога», слайсы <1ч → «Другое», max 6.
    - Дедлайны задач — нижняя полоска под графиком.
    """
    from collections import defaultdict
    from textwrap import wrap as _wrap

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.patches as mpatches
    import matplotlib.pyplot as plt
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(tz_str)
    now = datetime.now(tz)
    start_day = week_start if week_start is not None else now.replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    tasks = tasks or []
    routine_pats = _routine_patterns()

    BG, AX, GRID, TEXT = "#0d1117", "#161b22", "#30363d", "#e6edf3"
    FREE_FC = "#3fb950"

    # ── Парсим и классифицируем события ─────────────────────────────────────
    parsed: list = []  # (di, sd, ed, ev, is_routine, hours, is_hard)
    cat_hours_by_cat: dict[str, float] = defaultdict(float)
    routine_hours = 0.0
    day_hours: dict[int, float] = defaultdict(float)
    day_routine_hours: dict[int, float] = defaultdict(float)

    for ev in events:
        s, e = ev.get("start", ""), ev.get("end", "")
        if not s or "T" not in s:
            continue
        try:
            sd = tl.parse_iso_dt(s).astimezone(tz)
            ed = tl.parse_iso_dt(e).astimezone(tz)
        except Exception:
            continue
        di = (sd.date() - start_day.date()).days
        if di < 0 or di >= days:
            continue
        hrs = (ed - sd).total_seconds() / 3600
        is_rt = cat.is_routine(ev, routine_pats)
        desc_up = (ev.get("description") or "").upper()
        title_up = (ev.get("title") or "").upper()
        is_hard = "[HARD]" in desc_up or "[HARD]" in title_up
        parsed.append((di, sd, ed, ev, is_rt, hrs, is_hard))
        if is_rt:
            routine_hours += hrs
            day_routine_hours[di] += hrs
        else:
            day_hours[di] += hrs
            cat_hours_by_cat[cat.event_category(ev)] += hrs

    # ── Адаптивный Y-диапазон ───────────────────────────────────────────────
    HMIN, HMAX = config.HEATMAP_HOUR_MIN, config.HEATMAP_HOUR_MAX
    if parsed:
        min_h = min(p[1].hour + p[1].minute / 60 for p in parsed)
        max_h = max(p[2].hour + p[2].minute / 60 for p in parsed)
        HOUR_START = max(HMIN, int(min_h - 1))
        HOUR_END = min(HMAX, int(max_h + 1) + (1 if max_h % 1 else 0))
    else:
        HOUR_START, HOUR_END = HMIN, HMAX
    if HOUR_END <= HOUR_START:
        HOUR_START, HOUR_END = HMIN, HMAX
    N = HOUR_END - HOUR_START

    # ── Layout ──────────────────────────────────────────────────────────────
    fig = plt.figure(figsize=(19, 10))
    gs = fig.add_gridspec(1, 2, width_ratios=[3.5, 1.2], wspace=0.06)
    ax = fig.add_subplot(gs[0])
    ax2 = fig.add_subplot(gs[1])
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(AX)
    ax2.set_facecolor(BG)

    # Сетка
    for h in range(N + 1):
        lw = 1.0 if h % 3 == 0 else 0.35
        ax.axhline(h, color=GRID, linewidth=lw, zorder=1)
    for d in range(days + 1):
        ax.axvline(d, color=GRID, linewidth=0.9, zorder=1)

    # ── Подсветка свободных окон ────────────────────────────────────────────
    for d_idx in range(days):
        day_dt = start_day + timedelta(days=d_idx)
        day_start_dt = day_dt.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end_dt = day_start_dt + timedelta(days=1)
        day_evs = []
        for ev in events:
            s = ev.get("start", "")
            if "T" not in s:
                continue
            try:
                if tl.parse_iso_dt(s).astimezone(tz).date() == day_dt.date():
                    day_evs.append(ev)
            except Exception:
                continue
        try:
            windows = tl.find_free_windows(
                day_evs, day_start_dt, day_end_dt,
                config.WORK_HOUR_START, config.SLEEP_HOUR_START,
                config.MIN_FREE_WINDOW_HOURS,
            )
        except Exception:
            windows = []
        for ws, we in windows:
            ys = ws.hour + ws.minute / 60 - HOUR_START
            ye = we.hour + we.minute / 60 - HOUR_START
            if ye <= 0 or ys >= N:
                continue
            ys, ye = max(ys, 0.0), min(ye, float(N))
            ax.add_patch(plt.Rectangle(
                (d_idx + 0.06, ys), 0.88, ye - ys,
                facecolor=FREE_FC, edgecolor="none", alpha=0.10, zorder=1.5,
            ))

    # ── События ────────────────────────────────────────────────────────────
    legend_categories: set[str] = set()
    for di, sd, ed, ev, is_rt, hrs, is_hard in parsed:
        ys = sd.hour + sd.minute / 60 - HOUR_START
        ye = ed.hour + ed.minute / 60 - HOUR_START
        if ye <= 0 or ys >= N:
            continue
        ys, ye = max(ys, 0.0), min(ye, float(N))
        h_rect = ye - ys
        color = cat.event_color(ev)
        category = cat.event_category(ev)
        legend_categories.add(category)

        if is_rt:
            # Тонкая полоса справа + 🚗
            ax.add_patch(plt.Rectangle(
                (di + 0.86, ys), 0.10, h_rect,
                facecolor=color, edgecolor="none", alpha=0.85, zorder=2,
            ))
            if h_rect >= 0.7:
                ax.text(di + 0.91, ys + h_rect / 2, "🚗",
                        ha="center", va="center", fontsize=8, zorder=3)
        else:
            ec = "#ff7b72" if is_hard else "#222222"
            lw = 2.5 if is_hard else 1.0
            ax.add_patch(plt.Rectangle(
                (di + 0.06, ys), 0.78, h_rect,
                facecolor=color, edgecolor=ec, linewidth=lw, alpha=0.92, zorder=2,
            ))
            if h_rect >= 0.45:
                title = cat.clean_title(ev.get("title", ""))
                wrapped = _wrap(title, width=14)[:2]
                label = "\n".join(wrapped)
                ax.text(
                    di + 0.45, ys + h_rect / 2, label,
                    ha="center", va="center",
                    fontsize=6.5, color="#0d1117", fontweight="bold",
                    zorder=3, clip_on=True,
                )

    # Дневные тоталы — компактный формат: «3.0» или «3.0+0.5» (без эмодзи)
    for i in range(days):
        h = day_hours.get(i, 0.0)
        rt = day_routine_hours.get(i, 0.0)
        if h > 0 or rt > 0:
            txt = f"{h:.1f}+{rt:.1f}" if rt > 0.05 else f"{h:.1f}"
            ax.text(i + 0.5, -0.45, txt,
                    ha="center", va="center",
                    fontsize=8, color="#e3b341", fontweight="bold", zorder=5)

    # ── Дедлайны задач ──────────────────────────────────────────────────────
    deadline_strip_y = N + 0.7
    has_deadlines = False
    for t in tasks:
        due_dt = cat.parse_task_due(t.get("due", ""))
        if due_dt is None:
            continue
        try:
            due_local = due_dt.astimezone(tz)
        except Exception:
            continue
        di = (due_local.date() - start_day.date()).days
        if 0 <= di < days:
            has_deadlines = True
            title = cat.clean_title(t.get("title", ""))
            short = title[:12] + "…" if len(title) > 14 else title
            ax.text(
                di + 0.5, deadline_strip_y, f"📌 {short}",
                ha="center", va="center",
                fontsize=7, color="#bc8cff", fontweight="bold",
                zorder=5, clip_on=False,
            )
    if has_deadlines:
        ax.plot([0, days], [N + 0.4, N + 0.4], color=GRID, linewidth=0.6, zorder=4)

    # Y-axis
    ax.set_ylim(N + (1.2 if has_deadlines else 0), 0)
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

    # Линия «сейчас» с подписью
    today_idx = (now.date() - start_day.date()).days
    if 0 <= today_idx < days:
        cur_h = now.hour + now.minute / 60 - HOUR_START
        if 0 <= cur_h <= N:
            ax.plot([today_idx, today_idx + 1], [cur_h, cur_h],
                    color="#3fb950", linewidth=2.2, linestyle="--", zorder=4, alpha=0.9)
            ax.text(
                today_idx + 1.02, cur_h, f"← сейчас {now.strftime('%H:%M')}",
                ha="left", va="center",
                fontsize=8, color="#3fb950", fontweight="bold",
                zorder=5, clip_on=False,
            )

    # Легенда категорий
    legend_handles = []
    for c in [cc for cc in cat.KNOWN_CATEGORIES if cc in legend_categories]:
        legend_handles.append(
            mpatches.Patch(facecolor=cat.CATEGORY_COLORS[c][0], edgecolor=GRID, label=c)
        )
    if cat.UNKNOWN_CATEGORY in legend_categories:
        legend_handles.append(
            mpatches.Patch(
                facecolor=cat.CATEGORY_COLORS[cat.UNKNOWN_CATEGORY][0],
                edgecolor=GRID, label="без категории"
            )
        )
    if legend_handles:
        ax.legend(
            handles=legend_handles,
            loc="upper right", facecolor=AX, edgecolor=GRID,
            labelcolor=TEXT, fontsize=8.5, framealpha=0.95,
        )
    # Заголовок выше дневных итогов (pad=42 даёт зазор примерно в одну строку)
    ax.set_title("Расписание на неделю", color=TEXT, fontsize=13, fontweight="bold", pad=42)

    # ── PIE CHART ───────────────────────────────────────────────────────────
    pie_data: dict[str, float] = {}
    if routine_hours > 0.05:
        pie_data["🚗 Дорога"] = routine_hours
    for c, h in cat_hours_by_cat.items():
        if c == "дорога":
            pie_data["🚗 Дорога"] = pie_data.get("🚗 Дорога", 0) + h
            continue
        label = c.capitalize() if c != cat.UNKNOWN_CATEGORY else "Без категории"
        pie_data[label] = pie_data.get(label, 0) + h
    pie_data = {k: v for k, v in pie_data.items() if v > 0.01}

    sorted_pie = sorted(pie_data.items(), key=lambda x: x[1], reverse=True)
    threshold = 1.0
    big = [(k, v) for k, v in sorted_pie if v >= threshold]
    small_sum = sum(v for k, v in sorted_pie if v < threshold)
    if small_sum > 0.05:
        big.append(("Другое", small_sum))
    if len(big) > 6:
        rest = sum(v for _, v in big[6:])
        big = big[:6]
        if rest > 0.05:
            idx_other = next((i for i, (k, _) in enumerate(big) if k == "Другое"), -1)
            if idx_other >= 0:
                big[idx_other] = ("Другое", big[idx_other][1] + rest)
            else:
                big.append(("Другое", rest))

    def _pie_color(label: str) -> str:
        if label == "🚗 Дорога":
            return cat.CATEGORY_COLORS["дорога"][0]
        if label == "Другое":
            return "#525960"
        if label == "Без категории":
            return cat.CATEGORY_COLORS[cat.UNKNOWN_CATEGORY][0]
        c_low = label.lower()
        if c_low in cat.CATEGORY_COLORS:
            return cat.CATEGORY_COLORS[c_low][0]
        return "#525960"

    if big:
        labels = [k for k, _ in big]
        sizes = [v for _, v in big]
        total = sum(sizes)
        colors = [_pie_color(l) for l in labels]
        wedges, _, autotexts = ax2.pie(
            sizes, labels=None, colors=colors,
            autopct=lambda p: f"{p * total / 100:.1f}ч" if p > 5 else "",
            startangle=140, pctdistance=0.72,
            wedgeprops={"linewidth": 1.2, "edgecolor": BG},
        )
        for at in autotexts:
            at.set_color("#0d1117")
            at.set_fontsize(7.5)
            at.set_fontweight("bold")
        legend_labels = [f"{l}  {h:.1f}ч" for l, h in zip(labels, sizes)]
        ax2.legend(
            wedges, legend_labels,
            loc="lower center", bbox_to_anchor=(0.5, -0.38),
            facecolor=AX, edgecolor=GRID,
            labelcolor=TEXT, fontsize=7.5, framealpha=0.95, ncol=1,
        )
        per_week = config.WORK_HOURS_PER_WEEK or 0
        pct = (total / per_week * 100) if per_week > 0 else 0
        title2 = (
            f"Нагрузка\n{total:.1f}ч / {per_week}ч ({pct:.0f}%)"
            if per_week > 0 else f"Нагрузка\n{total:.1f}ч"
        )
        ax2.set_title(title2, color=TEXT, fontsize=10, fontweight="bold", pad=8)
    else:
        ax2.text(0.5, 0.5, "Нет событий", ha="center", va="center", color=TEXT, fontsize=10)
        ax2.axis("off")

    plt.tight_layout(pad=1.5)
    buf = io.BytesIO()
    # H8: dpi=100 для контроля размера. Telegram лимит 10 MB на photo;
    # если PNG > 9 MB — перерендер с dpi=70.
    plt.savefig(buf, format="png", dpi=100, bbox_inches="tight", facecolor=BG, edgecolor="none")
    if buf.tell() > 9_000_000:
        buf.seek(0)
        buf.truncate()
        plt.savefig(buf, format="png", dpi=70, bbox_inches="tight", facecolor=BG, edgecolor="none")
    plt.close(fig)
    buf.seek(0)
    return buf.read()


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    """Приветственное сообщение."""
    await message.answer(
        "👋 Привет! Я твой персональный планировщик.\n\n"
        "Просто напиши что нужно — добавить занятие, перенести встречу, посмотреть расписание.\n\n"
        "Кнопки внизу — быстрый доступ. /help — все команды. /settings — настройки.",
        parse_mode=None,
        reply_markup=MAIN_KB,
    )


@router.message(F.text == "📊 Статус")
async def btn_status(message: Message) -> None:
    await cmd_status(message)


@router.message(F.text == "📅 Нагрузка")
async def btn_heatmap(message: Message) -> None:
    await cmd_heatmap(message)


# ─── Shared helpers for text views ──────────────────────────────────────────


def _routine_patterns() -> list[str]:
    return [p.strip() for p in (config.ROUTINE_PATTERNS or "").split(",") if p.strip()]


def _format_event_line(ev: dict, tz) -> str:
    """  HH:MM[–HH:MM] — Название."""
    start_iso = ev.get("start", "")
    end_iso = ev.get("end", "")
    title = cat.clean_title(ev.get("title", ""))
    try:
        sd = tl.parse_iso_dt(start_iso).astimezone(tz)
        ed = tl.parse_iso_dt(end_iso).astimezone(tz)
        same_day_end = ed.date() == sd.date()
        if same_day_end:
            return f"  {sd.strftime('%H:%M')}–{ed.strftime('%H:%M')} — {title}"
        return f"  {sd.strftime('%H:%M')} — {title}"
    except Exception:
        return f"  {title}"


def _render_day_block(d, day_events: list[dict], today, tz) -> list[str]:
    """Рендерит '🗓 ДЕНЬ' + список событий + строку рутины."""
    label = tl.day_label(d, today)
    lines = [f"*🗓 {label}*"]
    non_routine, counts = cat.collapse_routines(day_events, _routine_patterns())
    if not non_routine and not counts:
        lines.append("  свободно")
        return lines
    for ev in non_routine:
        lines.append(_format_event_line(ev, tz))
    routine = cat.routine_summary_line(counts)
    if routine:
        lines.append(routine)
    return lines


def _render_tasks_with_urgency(
    tasks: list[dict], now: datetime, limit: int = 12, group_by_week: bool = False
) -> list[str]:
    """
    Сортирует по due, маркирует urgency-emoji, добавляет «через Nд».
    group_by_week=True → раскладывает по 'Эта неделя / След неделя / Позже'.
    """
    if not tasks:
        return ["  нет активных задач ✅"]

    enriched: list[tuple[str, int, dict, datetime | None]] = []  # (level, days_left, task, due_dt)
    for t in tasks:
        level, days_left = cat.task_urgency(
            t, now, config.URGENT_TASK_DAYS, config.WARM_TASK_DAYS
        )
        due_dt = cat.parse_task_due(t.get("due", ""))
        enriched.append((level, days_left, t, due_dt))

    # Сортировка: задачи без due — в конец; с due — по возрастанию
    def sort_key(item):
        level, days_left, t, due_dt = item
        # overdue первыми (отрицательный days_left), затем по due, затем без due
        if due_dt is None:
            return (1, 0)
        return (0, days_left)
    enriched.sort(key=sort_key)

    def task_line(level: str, days_left: int, t: dict, due_dt: datetime | None) -> str:
        emoji = cat.urgency_emoji(level)
        title = cat.clean_title(t.get("title", ""))
        if due_dt is not None:
            return f"  {emoji} {title} → {cat.format_due_human(due_dt, now)}"
        return f"  {emoji} {title}"

    if not group_by_week:
        return [task_line(*x) for x in enriched[:limit]]

    today = now.date()
    end_this_week = tl.end_of_iso_week(today)
    end_next_week = end_this_week + timedelta(days=7)

    sections: dict[str, list[str]] = {"🔥 Эта неделя": [], "📌 Следующая неделя": [], "📅 Позже": []}
    for level, days_left, t, due_dt in enriched[:limit]:
        line = task_line(level, days_left, t, due_dt)
        if due_dt is None:
            sections["📅 Позже"].append(line)
            continue
        d = due_dt.date()
        if d <= end_this_week:
            sections["🔥 Эта неделя"].append(line)
        elif d <= end_next_week:
            sections["📌 Следующая неделя"].append(line)
        else:
            sections["📅 Позже"].append(line)

    out: list[str] = []
    for header, items in sections.items():
        if items:
            out.append(f"\n*{header}*")
            out.extend(items)
    return out or ["  нет активных задач ✅"]


# ─── Buttons ────────────────────────────────────────────────────────────────


@router.message(F.text == "🗓 Сегодня")
async def btn_today(message: Message) -> None:
    """Сегодня: события + свободные окна + задачи на эту неделю + сводка."""
    tz = ZoneInfo(config.TIMEZONE)
    now = datetime.now(tz)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1) - timedelta(seconds=1)
    week_end = tl.end_of_iso_week_dt(now)

    # Параллельно тянем события и задачи
    try:
        events = await cal.get_events(day_start.isoformat(), day_end.isoformat())
    except Exception as e:
        await _handle_error(message, e)
        return
    try:
        tasks = await tasks_svc.get_tasks()
    except Exception as e:
        logger.warning("Tasks API failed for /today: %s", e)
        tasks = []

    label = tl.day_label(now.date(), now.date())
    weekday_short = tl.WEEKDAYS_SHORT_RU[now.weekday()]
    lines = [f"*🗓 {label}, {weekday_short}*\n"]

    # События
    non_routine, routine_counts = cat.collapse_routines(events, _routine_patterns())
    lines.append("*📅 События:*")
    if non_routine or routine_counts:
        for ev in non_routine:
            lines.append(_format_event_line(ev, tz))
        routine = cat.routine_summary_line(routine_counts)
        if routine:
            lines.append(routine)
    else:
        lines.append("  свободный день ✅")

    # Свободные окна
    free_windows = tl.find_free_windows(
        events, day_start, day_end,
        config.WORK_HOUR_START, config.SLEEP_HOUR_START,
        config.MIN_FREE_WINDOW_TODAY_HOURS,
    )
    if free_windows:
        lines.append("\n*🟢 Свободные окна:*")
        for s, e in free_windows:
            dur = (e - s).total_seconds() / 3600
            lines.append(f"  {s.strftime('%H:%M')}–{e.strftime('%H:%M')} ({tl.format_duration_short(dur)})")

    # Задачи на эту неделю
    week_tasks = [t for t in tasks if (cat.parse_task_due(t.get("due", "")) or datetime.max.replace(tzinfo=tz)) <= week_end]
    if week_tasks:
        lines.append("\n*📋 Задачи на эту неделю:*")
        lines.extend(_render_tasks_with_urgency(week_tasks, now, limit=10))

    # Сводка
    busy = tl.busy_hours(non_routine, tz)
    free_total = sum((e - s).total_seconds() / 3600 for s, e in free_windows)
    summary_parts = [f"📊 {tl.format_duration_short(busy)} занято"]
    if free_windows:
        summary_parts.append(f"Свободно: {tl.format_duration_short(free_total)}")
    summary_parts.append(f"Задач до {tl.WEEKDAYS_SHORT_RU[6]}: {len(week_tasks)}")
    lines.append("\n" + " • ".join(summary_parts))

    await message.answer("\n".join(lines), parse_mode="Markdown")


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    """Список доступных команд."""
    text = (
        "📖 *Справка*\n\n"
        "Пиши свободным текстом — я пойму («перенеси встречу», «добавь дедлайн на пятницу», «покажи задачи»).\n\n"
        "*Кнопки на клавиатуре:*\n"
        "📊 Статус — что сейчас/далее, события на сегодня и завтра, горящие задачи\n"
        "🗓 Сегодня — события + свободные окна + задачи на эту неделю\n"
        "📅 Нагрузка — визуальный график недели с категориями и дедлайнами\n\n"
        "*Команды:*\n"
        "/upload — загрузить PDF с расписанием\n"
        "/heatmap — график недели\n"
        "/settings — настройки (LLM, ключи, часы)\n"
        "/reauth — переавторизация Google Calendar\n"
        "/clear — сбросить историю диалога"
    )
    await message.answer(text, parse_mode="Markdown", reply_markup=MAIN_KB)


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    """Шапка (сейчас/далее) + сводка + сегодня/завтра + задачи с urgency."""
    tz = ZoneInfo(config.TIMEZONE)
    now = datetime.now(tz)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    horizon = today_start + timedelta(days=2)  # сегодня + завтра

    try:
        events = await cal.get_events(today_start.isoformat(), horizon.isoformat())
    except Exception as e:
        await _handle_error(message, e)
        return
    try:
        tasks = await tasks_svc.get_tasks()
    except Exception as e:
        logger.warning("Tasks API failed for /status: %s", e)
        tasks = []

    lines: list[str] = []

    # Шапка: сейчас или ближайшее
    current = tl.event_now(events, now)
    if current:
        title = cat.clean_title(current.get("title", ""))
        try:
            ed = tl.parse_iso_dt(current.get("end", "")).astimezone(tz)
            lines.append(f"🔴 *Сейчас:* {title} (до {ed.strftime('%H:%M')})")
        except Exception:
            lines.append(f"🔴 *Сейчас:* {title}")
    else:
        upcoming = tl.next_event_after(events, now)
        if upcoming:
            ev, td = upcoming
            title = cat.clean_title(ev.get("title", ""))
            try:
                sd = tl.parse_iso_dt(ev.get("start", "")).astimezone(tz)
                t_str = sd.strftime("%H:%M")
            except Exception:
                t_str = "?"
            lines.append(f"⏭ {tl.format_time_until(td)} — *{title}* ({t_str})")
        else:
            lines.append("⏭ На сегодня и завтра событий нет")

    # Сводка
    today_events = [
        ev for ev in events
        if (tl.parse_iso_dt(ev.get("start", "")).astimezone(tz).date() if ev.get("start") else None) == now.date()
    ]
    today_non_routine, _ = cat.collapse_routines(today_events, _routine_patterns())
    today_busy = tl.busy_hours(today_non_routine, tz)
    burning_count = sum(
        1 for t in tasks
        if cat.task_urgency(t, now, config.URGENT_TASK_DAYS, config.WARM_TASK_DAYS)[0]
        in ("overdue", "burning")
    )
    lines.append(
        f"📊 Сегодня: {len(today_non_routine)} событий, "
        f"{tl.format_duration_short(today_busy)} занято • Горящих задач: {burning_count}"
    )
    lines.append("")

    # События по дням (сегодня + завтра)
    grouped = tl.group_events_by_day(events, tz)
    if not grouped:
        lines.append("*🗓 События:* нет")
    for d, day_events in grouped[:2]:
        lines.extend(_render_day_block(d, day_events, now.date(), tz))
        lines.append("")

    # Задачи с urgency
    lines.append("*📋 Задачи:*")
    lines.extend(_render_tasks_with_urgency(tasks, now, limit=12))

    await message.answer("\n".join(lines).rstrip(), parse_mode="Markdown")


@router.message(Command("load"))
async def cmd_load(message: Message) -> None:
    """Текстовая сводка нагрузки на текущую неделю по дням и категориям."""
    tz = ZoneInfo(config.TIMEZONE)
    now = datetime.now(tz)
    week_start = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    week_end = week_start + timedelta(days=7)

    try:
        events = await cal.get_events(week_start.isoformat(), week_end.isoformat())
    except Exception as e:
        await _handle_error(message, e)
        return
    try:
        tasks = await tasks_svc.get_tasks()
    except Exception as e:
        logger.warning("Tasks API failed for /load: %s", e)
        tasks = []

    routine_pats = _routine_patterns()

    # По дням: основное / рутина
    day_main: dict[int, float] = {i: 0.0 for i in range(7)}
    day_routine: dict[int, float] = {i: 0.0 for i in range(7)}
    cat_hours: dict[str, float] = {}
    routine_total = 0.0

    for ev in events:
        s = ev.get("start", "")
        e = ev.get("end", "")
        if "T" not in s or "T" not in e:
            continue
        try:
            sd = tl.parse_iso_dt(s).astimezone(tz)
            ed = tl.parse_iso_dt(e).astimezone(tz)
        except Exception:
            continue
        hrs = (ed - sd).total_seconds() / 3600
        idx = sd.weekday()
        if cat.is_routine(ev, routine_pats):
            day_routine[idx] += hrs
            routine_total += hrs
        else:
            day_main[idx] += hrs
            c = cat.event_category(ev)
            cat_hours[c] = cat_hours.get(c, 0.0) + hrs

    day_names = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    lines = [
        f"📊 *Нагрузка ({week_start.strftime('%d.%m')} – {(week_end - timedelta(days=1)).strftime('%d.%m')}):*\n"
    ]
    for i, name in enumerate(day_names):
        m = day_main[i]
        r = day_routine[i]
        if m == 0 and r == 0:
            lines.append(f"  {name}: —")
            continue
        suffix = f" + 🚗 {tl.format_duration_short(r)}" if r > 0.05 else ""
        lines.append(f"  {name}: {tl.format_duration_short(m)}{suffix}")

    # По категориям
    if cat_hours or routine_total > 0:
        lines.append("\n*📂 По категориям:*")
        cat_emoji = {"учёба": "📚", "работа": "💼", "личное": "🎉", cat.UNKNOWN_CATEGORY: "❓"}
        for c, h in sorted(cat_hours.items(), key=lambda x: -x[1]):
            label = c.capitalize() if c != cat.UNKNOWN_CATEGORY else "Без категории"
            lines.append(f"  {cat_emoji.get(c, '·')} {label}: {tl.format_duration_short(h)}")
        if routine_total > 0.05:
            lines.append(f"  🚗 Дорога: {tl.format_duration_short(routine_total)}")

    # Итого
    main_total = sum(day_main.values())
    grand_total = main_total + routine_total
    per_week = config.WORK_HOURS_PER_WEEK or 0
    pct_str = f" ({main_total / per_week * 100:.0f}% от {per_week}ч)" if per_week > 0 else ""
    lines.append(f"\n*Итого:* {tl.format_duration_short(main_total)}{pct_str}")
    if routine_total > 0.05:
        lines.append(f"*С учётом дороги:* {tl.format_duration_short(grand_total)}")
    lines.append(f"*Событий:* {len(events)} • *Активных задач:* {len(tasks)}")

    await message.answer("\n".join(lines), parse_mode="Markdown")


@router.message(Command("done"))
async def cmd_done(message: Message) -> None:
    """Отмечает задачу выполненной по частичному совпадению названия."""
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Укажи название задачи: `/done Сдать отчёт`", parse_mode="Markdown")
        return

    query = args[1].strip().lower()

    try:
        tasks = await tasks_svc.get_tasks()
    except Exception as e:
        await _handle_error(message, e)
        return

    matches = [t for t in tasks if query in t.get("title", "").lower()]

    if not matches:
        await message.answer(f"Задача «{args[1]}» не найдена среди активных.")
        return

    if len(matches) > 1:
        names = "\n".join(f"  • {cat.clean_title(t.get('title', ''))}" for t in matches[:5])
        await message.answer(f"Найдено несколько, уточни название:\n{names}")
        return

    task = matches[0]
    title = cat.clean_title(task.get("title", ""))
    try:
        await tasks_svc.complete_task(task["id"])
        await message.answer(f"✅ Задача выполнена: *{title}*", parse_mode="Markdown")
    except Exception as e:
        logger.error("Ошибка Tasks API (complete_task): %s", e, exc_info=True)
        await _handle_error(message, e)


@router.message(Command("postpone"))
async def cmd_postpone(message: Message) -> None:
    """Откладывает задачу. Делегирует агенту для интерпретации времени."""
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer(
            "Укажи название и новое время:\n`/postpone Сдать отчёт завтра в 18:00`",
            parse_mode="Markdown",
        )
        return

    from app.services.agent import run_agent
    from app.handlers.messages import handle_agent_response

    user_id = message.from_user.id
    prompt = f"Отложи задачу: {args[1]}"

    thinking = await message.answer("⏳ Обрабатываю запрос...")
    try:
        response = await run_agent(user_id, prompt)
    except Exception as e:
        await _handle_error(message, e)
        return
    finally:
        try:
            await thinking.delete()
        except Exception:
            pass

    await handle_agent_response(message, response, user_id)


@router.message(Command("clear"))
async def cmd_clear(message: Message) -> None:
    """Очищает историю диалога пользователя."""
    user_id = message.from_user.id
    await clear_history(user_id)
    await message.answer(
        "🗑 История диалога очищена. Начинаем с чистого листа!"
    )


def _is_owner(message: Message) -> bool:
    return bool(message.from_user) and message.from_user.id == config.OWNER_ID


@router.message(Command("settings"))
async def cmd_settings(message: Message) -> None:
    """Открывает интерактивное меню настроек (только для OWNER_ID)."""
    if not _is_owner(message):
        await message.answer("⛔ Эта команда доступна только владельцу.")
        return
    from app.handlers.settings import send_settings_menu
    await send_settings_menu(message)


@router.message(Command("reauth"))
async def cmd_reauth(message: Message) -> None:
    """Генерирует ссылку для повторной авторизации Google (только для OWNER_ID)."""
    if not _is_owner(message):
        await message.answer("⛔ Эта команда доступна только владельцу.")
        return
    await send_token_expired(message)


@router.message(Command("auth_code"))
async def cmd_auth_code(message: Message) -> None:
    """Принимает код авторизации Google и сохраняет токен (только для OWNER_ID)."""
    if not _is_owner(message):
        await message.answer("⛔ Эта команда доступна только владельцу.")
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer(
            "Использование: `/auth_code <код или URL>`",
            parse_mode="Markdown",
        )
        return
    code = parts[1].strip()
    user_id = message.from_user.id

    progress = await message.answer("⏳ Проверяю код...")

    try:
        # complete_auth делает HTTP-запрос к Google + файловые операции — выносим в поток
        await asyncio.to_thread(cal.complete_auth, code, user_id)
    except Exception as e:
        logger.error("Ошибка при обмене кода авторизации: %s", e, exc_info=True)
        try:
            await progress.delete()
        except Exception:
            pass
        err_msg = str(e) or type(e).__name__
        if len(err_msg) > 500:
            err_msg = err_msg[:500] + "..."
        await message.answer(
            f"❌ Ошибка авторизации:\n`{err_msg}`\n\n"
            "Если ошибка про `state` — попробуй открыть СВЕЖУЮ ссылку из /reauth, "
            "не старую из истории чата.\n"
            "Если ошибка про `redirect_uri_mismatch` — добавь "
            "`http://localhost` в Authorized redirect URIs в Google Cloud Console.\n"
            "Иначе — /reauth и заново.",
            parse_mode="Markdown",
        )
        return

    # Токен сохранён. Делаем тестовый API-вызов чтобы убедиться что он работает.
    try:
        from datetime import datetime, timedelta, timezone as _tz
        now = datetime.now(_tz.utc)
        await cal.get_events(now.isoformat(), (now + timedelta(days=1)).isoformat())
        verified = "✅ Тестовый запрос к календарю прошёл."
    except Exception as e:
        logger.warning("Token saved but test API call failed: %s", e)
        verified = (
            "⚠️ Токен сохранён, но тестовый запрос упал: "
            f"`{str(e)[:200]}`. Попробуй /status — если работает, всё ок."
        )

    try:
        await progress.delete()
    except Exception:
        pass
    await message.answer(
        f"✅ Авторизация выполнена.\n{verified}",
        parse_mode="Markdown",
    )


@router.message(Command("auth_cancel"))
async def cmd_auth_cancel(message: Message) -> None:
    """Сбрасывает застрявший OAuth-сеанс (если /auth_code не получается)."""
    if not _is_owner(message):
        await message.answer("⛔ Эта команда доступна только владельцу.")
        return
    user_id = message.from_user.id
    if cal.cancel_auth(user_id):
        await message.answer("🗑 Активный auth-сеанс отменён. Запусти /reauth заново.")
    else:
        await message.answer("Нет активного auth-сеанса.")


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

    # Дедлайны задач — best-effort, без падения если Tasks API не отвечает
    try:
        tasks = await tasks_svc.get_tasks()
    except Exception as e:
        logger.warning("Tasks API failed for /heatmap: %s", e)
        tasks = []

    try:
        img_bytes = await _generate_heatmap_image(
            events, config.TIMEZONE, week_start=fetch_start, tasks=tasks,
        )
    except Exception as e:
        logger.error("Ошибка генерации heatmap: %s", e)
        await message.answer("❌ Ошибка генерации графика")
        return

    photo = BufferedInputFile(img_bytes, filename="heatmap.png")
    date_range = f"{fetch_start.strftime('%d.%m')} – {(fetch_end - timedelta(days=1)).strftime('%d.%m')}"
    try:
        await message.answer_photo(
            photo,
            caption=(
                f"📊 Расписание: {date_range}\n"
                "Цвета — категории · обводка ■ — нельзя двигать · 🚗 — рутина · 🟢 — свободно · 📌 — дедлайны"
            ),
        )
    except Exception as e:
        logger.error("Ошибка отправки heatmap: %s", e)
        await message.answer(f"❌ Не удалось отправить изображение: {e}")
