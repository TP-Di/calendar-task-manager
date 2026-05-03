"""
SQLite logging handler — пишет записи логов в таблицу bot_logs.
Используется вместе со стандартным logging; доступ к БД только через SSH.
emit() выполняется в отдельном потоке чтобы не блокировать event loop.
"""

import concurrent.futures
import logging
import os
import re
import sqlite3
import time


# Паттерны секретов для редактирования перед записью в БД
_REDACT_PATTERNS = [
    re.compile(r"ya29\.[A-Za-z0-9_\-]{20,}"),                      # Google access token
    re.compile(r"1//[A-Za-z0-9_\-]{20,}"),                          # Google refresh token
    re.compile(r"eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"),  # JWT
    re.compile(r'"refresh_token"\s*:\s*"[^"]+"'),                  # JSON refresh_token
    re.compile(r'"access_token"\s*:\s*"[^"]+"'),                   # JSON access_token
    re.compile(r'"client_secret"\s*:\s*"[^"]+"'),                  # OAuth client secret
    re.compile(r"gsk_[A-Za-z0-9]{20,}"),                            # Groq API keys (gsk_*)
    re.compile(r"AIza[A-Za-z0-9_\-]{30,}"),                         # Google API key (AIza*)
]


def _redact(text: str) -> str:
    if not text:
        return text
    for pat in _REDACT_PATTERNS:
        text = pat.sub("[REDACTED]", text)
    return text


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
        self._writes_since_cleanup = 0
        self._cleanup_every = 100
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
            msg = _redact(record.getMessage())
            exc = _redact(record.exc_text) if record.exc_text else None
            # logging.Handler не имеет formatTime — это метод Formatter.
            # Берём timestamp из record.created напрямую.
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.created))
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO bot_logs(ts, level, logger, message, exc_text) VALUES (?,?,?,?,?)",
                    (ts, record.levelname, record.name, msg, exc),
                )
                # M10: cleanup раз в N записей, не на каждый emit
                self._writes_since_cleanup += 1
                if self._writes_since_cleanup >= self._cleanup_every:
                    conn.execute(
                        "DELETE FROM bot_logs WHERE id <= (SELECT MAX(id) - ? FROM bot_logs)",
                        (self._max_rows,),
                    )
                    self._writes_since_cleanup = 0
        except Exception:
            self.handleError(record)
