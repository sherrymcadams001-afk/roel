import logging
import time

from .lotl_client import LotLClient
from config import settings

logger = logging.getLogger(__name__)

class WhatsAppBridge:
    def __init__(self, lotl_client: LotLClient = None):
        if lotl_client is None:
            self.client = LotLClient(base_url=settings.LOTL_BASE_URL)
        else:
            self.client = lotl_client

    def send_message(self, handle: str, message: str, service: str = "WhatsApp") -> bool:
        """
        Send a message via WhatsApp Web using the LotL controller.
        Retries with exponential backoff on transient failures.
        """
        retries = getattr(settings, "BRIDGE_SEND_RETRIES", 3)
        backoff = getattr(settings, "BRIDGE_SEND_BACKOFF", 2.0)

        # Clean handle (remove +, spaces, dashes)
        clean_handle = handle.replace("+", "").replace("-", "").replace(" ", "").strip()

        for attempt in range(1, retries + 1):
            try:
                logger.info(f"Sending WhatsApp message to {clean_handle} (attempt {attempt}/{retries})...")
                self.client.send_whatsapp(phone=clean_handle, message=message)
                return True
            except Exception as e:
                logger.error(f"WhatsApp send attempt {attempt}/{retries} failed: {e}")
                if attempt < retries:
                    wait = backoff * (2 ** (attempt - 1))
                    logger.warning(f"[WA_BRIDGE] Retrying in {wait:.1f}s...")
                    time.sleep(wait)

        logger.error(f"[WA_BRIDGE] All {retries} send attempts failed for {clean_handle}")
        return False
