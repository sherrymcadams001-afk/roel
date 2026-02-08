import subprocess
import logging
import time

from config import settings

logger = logging.getLogger(__name__)


class iMessageBridge:
    def send_message(self, handle: str, message: str, service: str = "iMessage") -> bool:
        """
        Executes AppleScript to send a message via the local Mac Messages app.
        Retries with exponential backoff on transient failures.
        Tries: direct chat lookup by ID, then buddy methods.
        """
        retries = getattr(settings, "BRIDGE_SEND_RETRIES", 3)
        backoff = getattr(settings, "BRIDGE_SEND_BACKOFF", 2.0)

        for attempt in range(1, retries + 1):
            success = self._try_send(handle, message, service)
            if success:
                return True
            if attempt < retries:
                wait = backoff * (2 ** (attempt - 1))
                logger.warning(
                    "[BRIDGE] Send attempt %d/%d failed for %s. Retrying in %.1fs",
                    attempt, retries, handle, wait,
                )
                time.sleep(wait)

        logger.error("[BRIDGE] All %d send attempts failed for %s", retries, handle)
        return False

    def _try_send(self, handle: str, message: str, service: str = "iMessage") -> bool:
        """Single send attempt via AppleScript."""
        # APPLESCRIPT ESCAPING LOGIC
        safe_message = message.replace("\\", "\\\\").replace('"', '\\"')
        safe_handle = handle.replace("\\", "\\\\").replace('"', '\\"')
        
        # Normalize handle - ensure +1 prefix for US numbers
        normalized_handle = safe_handle
        if safe_handle.isdigit() and len(safe_handle) == 10:
            normalized_handle = "+1" + safe_handle
        elif safe_handle.isdigit() and len(safe_handle) == 11 and safe_handle.startswith("1"):
            normalized_handle = "+" + safe_handle
        elif not safe_handle.startswith("+") and safe_handle.replace("-", "").replace(" ", "").isdigit():
            digits = safe_handle.replace("-", "").replace(" ", "")
            if len(digits) == 10:
                normalized_handle = "+1" + digits
            elif len(digits) == 11 and digits.startswith("1"):
                normalized_handle = "+" + digits
        
        applescript = f'''
        tell application "Messages"
            -- STRATEGY 1: Direct Chat ID lookup (most reliable)
            -- Try iMessage chat first, then SMS chat
            try
                set theChat to a reference to chat id ("iMessage;-;" & "{normalized_handle}")
                send "{safe_message}" to theChat
                return "SUCCESS: Direct iMessage Chat"
            on error
                try
                    set theChat to a reference to chat id ("SMS;-;" & "{normalized_handle}")
                    send "{safe_message}" to theChat
                    return "SUCCESS: Direct SMS Chat"
                on error
                    -- STRATEGY 2: Buddy method fallback
                    try
                        send "{safe_message}" to buddy "{normalized_handle}" of (1st service whose service type is iMessage)
                        return "SUCCESS: iMessage buddy"
                    on error
                        try
                            send "{safe_message}" to buddy "{normalized_handle}" of (1st service whose service type is SMS)
                            return "SUCCESS: SMS buddy"
                        on error e
                            return "ERROR: All strategies failed. " & e
                        end try
                    end try
                end try
            end try
        end tell
        '''
        
        try:
            result = subprocess.run(["osascript", "-"], input=applescript.encode('utf-8'), check=False, capture_output=True)
            output = result.stdout.decode('utf-8').strip()
            stderr = result.stderr.decode('utf-8').strip()
            
            logger.info(f"[BRIDGE] Sent to {normalized_handle}. Result: '{output}'")
            
            if result.returncode != 0:
                 logger.error(f"[BRIDGE ERROR] OsaScript failed. returncode={result.returncode}, stderr={stderr}")
                 return False

            if output.startswith("ERROR"):
                logger.error(f"AppleScript Error sending to {handle}: {output}")
                return False
            return True
        except Exception as e:
            logger.error(f"ERROR: Failed to send message to {handle}. {e}")
            return False
