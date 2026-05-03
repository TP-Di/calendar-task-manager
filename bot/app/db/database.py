"""
База данных: инициализация, история диалога, контекст пользователя, бэкап
"""

import json
import logging
import os
from datetime import datetime, timezone

import aiosqlite

from app.config import config

logger = logging.getLogger(__name__)


async def init_db() -> None:
    """Создаёт таблицы если их нет. Включает WAL для concurrent reads."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS dialog_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS user_context (
                user_id INTEGER PRIMARY KEY,
                context_json TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL
            )
            """
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_history_user ON dialog_history(user_id)"
        )
        await db.commit()
    logger.info("База данных инициализирована: %s", config.DB_PATH)


# ---------------------------------------------------------------------------
# История диалога
# ---------------------------------------------------------------------------


async def add_message(user_id: int, role: str, content: str) -> None:
    """Добавляет сообщение в историю и обрезает до MAX_HISTORY."""
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "INSERT INTO dialog_history (user_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (user_id, role, content, now),
        )
        # Оставляем только последние MAX_HISTORY сообщений
        await db.execute(
            """
            DELETE FROM dialog_history
            WHERE user_id = ? AND id NOT IN (
                SELECT id FROM dialog_history
                WHERE user_id = ?
                ORDER BY id DESC
                LIMIT ?
            )
            """,
            (user_id, user_id, config.MAX_HISTORY),
        )
        await db.commit()


async def get_history(user_id: int) -> list[dict]:
    """Возвращает историю диалога для пользователя."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT role, content FROM dialog_history WHERE user_id = ? ORDER BY id ASC",
            (user_id,),
        ) as cursor:
            rows = await cursor.fetchall()
    return [{"role": row["role"], "content": row["content"]} for row in rows]


async def clear_history(user_id: int) -> None:
    """Очищает историю диалога пользователя."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "DELETE FROM dialog_history WHERE user_id = ?", (user_id,)
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Контекст пользователя
# ---------------------------------------------------------------------------


async def get_user_context(user_id: int) -> dict:
    """Возвращает контекст пользователя (произвольный JSON)."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT context_json FROM user_context WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
    if row is None:
        return {}
    return json.loads(row["context_json"])


async def update_user_context(user_id: int, data: dict) -> None:
    """Обновляет контекст пользователя (merge с существующим)."""
    current = await get_user_context(user_id)
    current.update(data)
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO user_context (user_id, context_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                context_json = excluded.context_json,
                updated_at = excluded.updated_at
            """,
            (user_id, json.dumps(current, ensure_ascii=False), now),
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Бэкап
# ---------------------------------------------------------------------------


async def backup_db() -> None:
    """
    Создаёт бэкап SQLite через Backup API (WAL-safe).
    Ротация: оставляем последние 30 файлов.
    """
    backup_dir = os.path.join(os.path.dirname(config.DB_PATH), "backups")
    os.makedirs(backup_dir, exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d")
    backup_path = os.path.join(backup_dir, f"bot_{date_str}.db")

    # SQLite Backup API через aiosqlite — корректно работает с WAL
    async with aiosqlite.connect(config.DB_PATH) as src, \
               aiosqlite.connect(backup_path) as dst:
        await src.backup(dst)
    logger.info("Бэкап базы данных: %s", backup_path)

    # Ротация: keep last 30
    try:
        files = sorted(
            f for f in os.listdir(backup_dir)
            if f.startswith("bot_") and f.endswith(".db")
        )
        for old in files[:-30]:
            os.remove(os.path.join(backup_dir, old))
            logger.debug("Удалён старый бэкап: %s", old)
    except Exception as e:
        logger.warning("Не удалось ротировать бэкапы: %s", e)
