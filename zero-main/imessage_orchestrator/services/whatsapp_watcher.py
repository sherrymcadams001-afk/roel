from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import List, Set
from .lotl_client import LotLClient
from .interfaces import IncomingMessage
from config import settings
from utils.whatsapp_store import WhatsAppMessageStore

logger = logging.getLogger(__name__)


class WhatsAppWatcher:
    """Ingress service: polls WhatsApp Web for new inbound messages via LotL.

    Improvements over PoC:
    - Persistent message store for deduplication and history retrieval.
    - Tracks failed reads so unread notifications are not lost.
    """

    def __init__(self, lotl_client: LotLClient = None) -> None:
        self.client = lotl_client if lotl_client else LotLClient(base_url=settings.LOTL_BASE_URL)
        self._store = WhatsAppMessageStore(
            store_dir=getattr(settings, "WHATSAPP_STORE_DIR", settings.STATE_FILE.parent / "whatsapp_history")
        )
        # Track handles with unread messages that we failed to read,
        # so we can retry on the next poll cycle.
        self._pending_reads: Set[str] = set()

    def initialize(self) -> None:
        if not self.client.is_available():
            logger.warning("WhatsApp Watcher: LotL Controller not available.")

    def poll(self) -> List[IncomingMessage]:
        """
        Poll for unread messages.

        Human-like behavior:
        - Only scan the chat sidebar for unread badges (doesn't mark as read)
        - Return notification of unread WITHOUT reading content yet
        - The orchestrator decides when to actually "read" and respond

        Dedup: uses WhatsAppMessageStore to suppress duplicate notifications.
        """
        messages = []
        handles_with_unread: Set[str] = set()

        try:
            unread_chats = self.client.poll_whatsapp()
            for chat in unread_chats:
                handle = chat.get('handle')
                if not handle:
                    continue
                handles_with_unread.add(handle)

        except Exception as e:
            logger.error(f"WhatsApp Poll Error: {e}")

        # Merge with previously failed reads
        all_unread = handles_with_unread | self._pending_reads

        for handle in all_unread:
            msg_id = f"wa_{handle}_{int(time.time() * 1000)}"

            messages.append(IncomingMessage(
                message_rowid=msg_id,
                handle=handle,
                text=f"__UNREAD_PENDING__:{handle}",
                service="WhatsApp",
                date=int(time.time() * 1000000000)
            ))

        return messages

    def read_message(self, handle: str) -> str | None:
        """
        Actually open and read a chat - this marks it as "read" (blue ticks).
        Call this only when ready to process/respond.

        Stores the message in the persistent store and manages the pending-reads set.
        """
        try:
            text = self.client.read_whatsapp(handle)
            if text and text.strip():
                # Remove from pending reads on success
                self._pending_reads.discard(handle)

                # Persist & dedup
                is_new = self._store.store_message(
                    handle=handle,
                    text=text,
                    is_from_me=False,
                    service="WhatsApp",
                )
                if not is_new:
                    logger.info(f"[WA_WATCH] Duplicate message suppressed for {handle}")
                    return None

                return text
            else:
                logger.warning(f"[WA_WATCH] read_whatsapp returned empty for {handle}")
                # Keep in pending reads for retry
                self._pending_reads.add(handle)
                return None

        except Exception as e:
            logger.error(f"WhatsApp Read Error for {handle}: {e}")
            # Keep in pending reads so we retry next cycle
            self._pending_reads.add(handle)
            return None

    def store_outbound(self, handle: str, text: str) -> None:
        """Record an outbound message for history retrieval."""
        self._store.store_message(
            handle=handle,
            text=text,
            is_from_me=True,
            service="WhatsApp",
        )

    def fetch_recent_history(self, *, handle: str, limit: int = 20) -> list[dict]:
        """Retrieve recent messages from the persistent store."""
        return self._store.fetch_history(handle, limit=limit)
