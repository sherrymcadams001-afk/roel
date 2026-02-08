from __future__ import annotations

import time
from datetime import datetime
from typing import List
from .interfaces import TransportWatcher, IncomingMessage

class CompositeWatcher:
    def __init__(self, walkers: List[TransportWatcher]):
        self.watchers = walkers

    def initialize(self) -> None:
        for w in self.watchers:
            w.initialize()

    def load_state(self) -> None:
        # Some watchers define load_state, others don't in the interface.
        # But we know iMessageWatcher does.
        for w in self.watchers:
            if hasattr(w, "load_state"):
                w.load_state()
            if hasattr(w, "verify_permissions"):
                w.verify_permissions()

    def poll_new_messages(self) -> List[IncomingMessage]:
        all_messages = []
        for w in self.watchers:
            all_messages.extend(w.poll())
        
        # Sort by date/timestamp if possible to preserve order
        all_messages.sort(key=lambda m: m.date)
        return all_messages
    
    # Delegate other methods if needed, e.g. fetch_recent_history
    # But usually fetch_recent_history logic is specific to iMessage DB.
    # WhatsApp history retrieval is harder.
    # For now, if orchestrator calls `watcher.fetch_recent_history`, we need to handle it.
    
    def fetch_recent_history(self, *, handle: str, limit: int = 20) -> List[dict]:
        """Fetch recent message history from the best available source.

        Tries each watcher in order.  The WhatsAppWatcher now has a
        persistent store-backed ``fetch_recent_history`` so WhatsApp
        contacts will get real history instead of empty lists.
        """
        for w in self.watchers:
            if hasattr(w, "fetch_recent_history"):
                try:
                    hist = w.fetch_recent_history(handle=handle, limit=limit)
                    if hist:
                        return hist
                except Exception:
                    continue
                    
        return []

    def read_message(self, handle: str) -> str | None:
        """
        Actually read a message from a specific handle.
        This is for WhatsApp deferred reading - marks message as "read" (blue ticks).
        """
        for w in self.watchers:
            if hasattr(w, "read_message"):
                result = w.read_message(handle)
                if result:
                    return result
        return None

    def fetch_last_messages_with_timestamps(self, *, handle: str, limit: int = 3) -> List[dict]:
        """Best-effort recent messages with timestamps.

        iMessage watcher can provide real timestamps from chat.db.
        For transports without a message store (e.g. WhatsApp via UI), we fall back
        to whatever `fetch_recent_history` can provide and synthesize minimal
        timestamp fields so downstream context building doesn't crash.
        """

        # Prefer a native implementation if any watcher provides it.
        for w in self.watchers:
            fn = getattr(w, "fetch_last_messages_with_timestamps", None)
            if callable(fn):
                try:
                    msgs = fn(handle=handle, limit=limit)
                    if msgs:
                        return msgs
                except Exception:
                    # Try other watchers.
                    continue

        # Fallback: derive from fetch_recent_history and add timestamp-like keys.
        hist = self.fetch_recent_history(handle=handle, limit=max(1, int(limit)))
        if not hist:
            return []

        now_unix = int(time.time())
        now_str = datetime.fromtimestamp(now_unix).strftime("%Y-%m-%d %H:%M:%S")
        out: list[dict] = []

        # `fetch_recent_history` is typically [{role,text,date,...}] in chronological order.
        for item in hist[-limit:]:
            role = str(item.get("role", "user"))
            text = str(item.get("text", "")).strip()
            if not text:
                continue
            out.append(
                {
                    "sender": "Me" if role == "assistant" else "Them",
                    "role": role,
                    "text": text,
                    "time": now_str,
                    "time_ago": "",
                    "unix_ts": now_unix,
                }
            )

        return out

