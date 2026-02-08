"""Local message store for WhatsApp conversations.

Provides deduplication and history retrieval for WhatsApp messages,
which lack a local database like iMessage's chat.db.

Storage: one JSON file per handle under ``data/whatsapp_history/``.
Each file contains a chronologically ordered list of message dicts.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from utils.atomic import atomic_write_json

logger = logging.getLogger(__name__)

# Maximum messages retained per handle (FIFO eviction)
_MAX_MESSAGES_PER_HANDLE = 200


class WhatsAppMessageStore:
    """Persistent message store for WhatsApp conversations."""

    def __init__(self, store_dir: Path) -> None:
        self._dir = store_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        # In-memory dedup set: (handle, text_hash, epoch_bucket)
        # Epoch bucket = epoch // 60 (1-minute granularity) to allow same text at different times
        self._seen: Set[tuple] = set()
        self._load_seen_index()

    def _path_for(self, handle: str) -> Path:
        safe = handle.replace("+", "").replace(" ", "").replace("-", "").strip()
        return self._dir / f"{safe}.json"

    def _load_seen_index(self) -> None:
        """Build dedup index from existing files on startup."""
        try:
            for path in self._dir.glob("*.json"):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    if not isinstance(data, list):
                        continue
                    for msg in data[-50:]:  # Only index recent messages for memory efficiency
                        self._index_message(msg)
                except Exception:
                    continue
        except Exception as exc:
            logger.warning("[WA_STORE] Failed to build dedup index: %s", exc)

    def _index_message(self, msg: Dict) -> None:
        handle = str(msg.get("handle", ""))
        text = str(msg.get("text", ""))
        epoch = int(msg.get("epoch", 0))
        bucket = epoch // 60
        key = (handle, hash(text), bucket)
        self._seen.add(key)

    def is_duplicate(self, handle: str, text: str) -> bool:
        """Check if this message was already stored recently."""
        bucket = int(time.time()) // 60
        key = (handle, hash(text), bucket)
        # Also check previous minute to handle boundary cases
        key_prev = (handle, hash(text), bucket - 1)
        return key in self._seen or key_prev in self._seen

    def store_message(
        self,
        *,
        handle: str,
        text: str,
        is_from_me: bool,
        service: str = "WhatsApp",
        epoch: Optional[float] = None,
        message_id: Optional[str] = None,
    ) -> bool:
        """Store a message.  Returns False if it was a duplicate."""
        if not text or not text.strip():
            return False

        now = epoch or time.time()

        if self.is_duplicate(handle, text):
            logger.debug("[WA_STORE] Duplicate suppressed for %s: %s", handle, text[:40])
            return False

        msg: Dict[str, Any] = {
            "handle": handle,
            "text": text.strip(),
            "is_from_me": is_from_me,
            "service": service,
            "epoch": now,
            "message_id": message_id or f"wa_{int(now * 1000)}",
        }

        path = self._path_for(handle)
        messages = self._load_file(path)
        messages.append(msg)

        # FIFO eviction
        if len(messages) > _MAX_MESSAGES_PER_HANDLE:
            messages = messages[-_MAX_MESSAGES_PER_HANDLE:]

        atomic_write_json(path, messages)
        self._index_message(msg)
        return True

    def fetch_history(self, handle: str, limit: int = 20) -> List[Dict[str, Any]]:
        """Fetch recent messages for a handle, formatted for the orchestrator."""
        path = self._path_for(handle)
        messages = self._load_file(path)

        result: List[Dict[str, Any]] = []
        for msg in messages[-limit:]:
            is_from_me = msg.get("is_from_me", False)
            result.append({
                "role": "assistant" if is_from_me else "user",
                "text": msg.get("text", ""),
                "date": int(msg.get("epoch", 0)),
                "is_from_me": is_from_me,
            })
        return result

    def get_last_inbound_text(self, handle: str) -> Optional[str]:
        """Get the most recent inbound message text for a handle."""
        path = self._path_for(handle)
        messages = self._load_file(path)
        for msg in reversed(messages):
            if not msg.get("is_from_me", False):
                return msg.get("text")
        return None

    def _load_file(self, path: Path) -> List[Dict]:
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception:
            return []
