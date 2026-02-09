from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from config import settings
from utils.atomic import atomic_write_json
from utils.db_client import connect_readonly, fetch_all
from .interfaces import IncomingMessage

logger = logging.getLogger(__name__)


class iMessageWatcher:
    """Ingress service: polls chat.db for new inbound messages."""

    def __init__(
        self,
        *,
        chat_db_path: Path = settings.CHAT_DB_PATH,
        state_file: Path = settings.STATE_FILE,
        target_handles: set[str] | None = None,
    ) -> None:
        self.chat_db_path = chat_db_path
        self.state_file = state_file
        self.target_handles = target_handles
        self._state: dict[str, Any] = {}

    def initialize(self) -> None:
        """Perform startup checks and load state."""
        self.verify_permissions()
        self.load_state()

    def verify_permissions(self) -> None:
        # On macOS, Full Disk Access is typically required for ~/Library/Messages.
        if not self.chat_db_path.exists():
            raise FileNotFoundError(
                f"Messages db not found at {self.chat_db_path}. Is Messages enabled?"
            )
        # os.access can return True even when TCC blocks access, but it's still a useful hint.
        if not os.access(self.chat_db_path, os.R_OK):
            raise PermissionError(
                "No read access to chat.db. Grant your terminal/python Full Disk Access "
                "(System Settings → Privacy & Security → Full Disk Access)."
            )

    def load_state(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        if self.state_file.exists():
            try:
                self._state = json.loads(self.state_file.read_text(encoding="utf-8"))
            except Exception:
                logger.warning("State file unreadable; starting fresh")
                self._state = {}
        else:
            self._state = {}

        self._state.setdefault("last_message_rowid", 0)

    def save_state(self) -> None:
        atomic_write_json(self.state_file, self._state)

    def _target_handles_clause(self) -> tuple[str, tuple]:
        if self.target_handles is None:
            # No allowlist filtering at the watcher layer.
            return "(1=1)", ()

        if not self.target_handles:
            # Explicit empty allowlist means match nothing.
            return "(1=0)", ()
        placeholders = ",".join(["?"] * len(self.target_handles))
        return f"h.id IN ({placeholders})", tuple(self.target_handles)

    def poll_new_messages(self) -> list[IncomingMessage]:
        """Return new inbound messages since last poll."""

        last_rowid = int(self._state.get("last_message_rowid", 0))
        clause, params = self._target_handles_clause()

        query = f"""
        SELECT
            m.ROWID AS message_rowid,
            h.id AS handle,
            COALESCE(m.text, '') AS text,
            COALESCE(m.service, 'iMessage') AS service,
            m.date AS date
        FROM message m
        JOIN handle h ON h.ROWID = m.handle_id
        WHERE
            m.ROWID > ?
            AND m.is_from_me = 0
            AND {clause}
            AND COALESCE(m.text, '') <> ''
        ORDER BY m.ROWID ASC
        """

        with connect_readonly(
            self.chat_db_path,
            retries=settings.DB_LOCKED_RETRIES,
            backoff_seconds=settings.DB_LOCKED_BACKOFF_SECONDS,
        ) as conn:
            rows = fetch_all(conn, query, (last_rowid, *params))

        messages: list[IncomingMessage] = []
        max_rowid = last_rowid
        for r in rows:
            rowid = int(r["message_rowid"])
            max_rowid = max(max_rowid, rowid)
            messages.append(
                IncomingMessage(
                    message_rowid=rowid,
                    handle=str(r["handle"]),
                    text=str(r["text"]).strip(),
                    service=str(r["service"]),
                    date=int(r["date"] if r["date"] else 0),
                )
            )

        if messages:
            self._state["last_message_rowid"] = max_rowid
            self.save_state()

        return messages
    
    def poll(self) -> list[IncomingMessage]:
        return self.poll_new_messages()

    @staticmethod
    def _extract_text_from_attributed_body(blob: bytes) -> str:
        """
        Extract plain text from NSAttributedString blob (attributedBody column).
        
        macOS iMessage stores outgoing SMS text in attributedBody as a binary
        NSAttributedString/NSMutableAttributedString. Structure:
        - Header with "streamtyped" and class names
        - NSString marker followed by length byte(s) then UTF-8 text
        - Trailing dictionary data (__kIMMessagePart...)
        """
        if not blob:
            return ""
        try:
            import re
            
            # Decode the blob
            text = blob.decode('utf-8', errors='ignore')
            
            # Method 1: Find text between NSString marker and NSDictionary marker
            ns_dict_idx = text.find('NSDictionary')
            ns_string_idx = text.find('NSString')
            
            if ns_string_idx != -1 and ns_dict_idx != -1 and ns_dict_idx > ns_string_idx:
                # Pattern: NSString + \x01\x01+ + length_byte + actual_text + ... + NSDictionary
                # Skip 12 chars from NSString start (8 for "NSString" + 2 for \x01\x01 + 2 for "+"+length)
                start_offset = ns_string_idx + 12
                raw = text[start_offset:ns_dict_idx]
                
                # Clean: remove control characters
                clean = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', raw)
                
                # Remove trailing garbage - ends with "i" or "I" before NSDictionary marker
                # Look for question mark, period, or other sentence-enders followed by garbage
                clean = re.sub(r'([.!?])[iI]+$', r'\1', clean)
                clean = re.sub(r'[iI]{1,2}[A-Z]?[^a-z]*$', '', clean)
                clean = re.sub(r'[^A-Za-z0-9.!?,\'\"\s\-…]+$', '', clean)
                
                if len(clean) > 3:
                    return clean.strip()
            
            # Method 2: Fallback - find longest readable sequence
            readable = re.sub(r'[^\x20-\x7E\u2018\u2019\u201C\u201D\u2014\u2026]', ' ', text)
            parts = re.findall(r'[A-Za-z][A-Za-z0-9\s.,!?\'\"\-]{10,}', readable)
            if parts:
                return max(parts, key=len).strip()
            
            return ""
        except Exception:
            return ""

    def fetch_recent_history(self, *, handle: str, limit: int = settings.RECENT_HISTORY_LIMIT) -> list[dict[str, Any]]:
        """Fetch recent messages (both directions) for a handle.
        
        Uses chat_message_join to capture BOTH incoming and outgoing messages.
        Handles SMS quirk where outgoing text is in attributedBody, not text column.
        """

        query = """
        SELECT
            m.ROWID AS message_rowid,
            m.is_from_me AS is_from_me,
            COALESCE(m.text, '') AS text,
            m.attributedBody AS attributed_body,
            m.date AS date
        FROM message m
        JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
        JOIN chat c ON c.ROWID = cmj.chat_id
        WHERE (c.chat_identifier = ? OR c.chat_identifier LIKE '%' || ? || '%')
          AND (COALESCE(m.text, '') <> '' OR m.attributedBody IS NOT NULL)
        ORDER BY m.date DESC
        LIMIT ?
        """

        with connect_readonly(
            self.chat_db_path,
            retries=settings.DB_LOCKED_RETRIES,
            backoff_seconds=settings.DB_LOCKED_BACKOFF_SECONDS,
        ) as conn:
            rows = fetch_all(conn, query, (handle, handle, limit))

        history: list[dict[str, Any]] = []
        for r in reversed(rows):
            # Get text from text column, or extract from attributedBody if empty
            msg_text = str(r["text"]).strip()
            if not msg_text and r["attributed_body"]:
                msg_text = self._extract_text_from_attributed_body(r["attributed_body"])
            
            if not msg_text:
                continue  # Skip messages with no extractable text
                
            history.append(
                {
                    "message_rowid": int(r["message_rowid"]),
                    "role": "assistant" if int(r["is_from_me"]) == 1 else "user",
                    "text": msg_text,
                    "date": r["date"],
                }
            )

        return history

    def fetch_last_messages_with_timestamps(self, *, handle: str, limit: int = 3) -> list[dict[str, Any]]:
        """
        Fetch last N messages with human-readable timestamps for EC's immediate context.
        Returns messages in chronological order (oldest first).
        
        NOTE: Reading from chat.db does NOT trigger read receipts.
        Read receipts are only sent when iMessage UI marks the conversation as viewed.
        """
        import time
        from datetime import datetime
        
        # iMessage stores dates as nanoseconds since 2001-01-01 (Apple epoch)
        APPLE_EPOCH_OFFSET = 978307200  # Seconds between Unix epoch (1970) and Apple epoch (2001)
        
        query = """
        SELECT
            m.ROWID AS message_rowid,
            m.is_from_me AS is_from_me,
            COALESCE(m.text, '') AS text,
            m.attributedBody AS attributed_body,
            m.date AS date
        FROM message m
        JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
        JOIN chat c ON c.ROWID = cmj.chat_id
        WHERE (c.chat_identifier = ? OR c.chat_identifier LIKE '%' || ? || '%')
          AND (COALESCE(m.text, '') <> '' OR m.attributedBody IS NOT NULL)
        ORDER BY m.date DESC
        LIMIT ?
        """

        with connect_readonly(
            self.chat_db_path,
            retries=settings.DB_LOCKED_RETRIES,
            backoff_seconds=settings.DB_LOCKED_BACKOFF_SECONDS,
        ) as conn:
            rows = fetch_all(conn, query, (handle, handle, limit))

        messages: list[dict[str, Any]] = []
        now = time.time()
        
        for r in reversed(rows):  # Reverse to get chronological order
            raw_date = r["date"]
            
            # Convert Apple timestamp to Unix timestamp
            # chat.db stores dates in nanoseconds since 2001-01-01
            if raw_date and raw_date > 0:
                # Check if it's in nanoseconds (very large number) or already seconds
                if raw_date > 1e12:  # Nanoseconds
                    unix_ts = (raw_date / 1e9) + APPLE_EPOCH_OFFSET
                else:  # Already in seconds from Apple epoch
                    unix_ts = raw_date + APPLE_EPOCH_OFFSET
            else:
                unix_ts = now  # Fallback
            
            # Calculate time since message
            seconds_ago = now - unix_ts
            
            # Human-readable time ago (Strictly descriptive, no emotional markers)
            if seconds_ago < 60:
                time_ago = "just now"
            elif seconds_ago < 3600:
                mins = int(seconds_ago / 60)
                time_ago = f"{mins}m ago"
            elif seconds_ago < 86400:
                hrs = int(seconds_ago / 3600)
                time_ago = f"{hrs}h ago"
            elif seconds_ago < 172800:  # 24-48 hours
                hrs = int(seconds_ago / 3600)
                time_ago = f"Yesterday ({hrs}h ago)"
            else:
                days = int(seconds_ago / 86400)
                time_ago = f"{days} days ago"
            
            # Format the timestamp
            try:
                dt = datetime.fromtimestamp(unix_ts)
                formatted_time = dt.strftime("%I:%M %p").lstrip("0")  # "9:45 PM"
            except:
                formatted_time = "unknown"
            
            sender = "You" if int(r["is_from_me"]) == 1 else "Them"
            role = "assistant" if int(r["is_from_me"]) == 1 else "user"
            
            # Get text from text column, or extract from attributedBody if empty (SMS quirk)
            text = str(r["text"]).strip()
            if not text and r["attributed_body"]:
                text = self._extract_text_from_attributed_body(r["attributed_body"])
            
            if not text:
                continue  # Skip messages with no extractable text
            
            messages.append({
                "sender": sender,
                "role": role,
                "text": text,
                "time": formatted_time,
                "time_ago": time_ago,
                "unix_ts": unix_ts,
            })

        return messages


MessageWatcher = iMessageWatcher
