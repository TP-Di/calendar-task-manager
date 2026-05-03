"""
SQLite logging handler — пишет записи логов в таблицу bot_logs.
Используется вместе со стандартным logging; доступ к БД только через SSH.
emit() выполняется в отдельном потоке чтобы не блокировать event loop.
"""

import concurrent.futures
import logging
import os
import sqlite3


_MAX_ROWS_DEFAULT = 10_000

# Single-thread executor: log writes are sequential, no concurrent DB corruption
_log_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix="sqlite_log",
)


class SqliteLogHandler(logging.Handler):
    """logging.Handler that writes to SQLite without blocking the event loop."""

    def __init__(self, db_path: str):
        super().__init__()
        os.makedirs(os.path.dirname(db_path) if os.path.dirname(db_path) else ".", exist_ok=True)
        self._db_path = db_path
        self._max_rows = int(os.getenv("LOG_DB_MAX_ROWS", str(_MAX_ROWS_DEFAULT)))
        self._ensure_table()

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path)

    def _ensure_table(self) -> None:
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS bot_logs (
                    id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts       TEXT NOT NULL,
                    level    TEXT NOT NULL,
                    logger   TEXT NOT NULL,
                    message  TEXT NOT NULL,
                    exc_text TEXT
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_level ON bot_logs(level)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_ts    ON bot_logs(ts)")

    def emit(self, record: logging.LogRecord) -> None:
        # Submit to thread pool — returns immediately, does not block the caller
        _log_executor.submit(self._do_emit, record)

    def _do_emit(self, record: logging.LogRecord) -> None:
        try:
            exc = record.exc_text or None
            ts = self.formatTime(record, "%Y-%m-%d %H:%M:%S")
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO bot_logs(ts, level, logger, message, exc_text) VALUES (?,?,?,?,?)",
                    (ts, record.levelname, record.name, record.getMessage(), exc),
                )
                conn.execute(
                    "DELETE FROM bot_logs WHERE id <= (SELECT MAX(id) - ? FROM bot_logs)",
                    (self._max_rows,),
                )
        except Exception:
            self.handleError(record)
