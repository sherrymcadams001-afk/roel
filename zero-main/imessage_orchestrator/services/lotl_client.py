"""
LotL Client - Simple Python interface for the LotL Controller API

Usage:
    from lotl_client import LotLClient
    
    client = LotLClient()
    
    # Simple text prompt
    response = client.chat("What is 2 + 2?")
    print(response)  # "4"
    
    # With image
    response = client.chat("Describe this image", images=["screenshot.png"])
    print(response)
    
    # Async version
    response = await client.achat("Hello")
"""

import base64
import httpx
from pathlib import Path
from typing import Union, Optional


class LotLClient:
    """
    Client for the LotL (Living-off-the-Land) Controller API.
    
    Routes prompts through a logged-in Google AI Studio session
    to bypass API quotas and rate limits.
    """
    
    def __init__(
        self,
        base_url: str = "http://localhost:3000",
        timeout: float = 180.0
    ):
        """
        Initialize the LotL client.
        
        Args:
            base_url: Controller URL (default: http://localhost:3000)
            timeout: Request timeout in seconds (default: 180)
        """
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
    
    def _encode_image(self, image: Union[str, bytes, Path]) -> str:
        """
        Encode an image to base64 data URL.
        
        Args:
            image: File path, bytes, or existing base64 string
            
        Returns:
            Base64 data URL string
        """
        # Already a data URL
        if isinstance(image, str) and image.startswith("data:image"):
            return image
        
        # File path
        if isinstance(image, (str, Path)):
            path = Path(image)
            if not path.exists():
                raise FileNotFoundError(f"Image not found: {path}")
            
            # Detect MIME type
            suffix = path.suffix.lower()
            mime_types = {
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".gif": "image/gif",
                ".webp": "image/webp"
            }
            mime = mime_types.get(suffix, "image/png")
            
            with open(path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            
            return f"data:{mime};base64,{b64}"
        
        # Raw bytes
        if isinstance(image, bytes):
            b64 = base64.b64encode(image).decode()
            return f"data:image/png;base64,{b64}"
        
        raise ValueError(f"Unsupported image type: {type(image)}")
    
    def health(self) -> dict:
        """
        Check if the controller is running.
        
        Returns:
            Health status dict with 'status' and 'message' keys
            
        Raises:
            ConnectionError: If controller is not reachable
        """
        try:
            with httpx.Client(timeout=5.0) as client:
                response = client.get(f"{self.base_url}/health")
                return response.json()
        except httpx.ConnectError:
            raise ConnectionError(
                "Cannot connect to LotL Controller. "
                "Is it running on localhost:3000?"
            )
    
    def is_available(self) -> bool:
        """Check if the controller is available."""
        try:
            health = self.health()
            return health.get("status") == "ok"
        except:
            return False
    
    def chat(
        self,
        prompt: str,
        images: Optional[list] = None,
        timeout: Optional[float] = None,
        session_id: Optional[str] = None,
        fresh: bool = False,
        platform: str = 'gemini' 
    ) -> str:
        """
        Send a prompt to an AI platform and get a response.
        
        Args:
            prompt: The text prompt to send
            images: Optional list of image paths, bytes, or base64 strings
            timeout: Override default timeout (seconds)
            session_id: Optional session identifier
            fresh: Whether to force a fresh session (if supported)
            platform: Target platform ('gemini', 'chatgpt', 'copilot', 'whatsapp') - default 'gemini'
            
        Returns:
            The AI model's response text
            
        Raises:
            ConnectionError: If controller is not reachable
            RuntimeError: If platform returns an error
        """
        import time
        import random
        
        payload = {"prompt": prompt}

        if session_id:
            payload["sessionId"] = str(session_id)

        if fresh:
            payload["fresh"] = True
        
        if images:
            if platform == 'chatgpt' or platform == 'whatsapp':
                # These platforms don't support image input in this controller version
                pass 
            else:
                payload["images"] = [self._encode_image(img) for img in images]
        
        # Determine endpoint
        endpoint = "/gemini" # Default
        if platform == 'chatgpt':
            endpoint = "/chatgpt"
        elif platform == 'copilot':
            endpoint = "/copilot"
        elif platform == 'whatsapp':
            endpoint = "/whatsapp"
        elif platform == 'aistudio':
            endpoint = "/aistudio"
            
        max_retries = 5
        base_delay = 2.0
        
        last_error = None
        
        for attempt in range(max_retries):
            try:
                # Calculate effective timeout for this attempt
                current_timeout = timeout or self.timeout
                
                with httpx.Client(timeout=current_timeout) as client:
                    response = client.post(
                        f"{self.base_url}{endpoint}",
                        json=payload
                    )
                    
                    # Handle 503 Busy BEFORE raise_for_status to get proper backoff
                    if response.status_code == 503:
                        try:
                            data = response.json()
                            elapsed = data.get("elapsed", 0)
                            print(f"[LotLClient] Server busy ({elapsed}s elapsed). Waiting before retry...")
                        except:
                            print(f"[LotLClient] Server returned 503 Busy. Waiting before retry...")
                        # Use longer backoff for busy - the current request needs to finish
                        raise RuntimeError("LotL Server Busy (503)")
                    
                    # Raise for other 4xx/5xx status codes
                    response.raise_for_status()
                    
                    data = response.json()
                    
                    if data.get("success"):
                        reply = data["reply"]
                        # Validate the reply isn't an error message
                        if reply and str(reply).strip().lower().startswith("error"):
                            raise RuntimeError(f"LotL returned error response: {reply[:100]}")
                        return reply
                    else:
                        error_msg = data.get("error", "Unknown error")
                        is_busy = data.get("busy", False)
                        # If server is busy, wait and retry
                        if is_busy or "busy" in error_msg.lower():
                            raise RuntimeError(f"LotL Server Busy: {error_msg}")
                        # If it's a transient error, we'll catch and retry
                        raise RuntimeError(f"LotL API Error: {error_msg}")
                        
            except httpx.HTTPStatusError as e:
                last_error = e
                # Handle 503 Busy responses explicitly (shouldn't reach here due to above check)
                if e.response.status_code == 503:
                    try:
                        data = e.response.json()
                        if data.get("busy"):
                            print(f"[LotLClient] Server busy (HTTPStatusError path), waiting...")
                    except:
                        pass
                # Will retry with backoff
                pass
                        
            except (httpx.ConnectError, httpx.TimeoutException, httpx.ReadTimeout, ConnectionError) as e:
                last_error = e
                # Network/Timeout errors - retry immediately with backoff
                pass
                
            except RuntimeError as e:
                # check if error allows retry
                last_error = e
                if "rate limit" in str(e).lower() or "busy" in str(e).lower():
                    pass # Retry
                else:
                    # Logic error or non-recoverable
                    # But actually, often LotL errors are weird UI states, so retrying MIGHT help.
                    # Let's retry on almost everything for "ensure requests always get sent"
                    pass

            except Exception as e:
                last_error = e
                # Unexpected error
                pass
            
            # Backoff before next attempt
            if attempt < max_retries - 1:
                sleep_time = base_delay * (2 ** attempt) + random.uniform(0, 1)
                print(f"[LotLClient] Request failed (Attempt {attempt+1}/{max_retries}). Retrying in {sleep_time:.1f}s... Error: {last_error}")
                time.sleep(sleep_time)
        
        # If we get here, we failed all retries
        raise last_error or RuntimeError("LotL request failed after retries")
    
    async def achat(
        self,
        prompt: str,
        images: Optional[list] = None,
        timeout: Optional[float] = None,
        session_id: Optional[str] = None,
        fresh: bool = False,
        platform: str = 'gemini'
    ) -> str:
        """
        Async version of chat().
        
        Args:
            prompt: The text prompt to send
            images: Optional list of image paths, bytes, or base64 strings
            timeout: Override default timeout (seconds)
            session_id: Optional session identifier
            fresh: Whether to force a fresh session
            platform: Target platform ('gemini', 'chatgpt', 'copilot', 'whatsapp') - default 'gemini'
        Returns:
            The AI model's response text
        """
        payload = {"prompt": prompt}

        if session_id:
            payload["sessionId"] = str(session_id)

        if fresh:
            payload["fresh"] = True
        
        if images:
            if platform == 'chatgpt' or platform == 'whatsapp':
                pass
            else:
                payload["images"] = [self._encode_image(img) for img in images]
        
        # Determine endpoint
        endpoint = "/gemini" # Default
        if platform == 'chatgpt':
            endpoint = "/chatgpt"
        elif platform == 'copilot':
            endpoint = "/copilot"
        elif platform == 'whatsapp':
            endpoint = "/whatsapp"
        elif platform == 'aistudio':
            endpoint = "/aistudio"

        try:
            async with httpx.AsyncClient(timeout=timeout or self.timeout) as client:
                response = await client.post(
                    f"{self.base_url}{endpoint}",
                    json=payload
                )
                data = response.json()
                
                if data.get("success"):
                    return data["reply"]
                else:
                    raise RuntimeError(data.get("error", "Unknown error"))
                    
        except httpx.ConnectError:
            raise ConnectionError(
                "Cannot connect to LotL Controller. "
                "Is it running on localhost:3000?"
            )
        except httpx.TimeoutException:
            raise TimeoutError(
                f"Request timed out after {timeout or self.timeout}s. "
                "Try increasing the timeout or check AI Studio."
            )

    def send_whatsapp(self, phone: str, message: str) -> str:
        """
        Send a message via WhatsApp Web.
        """
        payload = {
            "prompt": message, 
            "sessionId": phone
        }
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(f"{self.base_url}/whatsapp", json=payload)
            response.raise_for_status()
            data = response.json()
            
            if not data.get("success"):
                raise RuntimeError(f"WhatsApp Error: {data.get('error')}")
                
            return data.get("reply")

    def poll_whatsapp(self) -> list[dict]:
        """
        Poll WhatsApp Web for unread messages.
        Returns: List of dicts like [{"handle": "+123...", "count": "1"}]
        """
        # Short timeout for polling
        with httpx.Client(timeout=5.0) as client:
            try:
                response = client.get(f"{self.base_url}/whatsapp/poll")
                if response.status_code == 404:
                    return [] # No tab open
                response.raise_for_status()
                data = response.json()
                return data.get("unread", [])
            except Exception:
                return []

    def read_whatsapp(self, phone: str) -> str:
        """
        Open the chat with 'phone' in read-only mode and fetch the last message.
        """
        payload = {
            "prompt": "", 
            "sessionId": phone,
            "readOnly": True
        }
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(f"{self.base_url}/whatsapp", json=payload)
            response.raise_for_status()
            data = response.json()
            
            if not data.get("success"):
                raise RuntimeError(f"WhatsApp Error: {data.get('error')}")
                
            return data.get("reply")
    
    def __repr__(self) -> str:
        return f"LotLClient(base_url='{self.base_url}', timeout={self.timeout})"


# Convenience function for quick usage
def ask(prompt: str, images: Optional[list] = None) -> str:
    """
    Quick function to send a prompt to LotL.
    
    Args:
        prompt: The text prompt
        images: Optional list of image paths
        
    Returns:
        AI response text
        
    Example:
        from lotl_client import ask
        print(ask("What is the capital of France?"))
    """
    client = LotLClient()
    return client.chat(prompt, images)


# Module-level client instance for simple usage
_default_client: Optional[LotLClient] = None


def get_client() -> LotLClient:
    """Get or create the default LotL client instance."""
    global _default_client
    if _default_client is None:
        _default_client = LotLClient()
    return _default_client


if __name__ == "__main__":
    # Quick test
    client = LotLClient()
    
    print("Testing LotL Client...")
    print(f"Controller available: {client.is_available()}")
    
    if client.is_available():
        response = client.chat("Say 'Hello from LotL Client!' and nothing else.")
        print(f"Response: {response}")
