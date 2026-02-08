from __future__ import annotations

import os
from pathlib import Path

# Persistent local state file (tracks last processed message rowid)
STATE_FILE: Path = Path(__file__).resolve().parents[1] / "data" / "state.json"

# Contacts directory (profiles are stored as one JSON per handle)
CONTACTS_DIR: Path = STATE_FILE.parent / "contacts"
UNVERIFIED_CONTACTS_DIR: Path = CONTACTS_DIR / "_unverified"


def _parse_handle_list(raw: str) -> set[str]:
    parts = [p.strip() for p in raw.split(",")]
    return {p for p in parts if p}


def _load_handles_from_contacts_dir() -> set[str]:
    if not CONTACTS_DIR.exists():
        return set()
    handles: set[str] = set()
    for p in CONTACTS_DIR.glob("*.json"):
        name = p.stem.strip()
        if not name or name.startswith("_"):
            continue
        handles.add(name)
    return handles


def get_target_handles() -> set[str]:
    """Return the current allowlist.

    Priority:
    1) env TARGET_HANDLES (comma-separated)
    2) data/contacts/*.json stems (dynamic from disk)
    No hardcoded fallback - unknown contacts go through approval flow.
    """
    env_raw = (os.getenv("TARGET_HANDLES") or "").strip()
    if env_raw:
        return _parse_handle_list(env_raw)
    return _load_handles_from_contacts_dir()


# ---- Safety: Only these handles can receive replies. ----
# Priority:
# 1) env TARGET_HANDLES (comma-separated)
# 2) data/contacts/*.json stems (dynamic from disk)
# No hardcoded fallback - system only replies to contacts with profile files
_env_target_handles = (os.getenv("TARGET_HANDLES") or "").strip()
if _env_target_handles:
    TARGET_HANDLES: set[str] = _parse_handle_list(_env_target_handles)
else:
    TARGET_HANDLES: set[str] = _load_handles_from_contacts_dir()

# Unknown-contact policy (WhatsApp-first):
# - Default: do NOT auto-reply to unknown contacts.
# - Instead, generate a suggested reply + risk flags and require operator approval.
AUTO_REPLY_UNKNOWN_CONTACTS: bool = os.getenv("AUTO_REPLY_UNKNOWN_CONTACTS", "false").lower() == "true"

# The Human Operator (You). If set, the agent can text this number for permission.
# Format: "+15550000000"
OPERATOR_HANDLE: str | None = "+18133636801"

# Operator's current location (for Context Analyst time calculations)
OPERATOR_LOCATION: str = os.getenv("OPERATOR_LOCATION", "Los Angeles, CA")

# Poll interval for new messages (seconds)
POLL_INTERVAL_SECONDS: float = 30.0

# How many recent messages to include in the dynamic chat history payload
RECENT_HISTORY_LIMIT: int = 20

# Messages database path
CHAT_DB_PATH: Path = Path.home() / "Library" / "Messages" / "chat.db"

# Logging
LOG_DIR: Path = Path(__file__).resolve().parents[1] / "data" / "logs"
LOG_FILE: Path = LOG_DIR / "imessage_orchestrator.log"
LOG_LEVEL: str = os.getenv("IMESSAGE_LOG_LEVEL", "INFO").upper()

# OpenAI
OPENAI_API_KEY: str | None = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL: str = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

# Anthropic
ANTHROPIC_API_KEY: str | None = os.getenv("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL: str = os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-latest")

# Gemini (Google)
# IMPORTANT: Do not hardcode API keys in this repo. Set env var instead.
GEMINI_API_KEY: str | None = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")
GEMINI_BASE_URL: str | None = os.getenv("GEMINI_BASE_URL")

# LotL (Living off the Land - AI Studio via Chrome CDP)
# Bypasses API quotas by routing through logged-in browser session
LOTL_BASE_URL: str = os.getenv("LOTL_BASE_URL", "http://localhost:3000")
LOTL_TIMEOUT: float = float(os.getenv("LOTL_TIMEOUT", "180"))

# Smart Default logic
# Priority:
# 1) Explicit env var always wins
# 2) Otherwise choose a provider that has credentials configured
# 3) Otherwise default to LotL (browser-routed) so the system can run without API keys
_env_provider = (os.getenv("LLM_PROVIDER") or "").strip().lower()

if _env_provider:
    _default_provider = _env_provider
elif OPENAI_API_KEY:
    _default_provider = "openai"
elif ANTHROPIC_API_KEY:
    _default_provider = "anthropic"
elif GEMINI_API_KEY:
    _default_provider = "gemini"
else:
    _default_provider = "lotl"

LLM_PROVIDER: str = _default_provider

# Retry behavior when the Messages db is locked
DB_LOCKED_RETRIES: int = 3
DB_LOCKED_BACKOFF_SECONDS: float = 0.35

# Services
ENABLE_IMESSAGE: bool = os.getenv("ENABLE_IMESSAGE", "true").lower() == "true"
ENABLE_WHATSAPP: bool = os.getenv("ENABLE_WHATSAPP", "false").lower() == "true"

# Send Queue (non-blocking pacing)
SEND_QUEUE_FILE: Path = STATE_FILE.parent / "send_queue.json"

# WhatsApp message store
WHATSAPP_STORE_DIR: Path = STATE_FILE.parent / "whatsapp_history"

# Provider failover order (csv).  First working provider wins.
# Default chain: primary -> lotl -> gemini -> openai
_env_failover = (os.getenv("LLM_FAILOVER_CHAIN") or "").strip()
LLM_FAILOVER_CHAIN: list[str] = (
    [p.strip().lower() for p in _env_failover.split(",") if p.strip()]
    if _env_failover
    else []  # Empty = no failover; populated at runtime from available providers
)

# Bridge retry settings
BRIDGE_SEND_RETRIES: int = int(os.getenv("BRIDGE_SEND_RETRIES", "3"))
BRIDGE_SEND_BACKOFF: float = float(os.getenv("BRIDGE_SEND_BACKOFF", "2.0"))
