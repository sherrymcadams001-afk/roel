"""File-based instance lock to prevent multiple orchestrator processes.

On macOS/Linux uses ``fcntl.flock``; on Windows uses ``msvcrt.locking``.
The lock is held for the lifetime of the returned context manager.  If a
second process tries to acquire the same lock it will fail fast with
``InstanceAlreadyRunning``.
"""

from __future__ import annotations

import logging
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

logger = logging.getLogger(__name__)

_LOCK_FILENAME = "orchestrator.lock"


class InstanceAlreadyRunning(RuntimeError):
    """Raised when another orchestrator instance holds the lock."""


@contextmanager
def acquire_instance_lock(
    lock_dir: Path | None = None,
) -> Generator[Path, None, None]:
    """Context manager that holds a file lock for the process lifetime.

    Args:
        lock_dir: Directory to place the lock file.  Defaults to
                  ``data/`` next to settings.STATE_FILE.

    Yields:
        The path of the lock file while the lock is held.

    Raises:
        InstanceAlreadyRunning: If another process already holds the lock.
    """
    if lock_dir is None:
        from config import settings
        lock_dir = settings.STATE_FILE.parent

    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / _LOCK_FILENAME

    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
    try:
        _try_lock(fd, lock_path)
        # Write PID for diagnostics
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0) if hasattr(os, "ftruncate") else None
        pid_bytes = f"{os.getpid()}\n".encode()
        os.write(fd, pid_bytes)

        logger.info("[LOCK] Instance lock acquired: %s (pid=%s)", lock_path, os.getpid())
        yield lock_path
    finally:
        _unlock(fd)
        os.close(fd)
        try:
            lock_path.unlink(missing_ok=True)
        except Exception:
            pass
        logger.info("[LOCK] Instance lock released.")


# ---------------------------------------------------------------------------
# Platform-specific lock helpers
# ---------------------------------------------------------------------------

def _try_lock(fd: int, lock_path: Path) -> None:
    if sys.platform == "win32":
        _try_lock_windows(fd, lock_path)
    else:
        _try_lock_posix(fd, lock_path)


def _try_lock_posix(fd: int, lock_path: Path) -> None:
    import fcntl
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError):
        _read_owner_and_raise(lock_path)


def _try_lock_windows(fd: int, lock_path: Path) -> None:
    import msvcrt
    try:
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    except (OSError, IOError):
        _read_owner_and_raise(lock_path)


def _unlock(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except Exception:
            pass
    else:
        import fcntl
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except Exception:
            pass


def _read_owner_and_raise(lock_path: Path) -> None:
    owner_pid = "unknown"
    try:
        owner_pid = lock_path.read_text().strip()
    except Exception:
        pass
    raise InstanceAlreadyRunning(
        f"Another orchestrator instance is already running (pid={owner_pid}). "
        f"Lock file: {lock_path}"
    )
