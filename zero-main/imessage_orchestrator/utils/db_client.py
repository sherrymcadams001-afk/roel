from __future__ import annotations

import logging
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)


def _is_locked_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return "database is locked" in message or "database schema is locked" in message


@contextmanager
def connect_readonly(
    db_path: Path,
    *,
    retries: int = 3,
    backoff_seconds: float = 0.35,
):
    """Open a read-only SQLite connection with retries for common macOS lock errors."""

    if not db_path.exists():
        raise FileNotFoundError(f"Messages db not found: {db_path}")

    uri = f"file:{db_path.as_posix()}?mode=ro"
    last_exc: BaseException | None = None

    for attempt in range(1, retries + 1):
        try:
            conn = sqlite3.connect(
                uri,
                uri=True,
                check_same_thread=False,
                timeout=1.0,
            )
            conn.row_factory = sqlite3.Row
            try:
                yield conn
            finally:
                conn.close()
            return
        except sqlite3.OperationalError as exc:
            last_exc = exc
            if _is_locked_error(exc) and attempt < retries:
                logger.warning(
                    "Messages db locked; retrying (%s/%s)",
                    attempt,
                    retries,
                )
                time.sleep(backoff_seconds * attempt)
                continue
            raise

    if last_exc is not None:
        raise last_exc


def fetch_all(conn: sqlite3.Connection, query: str, params: tuple = ()) -> list[sqlite3.Row]:
    cur = conn.execute(query, params)
    return list(cur.fetchall())


def fetch_one(conn: sqlite3.Connection, query: str, params: tuple = ()) -> sqlite3.Row | None:
    cur = conn.execute(query, params)
    return cur.fetchone()
