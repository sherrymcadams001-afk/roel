"""Non-blocking scheduled send queue.

Replaces the blocking ``time.sleep(delay)`` in the main loop with a
persistent, disk-backed queue of messages scheduled for future delivery.

Each entry has a ``send_after_epoch`` timestamp.  The main loop calls
``drain()`` every tick, which sends all entries whose timestamp has passed.

Entries are persisted via atomic JSON writes so nothing is lost on crash.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from utils.atomic import atomic_write_json

logger = logging.getLogger(__name__)


@dataclass
class ScheduledMessage:
    """A single message awaiting delivery at a future time."""

    handle: str
    text: str
    service: str
    send_after_epoch: float
    created_epoch: float = field(default_factory=time.time)
    retries: int = 0
    max_retries: int = 3
    # Context for logging / operator alerts
    context: str = ""

    def is_due(self) -> bool:
        return time.time() >= self.send_after_epoch

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ScheduledMessage":
        # Accept only known fields to avoid TypeError on schema evolution
        known = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in d.items() if k in known}
        return cls(**filtered)


class SendQueue:
    """Disk-backed queue of scheduled outbound messages.

    Thread-safety: This class is **not** thread-safe by design — it is
    called exclusively from the single-threaded orchestrator main loop.
    """

    def __init__(self, queue_file: Path) -> None:
        self._path = queue_file
        self._queue: List[ScheduledMessage] = []
        self._load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def enqueue(
        self,
        *,
        handle: str,
        text: str,
        service: str,
        delay_seconds: float,
        context: str = "",
    ) -> None:
        """Schedule a message for delivery after *delay_seconds*."""
        entry = ScheduledMessage(
            handle=handle,
            text=text,
            service=service,
            send_after_epoch=time.time() + delay_seconds,
            context=context,
        )
        self._queue.append(entry)
        self._save()
        logger.info(
            "[SEND_Q] Enqueued %s (delay=%.1fs, queue_depth=%d)",
            handle,
            delay_seconds,
            len(self._queue),
        )

    def drain(
        self,
        send_fn: Callable[[str, str, str], bool],
        *,
        max_per_tick: int = 10,
        on_failure: Optional[Callable[[ScheduledMessage], None]] = None,
    ) -> int:
        """Send all due messages.  Returns number of messages sent.

        Args:
            send_fn: ``(handle, text, service) -> bool``
            max_per_tick: Cap sends per call to avoid blocking too long.
            on_failure: Optional callback when a message permanently fails
                        (exhausted retries).
        """
        sent = 0
        still_pending: List[ScheduledMessage] = []

        for entry in self._queue:
            if not entry.is_due():
                still_pending.append(entry)
                continue

            if sent >= max_per_tick:
                still_pending.append(entry)
                continue

            success = False
            try:
                success = send_fn(entry.handle, entry.text, entry.service)
            except Exception as exc:
                logger.error("[SEND_Q] Send raised for %s: %s", entry.handle, exc)

            if success:
                logger.info("[SEND_Q] Delivered to %s", entry.handle)
                sent += 1
            else:
                entry.retries += 1
                if entry.retries >= entry.max_retries:
                    logger.error(
                        "[SEND_Q] Permanently failed for %s after %d retries",
                        entry.handle,
                        entry.retries,
                    )
                    if on_failure:
                        on_failure(entry)
                    # Drop from queue
                else:
                    # Re-schedule with exponential backoff (10s, 20s, 40s …)
                    backoff = 10.0 * (2 ** (entry.retries - 1))
                    entry.send_after_epoch = time.time() + backoff
                    still_pending.append(entry)
                    logger.warning(
                        "[SEND_Q] Retry %d/%d for %s in %.0fs",
                        entry.retries,
                        entry.max_retries,
                        entry.handle,
                        backoff,
                    )

        if sent or len(still_pending) != len(self._queue):
            self._queue = still_pending
            self._save()

        return sent

    @property
    def depth(self) -> int:
        return len(self._queue)

    def has_pending_for(self, handle: str) -> bool:
        return any(e.handle == handle for e in self._queue)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _save(self) -> None:
        try:
            atomic_write_json(self._path, [e.to_dict() for e in self._queue])
        except Exception as exc:
            logger.error("[SEND_Q] Failed to persist queue: %s", exc)

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                self._queue = [ScheduledMessage.from_dict(d) for d in raw if isinstance(d, dict)]
                if self._queue:
                    logger.info("[SEND_Q] Loaded %d pending sends from disk", len(self._queue))
        except Exception as exc:
            logger.warning("[SEND_Q] Failed to load queue: %s", exc)
            self._queue = []
