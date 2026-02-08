from typing import Protocol, List, Any
from dataclasses import dataclass

@dataclass(frozen=True)
class IncomingMessage:
    message_rowid: int | str
    handle: str
    text: str
    service: str
    date: int = 0

class TransportBridge(Protocol):
    def send_message(self, handle: str, message: str, service: str) -> bool:
        """Sends a message to the specified handle via the service."""
        ...

class TransportWatcher(Protocol):
    def initialize(self) -> None:
        """Perform any necessary startup checks (e.g. DB existence, Browser launch)."""
        ...
        
    def poll(self) -> List[IncomingMessage]:
        """Poll the transport for new messages."""
        ...
