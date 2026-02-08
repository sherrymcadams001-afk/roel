from __future__ import annotations

import warnings
warnings.filterwarnings("ignore", category=FutureWarning, module="google.generativeai")

import logging
import sys
import time
import random
import datetime
import json
import threading
from typing import Callable, TypeVar
from pathlib import Path
from zoneinfo import ZoneInfo

from config import settings
from services.analyst_service import AnalystService
from services.archivist import Archivist
from services.delegate import Delegate, RateLimitError
from services.bridge import iMessageBridge
from services.whatsapp_bridge import WhatsAppBridge
from services.watcher import MessageWatcher, IncomingMessage, iMessageWatcher
from services.whatsapp_watcher import WhatsAppWatcher
from services.composite_watcher import CompositeWatcher
from services.lotl_client import LotLClient
from services import policy
from services.send_queue import SendQueue
from services.instance_lock import acquire_instance_lock, InstanceAlreadyRunning
from utils.atomic import atomic_write_json

logger = logging.getLogger(__name__)

T = TypeVar("T")

# --- PERSISTENCE PATH FOR APPROVALS ---
APPROVALS_FILE = settings.STATE_FILE.parent / "pending_approvals.json"
DEFERRED_OUTBOX_FILE = settings.STATE_FILE.parent / "deferred_outbox.json"


# Re-export for backward compatibility; actual implementation in utils.atomic
_atomic_write_json = atomic_write_json

def _configure_logging() -> None:
    settings.LOG_DIR.mkdir(parents=True, exist_ok=True)
    
    root_logger = logging.getLogger()
    if any(isinstance(h, logging.FileHandler) for h in root_logger.handlers):
        return

    level = getattr(logging, settings.LOG_LEVEL, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(settings.LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )

    # Dedicated audit logger for blocked messages (leak detection post-mortem)
    audit_logger = logging.getLogger("orchestrator.audit")
    audit_logger.propagate = False  # Don't duplicate to root
    audit_file = settings.LOG_DIR / "blocked_messages_audit.log"
    audit_handler = logging.FileHandler(audit_file, encoding="utf-8")
    audit_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    )
    audit_logger.addHandler(audit_handler)
    audit_logger.setLevel(logging.INFO)

class Orchestrator:
    """
    Project Zero: Phase 2 Orchestrator (State Machine Architecture)
    
    State Machine States per Handle:
      - IDLE: No pending action. Agent can initiate or respond.
      - AWAITING_APPROVAL: A draft exists. Agent is blocked until Operator responds.
    """
    SHADOW_MODE_TRUST_THRESHOLD = 3
    
    def __init__(self, api_key: str, watcher: MessageWatcher = None):
        self.archivist = Archivist(contacts_dir=settings.STATE_FILE.parent / "contacts")
        self.delegate = Delegate(api_key, "Project Zero")
        
        self.bridges = {
            "iMessage": iMessageBridge(),
            "SMS": iMessageBridge(), # SMS uses same bridge
            "WhatsApp": WhatsAppBridge()
        }
        # Fallback default
        if settings.ENABLE_WHATSAPP and not settings.ENABLE_IMESSAGE:
            self.bridge = self.bridges["WhatsApp"]
        else:
            self.bridge = self.bridges["iMessage"]
        
        self.watcher = watcher

        # One-time diagnostics: helps confirm which handles are being scanned.
        self._logged_proactive_handles_once = False
        
        # CRITICAL: LLM Call Mutex - prevents concurrent requests to LotL
        # This ensures we never interrupt a generation with a new prompt
        self._llm_lock = threading.Lock()
        
        # Unified Analyst Service (Tier 1-5)
        try:
            lotl = LotLClient(base_url=settings.LOTL_BASE_URL, timeout=60.0)
            self.analyst = AnalystService(api_key=api_key, lotl_client=lotl)
            logger.info("[INIT] Unified Analyst Service initialized with LotL client")
        except Exception as e:
            logger.warning(f"[INIT] Analyst Service fallback mode (no LotL): {e}")
            self.analyst = AnalystService(api_key=api_key, lotl_client=None)
        
        # CRITICAL: Load persisted approvals on startup
        self.pending_approvals = self._load_approvals()
        logger.info(f"Loaded {len(self.pending_approvals)} pending approvals from disk.")

        # Quiet-hours outbox: inbound messages deferred until quiet hours ends.
        self.deferred_outbox = self._load_deferred_outbox()
        if self.deferred_outbox:
            logger.info(f"Loaded {len(self.deferred_outbox)} deferred outbox handles from disk.")

        # Non-blocking send queue (replaces time.sleep pacing in main loop)
        self.send_queue = SendQueue(settings.SEND_QUEUE_FILE)
        if self.send_queue.depth:
            logger.info(f"Loaded {self.send_queue.depth} pending scheduled sends from disk.")
        
        # Startup cleanup: clear stale state
        self._startup_cleanup()

    def _startup_cleanup(self) -> None:
        """
        Clean stale state on startup:
        - Remove deprecated/unused fields from contact profiles
        - Clear pending_approvals older than 24 hours
        """
        contacts_dir = settings.STATE_FILE.parent / "contacts"
        if not contacts_dir.exists():
            return
            
        now = datetime.datetime.now()
        cleaned_profiles = 0
        cleaned_approvals = 0
        
        # 1. Clear deprecated fields only.
        for profile_path in contacts_dir.glob("*.json"):
            try:
                with open(profile_path, 'r') as f:
                    profile = json.load(f)
                
                modified = False
                
                # Remove deprecated fields (fields that are NO LONGER USED at all)
                deprecated_fields = [
                    "recent_interactions",  # No longer stored in profile
                    "search_vectors",  # Never implemented
                    "shadow_mode_approvals",  # Moved to separate file
                    "linguistic_mirror",  # Never used
                    "immediate_context",  # Computed dynamically, not stored
                    "cadence_preference"  # Removed from schema
                ]
                # Note: mute_agent and requires_approval are NOT deprecated - they're
                # orchestrator-only control fields in ORCHESTRATOR_STATE_SCHEMA
                
                # Also clean up deprecated sub-fields in operational_state
                if "operational_state" in profile:
                    deprecated_subfields = ["limerence_index", "compliance_score", "active_tactic"]
                    for field in deprecated_subfields:
                        if field in profile["operational_state"]:
                            del profile["operational_state"][field]
                            modified = True
                
                # Also clean up deprecated sub-fields in psychometric_profile
                if "psychometric_profile" in profile:
                    if "risk_tolerance" in profile["psychometric_profile"]:
                        del profile["psychometric_profile"]["risk_tolerance"]
                        modified = True
                
                for field in deprecated_fields:
                    if field in profile:
                        del profile[field]
                        modified = True
                
                if modified:
                    _atomic_write_json(profile_path, profile, indent=4)
                    cleaned_profiles += 1
                    
            except Exception as e:
                logger.warning(f"[CLEANUP] Failed to clean {profile_path}: {e}")
        
        # 2. Clear pending_approvals older than 24 hours
        # Backward-compatible: timestamp may be ISO string or epoch float.
        stale_handles: list[str] = []
        for handle, data in self.pending_approvals.items():
            ts = data.get("timestamp")
            if ts is None:
                continue

            approval_time: datetime.datetime | None = None
            try:
                if isinstance(ts, (int, float)):
                    approval_time = datetime.datetime.fromtimestamp(float(ts))
                elif isinstance(ts, str) and ts:
                    approval_time = datetime.datetime.fromisoformat(ts)
            except Exception:
                approval_time = None

            if approval_time is None:
                continue

            age_hours = (now - approval_time).total_seconds() / 3600
            if age_hours > 24:
                stale_handles.append(handle)
                logger.info(f"[CLEANUP] Removing stale approval for {handle} ({age_hours:.1f}h old)")
        
        for handle in stale_handles:
            del self.pending_approvals[handle]
            cleaned_approvals += 1
        
        if cleaned_approvals > 0:
            self._save_approvals()
        
        if cleaned_profiles > 0 or cleaned_approvals > 0:
            logger.info(f"[CLEANUP] Startup complete: {cleaned_profiles} profiles cleaned, {cleaned_approvals} stale approvals removed")

    def _load_approvals(self) -> dict:
        """Load pending approvals from disk for crash recovery."""
        if APPROVALS_FILE.exists():
            try:
                with open(APPROVALS_FILE, 'r') as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Failed to load approvals: {e}")
        return {}

    def _save_approvals(self) -> None:
        """Persist pending approvals to disk."""
        try:
            _atomic_write_json(APPROVALS_FILE, self.pending_approvals, indent=2)
        except Exception as e:
            logger.error(f"Failed to save approvals: {e}")

    def _load_deferred_outbox(self) -> dict:
        if DEFERRED_OUTBOX_FILE.exists():
            try:
                with open(DEFERRED_OUTBOX_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return data if isinstance(data, dict) else {}
            except Exception as e:
                logger.warning(f"Failed to load deferred outbox: {e}")
        return {}

    def _save_deferred_outbox(self) -> None:
        try:
            _atomic_write_json(DEFERRED_OUTBOX_FILE, self.deferred_outbox, indent=2)
        except Exception as e:
            logger.error(f"Failed to save deferred outbox: {e}")

    def _is_in_quiet_hours(self, profile: dict) -> tuple[bool, int]:
        pacing = profile.get("pacing_engine", {})
        quiet = pacing.get("quiet_hours", {})
        if not quiet.get("enabled", False):
            return False, -1

        try:
            tz_name = profile.get("identity_matrix", {}).get("timezone", "America/New_York")
            tz = ZoneInfo(tz_name)
            now_hour = datetime.datetime.now(tz).hour
        except Exception:
            now_hour = datetime.datetime.now().hour

        start = quiet.get("start_hour", 23)
        end = quiet.get("end_hour", 8)

        if start < end:
            in_window = start <= now_hour < end
        else:
            in_window = now_hour >= start or now_hour < end

        return bool(in_window), int(now_hour)

    def _defer_inbound_quiet_hours(self, *, msg: IncomingMessage, profile: dict, now_hour: int) -> None:
        handle = str(msg.handle)
        inbound_text = str(msg.text or "").strip()
        if not inbound_text:
            return

        now_epoch = time.time()
        entry = self.deferred_outbox.get(handle)
        if not isinstance(entry, dict):
            entry = {
                "first_deferred_epoch": now_epoch,
                "deferred_count": 0,
            }

        entry["last_deferred_epoch"] = now_epoch
        entry["last_deferred_iso"] = datetime.datetime.now().isoformat()
        entry["service"] = str(getattr(msg, "service", "iMessage") or "iMessage")
        entry["message_rowid"] = getattr(msg, "message_rowid", "deferred")
        entry["latest_inbound_text"] = inbound_text[:4000]
        entry["deferred_count"] = int(entry.get("deferred_count", 0)) + 1
        entry["reason"] = f"quiet_hours@{now_hour}"

        self.deferred_outbox[handle] = entry
        self._save_deferred_outbox()

        # Persist time-touch so the orchestrator remembers recency without fabricating an exchange.
        try:
            profile.setdefault("identity_matrix", {})
            profile["identity_matrix"]["last_interaction_epoch"] = now_epoch
            self.archivist.update_profile(handle, profile)
        except Exception:
            pass

    def drain_deferred_outbox(self, *, max_per_tick: int = 5) -> int:
        """Attempt deferred sends once quiet hours ends.

        Returns the number of handles drained.
        """
        if not self.deferred_outbox:
            return 0

        drained = 0

        # Oldest-first drain to preserve temporal order.
        def _key(item: tuple[str, dict]) -> float:
            data = item[1] if isinstance(item[1], dict) else {}
            ts = data.get("last_deferred_epoch")
            try:
                return float(ts)
            except Exception:
                return 0.0

        for handle, entry in sorted(list(self.deferred_outbox.items()), key=_key):
            if drained >= max_per_tick:
                break
            if not isinstance(entry, dict):
                continue

            profile = self.archivist.load_profile(handle)
            if profile.get("mute_agent", False):
                continue

            in_quiet, _ = self._is_in_quiet_hours(profile)
            if in_quiet:
                continue

            inbound_text = str(entry.get("latest_inbound_text") or "").strip()
            if not inbound_text:
                # Nothing actionable; drop the entry.
                del self.deferred_outbox[handle]
                self._save_deferred_outbox()
                drained += 1
                continue

            svc = str(entry.get("service") or "iMessage")
            rowid = entry.get("message_rowid") or "deferred"
            msg = IncomingMessage(
                message_rowid=rowid,
                handle=handle,
                text=inbound_text,
                service=svc,
                date=0,
            )

            # Best-effort history fetch (keeps behavior close to normal inbound path).
            history: list = []
            try:
                if self.watcher and hasattr(self.watcher, "fetch_recent_history"):
                    history = self.watcher.fetch_recent_history(handle=handle)
            except Exception:
                history = []

            try:
                self.handle_incoming(msg, history)
            except RateLimitError:
                # Keep entry for retry after cooldown.
                raise
            except Exception as e:
                logger.exception(f"[DEFERRED] Failed to drain deferred outbox for {handle}: {e}")
                continue

            # If we got here, the message was either sent or queued for approval.
            # Remove from outbox to avoid duplicate handling.
            try:
                del self.deferred_outbox[handle]
                self._save_deferred_outbox()
            except Exception:
                pass

            drained += 1

        return drained
    
    def _synchronized_llm_call(self, payload: dict, context: str = "unknown") -> str:
        """
        CRITICAL: Synchronized LLM call wrapper.
        
        Ensures only ONE LLM request is in flight at a time across the entire
        orchestrator. This prevents the "internal error" caused by interrupting
        a generation with a new prompt.
        
        Args:
            payload: The payload to send to delegate.generate_reply()
            context: Description for logging (e.g. "reactive:+1234", "proactive:+5678")
            
        Returns:
            The generated reply text
            
        Raises:
            Exception: If generation fails after lock is acquired
        """
        return self._with_llm_lock(
            context=context,
            fn=lambda: self.delegate.generate_reply(payload),
        )

    def _with_llm_lock(self, *, context: str, fn: Callable[[], T]) -> T:
        """Run any LotL/LLM-adjacent operation under the global mutex.

        This must guard *all* operations that can touch the browser-backed
        LotL controller (Tier-1 analyst, delegate generation, post-analysis),
        otherwise retries/fresh sessions can interleave and interrupt active
        generations.
        """
        acquired = self._llm_lock.acquire(timeout=300)  # 5 min max wait
        if not acquired:
            raise RuntimeError(f"[SYNC] Timeout waiting for LLM lock ({context})")

        try:
            logger.info(f"[SYNC] Acquired LLM lock for {context}")
            result = fn()
            logger.info(f"[SYNC] Released LLM lock for {context} (success)")
            return result
        except Exception as e:
            logger.error(f"[SYNC] Released LLM lock for {context} (error: {e})")
            raise
        finally:
            self._llm_lock.release()
    
    def _fetch_recent_messages(self, handle: str, limit: int = 3) -> list:
        """Fetch last N messages with timestamps for EC's immediate context."""
        if self.watcher and hasattr(self.watcher, "fetch_last_messages_with_timestamps"):
            try:
                return self.watcher.fetch_last_messages_with_timestamps(handle=handle, limit=limit)
            except Exception as e:
                logger.warning(f"Recent-message fetch failed for {handle}: {e}")

        # Fallback for transports without timestamped history.
        if self.watcher and hasattr(self.watcher, "fetch_recent_history"):
            try:
                hist = self.watcher.fetch_recent_history(handle=handle, limit=limit)
                # Provide a compatible minimal shape if needed.
                if hist and isinstance(hist, list) and isinstance(hist[0], dict) and "time" in hist[0]:
                    return hist
                now_unix = int(time.time())
                now_str = datetime.datetime.fromtimestamp(now_unix).strftime("%Y-%m-%d %H:%M:%S")
                out = []
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
            except Exception as e:
                logger.warning(f"Recent-history fallback failed for {handle}: {e}")
        return []

    def _calculate_response_delay(self, current_msg: IncomingMessage, history: list) -> float:
        """
        Calculates delay using the V2 Pacing Engine (Deep Limerence).
        Uses 'average_latency_seconds' and 'variable_reward_ratio' to create addictive rhythms.
        """
        profile = self.archivist.load_profile(current_msg.handle)
        pacing = profile.get("pacing_engine", {})
        
        # Pacing Engine Parameters
        avg_latency = float(pacing.get("average_latency_seconds", 60.0))
        reward_variability = float(pacing.get("variable_reward_ratio", 0.5)) # 0.0 to 1.0

        # Calculate a randomized delay centered on the average
        # e.g. Avg 60, Var 0.5 => Range [30, 90]
        min_delay = avg_latency * (1.0 - reward_variability)
        max_delay = avg_latency * (1.0 + reward_variability)
        
        # Guard against negative/zero
        if min_delay < 1.0: min_delay = 1.0
        if max_delay < min_delay: max_delay = min_delay + 1.0

        final_delay = random.uniform(min_delay, max_delay)
        
        # Hard Cap to prevent stall
        MAX_DELAY_SECONDS = 900 # 15 minutes
        if final_delay > MAX_DELAY_SECONDS:
            logger.info(f"Capping delay at {MAX_DELAY_SECONDS}s (calculated {final_delay:.1f}s)")
            final_delay = MAX_DELAY_SECONDS
            
        logger.info(f"Pacing Engine: Target Avg {avg_latency}s | Var {reward_variability} | Final {final_delay:.1f}s")
        return final_delay

    def _looks_like_system_prompt_leak(self, text: str) -> bool:
        import re

        t = str(text or "").strip()
        if not t:
            return False

        # Check raw lowercased text for structural headers
        tl = t.lower()
        
        # Indicators of prompt leakage (headers, XML tags, role markers)
        # Note: We check for newline predecessors to distinguish "Strategy:" header from "my strategy is..."
        leak_patterns = [
            r'^thinking', r'\nthinking', 
            r'^reasoning', r'\nreasoning',
            r'^strategy:', r'\nstrategy:',
            r'^analysis:', r'\nanalysis:',
            r'<thinking>', r'<reasoning>',
            r'^system:', r'\nsystem:',
            r'^chat:', r'\nchat:',
            r'\[system injection\]'
        ]
        
        for pattern in leak_patterns:
             if re.search(pattern, tl, re.MULTILINE):
                 return True

        # Fallback to the normalized check for other keywords that are strictly forbidden anywhere
        tn = re.sub(r"\s+", " ", t).strip().lower()
        
        forbidden_phrases = [
            "contact context:",
            "internal thought:",
            "stop generation before creating a new chat",
            "verify it's you",
            "unusual traffic",
            "captcha",
            "sign in",
        ]
        
        if any(phrase in tn for phrase in forbidden_phrases):
            return True

        # Text message drafts should be short; long blocks are often prompt dumps.
        if len(t) > 1200:
            return True

        return False

    def _contains_analyst_leak(self, text: str) -> bool:
        """
        CRITICAL: Detect if analyst output has leaked into the response.
        
        Analyst output is STRICTLY for delegate consumption only.
        It must NEVER reach the contact as a message.
        """
        import re
        
        t = str(text or "").strip()
        if not t:
            return False
        
        # Analyst markers that should NEVER appear in a message to contact
        analyst_markers = [
            'SYSTEM:', 
            '## ANALYSIS REQUEST',
            '## YOUR TASK',
            '## TACTICAL CONTEXT',  # Legacy
            '## INTELLIGENCE DOSSIER',  # Current
            '⏰ TIME CHECK',
            '📊 DYNAMICS',
            '🎯 TACTICS',
            '⚠️ WATCH',
            '📋 TIER 1 ANALYST',
            'TACTICAL CONTEXT',
            'TACTICAL BRIEF',
            'INTELLIGENCE DOSSIER',
            'INTELLIGENCE BRIEF',
            'INTELLIGENCE REPORT',
            'TIME VERIFICATION',
            'CONVERSATION DYNAMICS',
            'Their avg length:',
            'Match their length',
            'engagement level:',
            'Momentum:',
            'DIRECTIVE:',
            '=' * 20,  # Header bars
            # UI Artifacts (AI Studio / Web scraping errors)
            'Send prompt', 
            '⌘ + Enter',
            'Run',
            'model thoughts',
        ]
        
        for marker in analyst_markers:
            if marker in t:
                logger.warning(f"[LEAK DETECTED] Analyst marker found in reply: {marker}")
                return True
        
        # Check for emoji markers that are analyst-specific
        analyst_emojis = ['⏰', '📊', '🎯', '⚠️', '📋', '💕']
        emoji_count = sum(1 for e in analyst_emojis if e in t)
        if emoji_count >= 2:  # Multiple analyst emojis = likely leak
            logger.warning(f"[LEAK DETECTED] Multiple analyst emojis found: {emoji_count}")
            return True
        
        return False

    def _require_safe_reply(self, handle: str, reply_text: str, *, _regen_attempt: int = 0) -> str:
        """
        Final safety gate before sending any message to a contact.
        
        CRITICAL: Ensures analyst output NEVER leaks to contact.
        Only delegate output should reach contacts as messages.

        On leak detection:
        1. Alert operator.
        2. Attempt ONE regeneration with a minimal prompt (no analyst report).
        3. If regen also leaks, raise ValueError to abort.
        """
        cleaned = str(reply_text or "").strip()
        if not cleaned:
            raise ValueError("Empty reply")

        is_leak = self._contains_analyst_leak(cleaned) or self._looks_like_system_prompt_leak(cleaned)

        if not is_leak:
            return cleaned

        leak_kind = "analyst" if self._contains_analyst_leak(cleaned) else "system_prompt"
        logger.error(f"[BLOCKED] {leak_kind} leak detected in reply for {handle} (len={len(cleaned)})")

        # Audit log: record every blocked message for post-mortem analysis
        audit = logging.getLogger("orchestrator.audit")
        audit.warning(
            "BLOCKED handle=%s leak_kind=%s regen_attempt=%d len=%d text=%s",
            handle, leak_kind, _regen_attempt, len(cleaned), cleaned[:500],
        )

        # Alert operator
        if settings.OPERATOR_HANDLE:
            try:
                self.bridge.send_message(
                    settings.OPERATOR_HANDLE,
                    f"⚠️ LEAK BLOCKED [{handle}]: {leak_kind} leak detected in generated reply. "
                    f"{'Attempting regen...' if _regen_attempt == 0 else 'Regen also leaked — aborting.'}",
                )
            except Exception:
                pass

        # One regen attempt with stripped-down prompt (no analyst report)
        if _regen_attempt == 0:
            logger.warning(f"[LEAK REGEN] Attempting regeneration without analyst report for {handle}")
            try:
                profile = self.archivist.load_profile(handle)
                recent_msgs = self._fetch_recent_messages(handle, limit=5)
                # Build payload WITHOUT analyst_report to minimize leak surface
                payload = self.archivist.build_context_payload(
                    profile,
                    recent_messages=recent_msgs,
                    analyst_report=None,
                )
                regen_text = self._synchronized_llm_call(payload, context=f"leak_regen:{handle}")
                return self._require_safe_reply(handle, regen_text, _regen_attempt=1)
            except Exception as e:
                logger.error(f"[LEAK REGEN] Regeneration failed for {handle}: {e}")

        raise ValueError(f"{leak_kind} leakage detected in output for {handle} ({len(cleaned)} chars)")

    def _check_proactive_initiation(self):
        """
        Polls all contacts to see if the Pacing Engine dictates an initiation.
        """
        try:
            handles = self.archivist.get_all_handles()
            if not self._logged_proactive_handles_once:
                logger.info(
                    f"[PROACTIVE] Scanning {len(handles)} handles from {getattr(self.archivist, 'base_path', 'unknown')}: {handles}"
                )
                self._logged_proactive_handles_once = True
            logger.debug(f"Checking handles: {handles}")  # Reduced from info to debug
            for handle in handles:
                # GUARD: Do not analyze the Operator/Admin
                if settings.OPERATOR_HANDLE and handle == settings.OPERATOR_HANDLE:
                    continue

                self._process_single_handle_initiation(handle)
        except Exception as e:
            import traceback
            logger.error(f"Proactive check failed: {e}\n{traceback.format_exc()}")

    def _process_single_handle_initiation(self, handle: str):
        """
        STATE MACHINE GUARD: Only process if handle is in IDLE state.
        If AWAITING_APPROVAL, do nothing - wait for operator.
        """
        profile = self.archivist.load_profile(handle)
        pacing = profile.get("pacing_engine", {})
        identity = profile.get("identity_matrix", {})

        # If we're awaiting approval, we normally block.
        # However, if the operator didn't see the request, force_trigger should re-send it.
        if handle in self.pending_approvals:
            force_trigger = bool(pacing.get("force_trigger", False))
            if force_trigger:
                logger.warning(f"FORCE TRIGGER while AWAITING_APPROVAL for {handle}. Re-sending approval request.")
                self._resend_approval(handle)
                profile["pacing_engine"]["force_trigger"] = False
                self.archivist.update_profile(handle, profile)
            else:
                logger.debug(f"[STATE] {handle} is AWAITING_APPROVAL. Skipping initiation.")
            return
        
        # 0. Force Trigger (Manual Override)
        force_trigger = bool(pacing.get("force_trigger", False))
        if handle == "+15082615479":
            logger.info(f"[DEBUG] {handle} Pacing: {pacing}")

        # One-shot semantics: if force_trigger is set, consume it immediately.
        # This prevents repeated Tier-1 analyst calls every poll tick if downstream
        # generation/sending fails (rate limit, UI error, etc.).
        if force_trigger:
            try:
                if "pacing_engine" not in profile:
                    profile["pacing_engine"] = {}
                profile["pacing_engine"]["force_trigger"] = False
                self.archivist.update_profile(handle, profile)
                logger.info(f"[FORCE TRIGGER] Consumed one-shot force_trigger for {handle}")
            except Exception as e:
                logger.warning(f"[FORCE TRIGGER] Failed to persist force_trigger reset for {handle}: {e}")

        if not force_trigger:
            # 1. Global Switch
            if not pacing.get("initiation_enabled", False):
                return

            # 2. Daily Frequency Check
            last_auto = pacing.get("last_auto_initiation_timestamp", "")
            if last_auto:
                try:
                    last_dt = datetime.datetime.fromisoformat(last_auto)
                    if last_dt.date() == datetime.datetime.now().date():
                        return 
                except ValueError:
                    pass 

            # 3. Timezone Logic
            tz_name = identity.get("timezone", "America/New_York")
            try:
                tz = ZoneInfo(tz_name)
                now_local = datetime.datetime.now(tz)
            except Exception:
                now_local = datetime.datetime.now(ZoneInfo("UTC"))
                
            current_hour = now_local.hour
            
            # 4. Active Hours (Pacing Engine)
            start = pacing.get("active_hours_start", 9)
            end = pacing.get("active_hours_end", 22)
            
            if not (start <= current_hour <= end):
                return

            # 5. Trigger Windows (Morning/Eve) 
            is_trigger_time = False
            context_hint = ""
            
            if 8 <= current_hour <= 10:
                is_trigger_time = True
                context_hint = "It is morning. Initiate casually."
            elif 18 <= current_hour <= 20:
                is_trigger_time = True
                context_hint = "It is evening. Check in."

            if not is_trigger_time:
                return
        else:
            logger.warning(f"FORCE TRIGGER DETECTED for {handle}. Bypassing Pacing Engine.")
            context_hint = "COMMAND OVERRIDE: Immediate engagement requested."

        logger.info(f"Triggering Proactive Message for {handle} ({context_hint})")
        
        try:
            # --- Run Tier 1 Context Analyst for Proactive Messages ---
            logger.info(f"[PROACTIVE] Running context analysis for {handle}...")

            # Reset Trigger & Tick Timestamp
            if "pacing_engine" not in profile: profile["pacing_engine"] = {}
            profile["pacing_engine"]["last_auto_initiation_timestamp"] = datetime.datetime.now().isoformat()
            self.archivist.update_profile(handle, profile)
            
            # Fetch EXTENDED history (20 msgs) for analyst, 5 for display
            extended_msgs = self._fetch_recent_messages(handle, limit=20)
            recent_msgs = extended_msgs[-5:] if len(extended_msgs) > 5 else extended_msgs
            
            analyst_report = None
            try:
                analyst_report = self._with_llm_lock(
                    context=f"analyst_pre:proactive:{handle}",
                    fn=lambda: self.analyst.analyze_pre_response(
                        profile=profile,
                        messages=extended_msgs,
                        operator_location=settings.OPERATOR_LOCATION,
                    ),
                )
                logger.info(
                    f"[PROACTIVE ANALYST] Context report generated ({len(str(analyst_report))} chars)"
                )
            except Exception as e:
                logger.warning(f"[PROACTIVE] Analyst failed: {e}")
            
            payload = self.archivist.build_context_payload(
                profile,
                recent_messages=recent_msgs,
                analyst_report=analyst_report,
                is_proactive=True  # Inject initiation mode instructions
            )
            
            reply_text = self._synchronized_llm_call(payload, context=f"proactive:{handle}")
            reply_text = self._require_safe_reply(handle, reply_text)
            if reply_text:
                # --- SHADOW MODE CHECK FOR PROACTIVE PATH ---
                requires_approval = profile.get("requires_approval", False)
                if requires_approval and settings.OPERATOR_HANDLE:
                    logger.info(f"[PROACTIVE] Target {handle} requires approval. Asking Operator.")
                    
                    # Prevent infinite proactive loop: clear trigger BEFORE requesting approval
                    if force_trigger:
                        profile = self.archivist.load_profile(handle)
                        if "pacing_engine" not in profile:
                            profile["pacing_engine"] = {}
                        profile["pacing_engine"]["force_trigger"] = False
                        self.archivist.update_profile(handle, profile)

                    self._request_approval(handle, f"(Proactive: {context_hint})", reply_text)
                        
                    return  # Don't send yet, wait for approval
                
                logger.info(f">> PROACTIVE SEND to {handle}: {reply_text}")
                # Use profile service preference or default to iMessage
                svc = profile.get("identity_matrix", {}).get("service", "iMessage")
                success = self._send_message(handle, reply_text, service=svc)
                if success:
                    # NOW we clear the trigger (if we didn't already clear it in approval block)
                    if not requires_approval: # Only clear if we didn't just clear it above
                        profile = self.archivist.load_profile(handle) # Reload to be safe
                        if "pacing_engine" not in profile: profile["pacing_engine"] = {}
                        profile["pacing_engine"]["force_trigger"] = False
                        self.archivist.update_profile(handle, profile)

            else:
                # If generation produced no reply, ensure force_trigger is not left armed.
                try:
                    profile = self.archivist.load_profile(handle)
                    if "pacing_engine" not in profile:
                        profile["pacing_engine"] = {}
                    profile["pacing_engine"]["force_trigger"] = False
                    self.archivist.update_profile(handle, profile)
                except Exception:
                    pass

        except Exception as e:
            logger.error(f"Failed to auto-initiate for {handle}: {e}")
            # Defensive: never leave force_trigger armed after an exception.
            try:
                profile = self.archivist.load_profile(handle)
                if "pacing_engine" not in profile:
                    profile["pacing_engine"] = {}
                profile["pacing_engine"]["force_trigger"] = False
                self.archivist.update_profile(handle, profile)
            except Exception:
                pass

    def _request_approval(self, target_handle: str, trigger_msg: str, draft: str):
        """
        STATE TRANSITION: IDLE -> AWAITING_APPROVAL
        Stores draft and notifies Operator. Persists to disk.
        
        DETERMINISTIC: Only sends ONE approval request per handle.
        Agent BLOCKS until Operator responds with Y/N/advice.
        """
        # GUARD: Already waiting for approval on this handle - DO NOT RESEND
        if target_handle in self.pending_approvals:
            logger.debug(f"[STATE] {target_handle} already AWAITING_APPROVAL. Not re-requesting.")
            return

        # Safety guard: never allow system/prompt leakage to be stored as a draft.
        if self._looks_like_system_prompt_leak(draft):
            logger.error(f"[SAFETY] Refusing to store unsafe draft for {target_handle}.")
            if settings.OPERATOR_HANDLE:
                self.bridge.send_message(
                    settings.OPERATOR_HANDLE,
                    f"❌ Draft blocked for {target_handle}: looked like SYSTEM/prompt leakage. Regenerate after fixing provider/tab state."
                )
            return
        
        self.pending_approvals[target_handle] = {
            "draft": draft, 
            "trigger": trigger_msg,
            # Store ISO timestamp for readability and cleanup logic; keep backward compatibility in loaders.
            "timestamp": datetime.datetime.now().isoformat(),
            "approval_sent": True  # Mark that we've sent the approval request
        }
        self._save_approvals()  # PERSIST TO DISK
        
        preview = draft[:200] + "..." if len(draft) > 200 else draft
            
        # Clear, deterministic prompt - agent will NOT act until response
        prompt = (
            f"⚠️ APPROVAL [{target_handle}]\n"
            f"CONTEXT: {trigger_msg}\n"
            f"DRAFT: \"{preview}\"\n\n"
            f"Reply: Y / N / <your guidance>\n"
            f"(Agent is BLOCKED until you respond)"
        )
        if settings.OPERATOR_HANDLE:
            self.bridge.send_message(settings.OPERATOR_HANDLE, prompt)
            logger.info(f"[STATE] {target_handle}: IDLE -> AWAITING_APPROVAL (approval msg sent, agent blocked)")

    def _resend_approval(self, target_handle: str) -> None:
        """Re-send the approval prompt for an existing pending approval."""
        if not settings.OPERATOR_HANDLE:
            return

        data = self.pending_approvals.get(target_handle)
        if not data:
            return

        draft = str(data.get("draft", "")).strip()
        trigger_msg = str(data.get("trigger", "(Pending approval)"))
        if not draft:
            return

        preview = draft[:200] + "..." if len(draft) > 200 else draft
        prompt = (
            f"⚠️ APPROVAL [{target_handle}] (RESEND)\n"
            f"CONTEXT: {trigger_msg}\n"
            f"DRAFT: \"{preview}\"\n\n"
            f"Reply: Y / N / <your guidance>\n"
            f"(Agent is BLOCKED until you respond)"
        )

        self.bridge.send_message(settings.OPERATOR_HANDLE, prompt)
        data["timestamp"] = datetime.datetime.now().isoformat()
        data["approval_sent"] = True
        self.pending_approvals[target_handle] = data
        self._save_approvals()
        logger.info(f"[STATE] Re-sent approval request for {target_handle}")

    def _handle_operator_response(self, text: str):
        """
        STATE TRANSITION: AWAITING_APPROVAL -> IDLE (on Y/N) or AWAITING_APPROVAL (on advice)
        """
        text_stripped = text.strip()
        text_clean = text_stripped.lower()

        # Accept multi-word operator commands by parsing the first token.
        # Examples: "Y.", "ok send", "yes please", "nope", "cancel".
        import re
        normalized = re.sub(r"[^a-z0-9]+", " ", text_clean).strip()
        first_token = normalized.split(" ", 1)[0] if normalized else ""
        
        # --- GUARD: No pending approvals ---
        if not self.pending_approvals:
            # Don't spam operator if they're just chatting
            logger.info(f"[OPERATOR] No pending approvals. Ignoring: {text_stripped[:30]}...")
            return

        def _ts_to_epoch(ts: object) -> float:
            if ts is None:
                return 0.0
            if isinstance(ts, (int, float)):
                return float(ts)
            if isinstance(ts, str):
                try:
                    return datetime.datetime.fromisoformat(ts).timestamp()
                except Exception:
                    try:
                        return float(ts)
                    except Exception:
                        return 0.0
            return 0.0

        # Get most recent pending approval
        target_handle = max(
            self.pending_approvals.keys(),
            key=lambda k: _ts_to_epoch(self.pending_approvals[k].get("timestamp")),
        )
        data = self.pending_approvals[target_handle]
        draft = data["draft"]
        
        # --- DECISION: YES ---
        if first_token in {"y", "yes", "send", "ok", "okay", "go", "approve", "approved", "yea", "yeah", "yep"}:
            logger.info(f"[DECISION] Operator APPROVED for {target_handle}")
            
            # --- PROMOTION LOGIC (Migration from Unverified to Main) ---
            # If this was an unknown contact, their profile is in _unverified. Move it to main contacts.
            try:
                if not self.archivist.has_profile(target_handle):
                    unverified_archivist = Archivist(contacts_dir=settings.UNVERIFIED_CONTACTS_DIR)
                    if unverified_archivist.has_profile(target_handle):
                        logger.info(f"[PROMOTION] Migrating {target_handle} from Unverified -> Main.")
                        probation_profile = unverified_archivist.load_profile(target_handle)
                        
                        # Add metadata for Analyst to see they are verified-by-operator
                        probation_profile.setdefault("unverified", {})
                        probation_profile["unverified"]["promoted_at"] = datetime.datetime.now().isoformat()
                        probation_profile["requires_approval"] = True # Start with shadow mode for safety
                        probation_profile["shadow_mode_approvals"] = 0
                        
                        # Save to Main Archivist
                        self.archivist.update_profile(target_handle, probation_profile)
                        
                        # Clean up old file
                        unverified_archivist.delete_profile(target_handle)
            except Exception as e:
                logger.error(f"[PROMOTION] Failed to migrate profile for {target_handle}: {e}")

            # CRITICAL: Remove from pending BEFORE attempting send.
            # If the process crashes after send but before delete, a restart
            # would re-send (idempotent).  But crashing after delete but
            # before send means the draft is lost — acceptable because the
            # operator can re-trigger.  The reverse (message sent + still
            # pending) is worse because it causes double-send on restart.
            del self.pending_approvals[target_handle]
            self._save_approvals()

            success = self.bridge.send_message(target_handle, draft)
            
            if success:
                self.archivist.store_interaction(target_handle, "(Approved)", draft)
                
                # Trust calibration
                profile = self.archivist.load_profile(target_handle)
                clean_count = profile.get("shadow_mode_approvals", 0) + 1
                profile["shadow_mode_approvals"] = clean_count
                
                if clean_count >= self.SHADOW_MODE_TRUST_THRESHOLD:
                    profile["requires_approval"] = False
                    profile["shadow_mode_approvals"] = 0
                    self.archivist.update_profile(target_handle, profile)
                    if settings.OPERATOR_HANDLE:
                        self.bridge.send_message(settings.OPERATOR_HANDLE, 
                            f"✅ Sent. Trust earned ({clean_count}/{self.SHADOW_MODE_TRUST_THRESHOLD}). Shadow mode OFF.")
                else:
                    self.archivist.update_profile(target_handle, profile)
                    if settings.OPERATOR_HANDLE:
                        self.bridge.send_message(settings.OPERATOR_HANDLE, 
                            f"✅ Sent to {target_handle}. ({clean_count}/{self.SHADOW_MODE_TRUST_THRESHOLD})")
                
                logger.info(f"[STATE] {target_handle}: AWAITING_APPROVAL -> IDLE (Sent)")
            else:
                if settings.OPERATOR_HANDLE:
                    self.bridge.send_message(settings.OPERATOR_HANDLE, f"❌ Send failed for {target_handle}.")
            return

        # --- DECISION: NO ---
        if first_token in {"n", "no", "stop", "cancel", "deny", "denied", "nope", "nah", "skip"}:
            logger.info(f"[DECISION] Operator DENIED for {target_handle}")
            del self.pending_approvals[target_handle]
            self._save_approvals()
            if settings.OPERATOR_HANDLE:
                self.bridge.send_message(settings.OPERATOR_HANDLE, f"🚫 Cancelled for {target_handle}.")
            logger.info(f"[STATE] {target_handle}: AWAITING_APPROVAL -> IDLE (Cancelled)")
            return
        
        # --- DECISION: ADVICE (anything else) ---
        logger.info(f"[DECISION] Operator GUIDANCE for {target_handle}: {text_stripped}")
        
        # Extract advice (remove "advice:" prefix if present)
        advice = text_stripped
        if text_clean.startswith("advice:"):
            advice = text_stripped[7:].strip()
        elif text_clean.startswith("advice "):
            advice = text_stripped[7:].strip()
        
        # Store guidance in profile (persistent)
        profile = self.archivist.load_profile(target_handle)
        timestamp = datetime.datetime.now().strftime("%H:%M")
        
        # REPLACE not APPEND to avoid pollution
        profile["operator_context"] = f"[{timestamp}] OPERATOR GUIDANCE: {advice}"
        profile["shadow_mode_approvals"] = 0  # Reset trust
        self.archivist.update_profile(target_handle, profile)
        
        # Regenerate with new guidance (run analyst on extended history)
        extended_msgs = self._fetch_recent_messages(target_handle, limit=20)
        recent_msgs = extended_msgs[-5:] if len(extended_msgs) > 5 else extended_msgs
        
        analyst_report = None
        try:
            analyst_report = self._with_llm_lock(
                context=f"analyst_pre:advice:{target_handle}",
                fn=lambda: self.analyst.analyze_pre_response(
                    profile=profile,
                    messages=extended_msgs,
                    operator_location=settings.OPERATOR_LOCATION,
                ),
            )
        except Exception:
            pass
        
        payload = self.archivist.build_context_payload(
            profile,
            recent_messages=recent_msgs,
            analyst_report=analyst_report
        )
        
        new_draft = self._synchronized_llm_call(payload, context=f"advice:{target_handle}")
        
        # Update pending approval with new draft (stays in AWAITING_APPROVAL)
        self.pending_approvals[target_handle] = {
            "draft": new_draft,
            "trigger": "(Refined)",
            "timestamp": datetime.datetime.now().isoformat(),
            "approval_sent": True,
        }
        self._save_approvals()
        
        # Send new draft for approval
        preview = new_draft[:200] + "..." if len(new_draft) > 200 else new_draft
        if settings.OPERATOR_HANDLE:
            self.bridge.send_message(settings.OPERATOR_HANDLE, 
                f"🔄 REVISED [{target_handle}]\n"
                f"NEW DRAFT: \"{preview}\"\n\n"
                f"Reply: Y / N / <more guidance>")
        
        logger.info(f"[STATE] {target_handle}: AWAITING_APPROVAL (draft updated)")

    def _send_message(self, handle: str, text: str, service: str = "iMessage") -> bool:
        """Route message to the correct bridge and persist outbound for history."""
        # Normalize service name
        svc_key = "WhatsApp" if "WhatsApp" in service else "iMessage"
        if svc_key == "iMessage" and "SMS" in service:
            svc_key = "SMS"
        
        # Select bridge
        if "WhatsApp" in service:
            bridge = self.bridges.get("WhatsApp")
        else:
            bridge = self.bridges.get("iMessage")
            
        if not bridge:
            logger.error(f"No bridge found for service {service}")
            return False

        success = bridge.send_message(handle, text, service=service)

        # Persist outbound WhatsApp messages in the local store for history retrieval
        if success and "WhatsApp" in service and self.watcher:
            try:
                for w in getattr(self.watcher, "watchers", []):
                    if hasattr(w, "store_outbound"):
                        w.store_outbound(handle, text)
                        break
            except Exception:
                pass

        return success

    def _handle_unknown_contact(self, *, handle: str, inbound_text: str, service: str) -> None:
        """Safety-first flow for unknown senders.

        Default behavior:
        - Do not message unknown contacts automatically.
        - Create/update an unverified profile under data/contacts/_unverified.
        - Produce a triage report + suggested reply and queue it for operator approval.
        """
        canonical = str(handle or "").strip()
        if canonical.isdigit():
            canonical = "+" + canonical

        inbound_text = str(inbound_text or "").strip()

        # If we already have a pending approval for this handle, don't spam new requests.
        if canonical in self.pending_approvals:
            logger.info(f"[UNKNOWN] {canonical} already pending approval; not re-triaging")
            return

        # Run triage first.
        triage: dict = {}
        try:
            triage = self._with_llm_lock(
                context=f"analyst_unknown:{canonical}",
                fn=lambda: self.analyst.analyze_unknown_contact(handle=canonical, inbound_text=inbound_text),
            )
        except Exception as e:
            logger.warning(f"[UNKNOWN] Analyst triage failed for {canonical}: {e}")

        classification = str(triage.get("classification", "unknown"))
        confidence = triage.get("confidence", 0.0)
        risk_flags = triage.get("risk_flags", [])
        
        belief_state = triage.get("belief_state", {})
        p_male = belief_state.get("p_male", 0.0)
        p_business = belief_state.get("p_business", 0.0)

        suggested_reply = str(triage.get("suggested_reply") or "").strip()

        # Persist a minimal unverified profile snapshot (never allowlists the handle).
        try:
            settings.UNVERIFIED_CONTACTS_DIR.mkdir(parents=True, exist_ok=True)
            unverified_archivist = Archivist(contacts_dir=settings.UNVERIFIED_CONTACTS_DIR)
            profile = unverified_archivist.load_profile(canonical)
            profile.setdefault("unverified", {})
            profile["unverified"].update(
                {
                    "first_seen_epoch": profile.get("unverified", {}).get("first_seen_epoch")
                    or datetime.datetime.now().timestamp(),
                    "last_inbound_text": inbound_text[:2000],
                    "last_seen_epoch": datetime.datetime.now().timestamp(),
                    "service": service,
                    "belief_state": belief_state,
                    "triage_classification": classification
                }
            )
            unverified_archivist.update_profile(canonical, profile)
        except Exception as e:
            logger.warning(f"[UNKNOWN] Failed to persist unverified profile for {canonical}: {e}")

        # --- GENDER/INTENT FILTERING (Probabilistic) ---
        # Logic: Auto-reject recognized males unless it's explicitly business.
        # Strict gate: High confidence male AND Low confidence business.
        if p_male > 0.9 and p_business < 0.2:
            logger.info(f"[FILTER] Auto-rejecting contact {canonical} (p_male={p_male:.2f}, p_business={p_business:.2f})")
            # We do NOT queue for approval. We do NOT reply.
            return

        trigger_msg = (
            f"UNKNOWN CONTACT ({service})\n"
            f"THEY SAID: \"{inbound_text[:300]}\"\n"
            f"TRIAGE: {classification} (conf={confidence})\n"
            f"BELIEF: male={p_male:.2f}, biz={p_business:.2f}\n"
            f"FLAGS: {risk_flags}"
        )

        # Auto-reply only if explicitly enabled and triage isn't suspicious.
        if settings.AUTO_REPLY_UNKNOWN_CONTACTS and classification not in {"spam", "scam"}:
            if suggested_reply:
                logger.info(f"[UNKNOWN] Auto-reply enabled; sending to {canonical}")
                self._send_message(canonical, suggested_reply, service=service)
            return

        if not suggested_reply:
            suggested_reply = "Hey — good to hear from you. Hope your day’s going well."

        logger.info(f"[UNKNOWN] Queuing approval for {canonical} (no auto-reply)")
        self._request_approval(canonical, trigger_msg, suggested_reply)

    def handle_incoming(self, msg: IncomingMessage, chat_history: list):
        handle = msg.handle
        message_text = msg.text

        # --- A. OPERATOR INTERVENTION (COMPLETELY SEPARATE FLOW) ---
        # Operator messages NEVER go to contact context, and contacts NEVER see operator chat
        if settings.OPERATOR_HANDLE and policy.canonicalize_handle(handle) == policy.canonicalize_handle(settings.OPERATOR_HANDLE):
            self._handle_operator_response(message_text)
            return  # STOP HERE - No further processing for operator messages

        # 1. Load Data
        profile = self.archivist.load_profile(handle)

        # Canonicalize handle for consistent allowlist + approvals.
        canonical_handle = str(profile.get("identity_matrix", {}).get("handle") or handle).strip()
        if canonical_handle:
            handle = canonical_handle

        # --- B.1 ROUTING DECISION (Single Source of Truth) ---
        decision = policy.decide_inbound(handle)
        handle = decision.handle

        # --- TIER 1 ANALYSIS: Now handled by ContextAnalyst (local Python, no LLM) ---
        # The old LLM-based analyst.analyze_pre_response() was causing echo loops.
        # ContextAnalyst does local analysis of time, patterns, etc.
        # Risk detection can be done post-delegate if needed.

        # --- B. MUTE PROTOCOL (SHUT UP BUTTON) ---
        if profile.get("mute_agent", False):
            logger.info(f"[MUTED] Ignoring message from {handle} for silence protocol.")
            return

        # --- B.1 UNKNOWN CONTACT GATE (SAFETY-FIRST) ---
        # If strict policy says "unknown", we normally divert to triage.
        # EXCEPTION: If the profile exists in our MAIN contacts directory (self.archivist),
        # it means they were previously approved/promoted, so we treat them as "Probationary" 
        # (known but not in env allowlist).
        if decision.kind == "unknown":
            if self.archivist.has_profile(handle):
                 # They are PROBATIONARY (Approved but not in static config)
                 # Allow them to proceed to main Delegate flow.
                 logger.info(f"[GATE] {handle} is Unknown in Policy but has Profile. Treating as PROBATIONARY.")
            else:
                 # True Unknown - route to Triage
                 self._handle_unknown_contact(handle=handle, inbound_text=message_text, service=msg.service)
                 return

        # --- C. STATE GUARD: AWAITING_APPROVAL ---
        # If we're waiting for operator approval on a previous draft for this handle,
        # the contact has sent a new message - we need to regenerate with fresh context
        if handle in self.pending_approvals:
            logger.info(f"[STATE] {handle} sent new message while AWAITING_APPROVAL. Regenerating draft with new context.")
            # Mark that we're updating (not sending new approval request)
            old_pending = self.pending_approvals[handle]
            del self.pending_approvals[handle]
            self._save_approvals()
            # NOTE: The new draft will go through approval again via _request_approval below
            # which has a guard against duplicate requests

        # 1.5 Quiet Hours (Option B): persist inbound + auto-respond after quiet hours ends.
        in_quiet, now_hour = self._is_in_quiet_hours(profile)
        if in_quiet:
            quiet = profile.get("pacing_engine", {}).get("quiet_hours", {})
            if quiet.get("ignore_user_messages", True):
                logger.info(
                    f"[QUIET HOURS] Deferring inbound from {handle} (Hour: {now_hour}). Will respond after quiet hours ends."
                )
                self._defer_inbound_quiet_hours(msg=msg, profile=profile, now_hour=now_hour)
                return

        # 2. Run Tier 1 Context Analyst (pre-response intelligence)
        # Fetch EXTENDED history (20 messages) for analyst, but only 5 for delegate display
        extended_msgs = self._fetch_recent_messages(handle, limit=20)
        recent_msgs = extended_msgs[-5:] if len(extended_msgs) > 5 else extended_msgs
        
        analyst_report = None
        try:
            logger.info(f"[ANALYST] Running context analysis for {handle}...")
            analyst_report = self._with_llm_lock(
                context=f"analyst_pre:reactive:{handle}",
                fn=lambda: self.analyst.analyze_pre_response(
                    profile=profile,
                    messages=extended_msgs,
                    operator_location=settings.OPERATOR_LOCATION,
                ),
            )
            logger.info(f"[ANALYST] Context report generated ({len(str(analyst_report))} chars)")
        except Exception as e:
            logger.warning(f"[ANALYST] Analysis failed, proceeding without: {e}")
        
        # 3. Build Context with Analyst Injection
        payload = self.archivist.build_context_payload(
            profile,
            recent_messages=recent_msgs,
            analyst_report=analyst_report
        )
        
        # 4. Generate Reply (SYNCHRONIZED to prevent interruptions)
        try:
            logger.info(f"Generating reply for {handle}...")
            reply_text = self._synchronized_llm_call(payload, context=f"reactive:{handle}")
            logger.info(f"[DELEGATE] Generated raw reply (len={len(reply_text)}): {reply_text[:100]}...")
        except RateLimitError as e:
            raise e 
        except Exception as e:
            logger.error(f"Generation failed: {e}")
            return

        try:
            reply_text = self._require_safe_reply(handle, reply_text)
        except ValueError as e:
            logger.error(f"[SAFETY BLOCK] {str(e)}")
            return
        if not reply_text:
            logger.warning("[ABORT] Reply text was empty after safety check.")
            return

        # --- SHADOW MODE CHECK (DETERMINISTIC: ONE REQUEST, THEN BLOCK) ---
        requires_approval = profile.get("requires_approval", False)
        if requires_approval and settings.OPERATOR_HANDLE:
            logger.info(f"[SHADOW MODE] Target {handle} requires approval. Requesting (agent will block).")
            # Context for operator: show what the contact said
            trigger_context = f"THEY SAID: \"{message_text[:100]}..\"" if len(message_text) > 100 else f"THEY SAID: \"{message_text}\""
            self._request_approval(handle, trigger_context, reply_text)
            return  # BLOCK - Do not send, do not process further

        # 4. Temporal Dynamics (Pacing) — NON-BLOCKING
        # Instead of blocking the entire poll loop with time.sleep(), enqueue
        # the message for future delivery.  The main loop drains the queue
        # every tick, sending messages whose delay has elapsed.
        delay = self._calculate_response_delay(msg, chat_history)
        logger.info(f"Scheduling send to {handle} in {delay:.1f}s (non-blocking)")
        self.send_queue.enqueue(
            handle=handle,
            text=reply_text,
            service=msg.service,
            delay_seconds=delay,
            context=f"reactive:{handle}",
        )
        # Pre-record interaction so profile timestamps update immediately
        try:
            self.archivist.store_interaction(handle, message_text, reply_text)
        except Exception as e:
            logger.error(f"Failed to store interaction for {handle}: {e}")

    def _run_post_interaction_analysis(self, handle: str, user_msg: str, agent_msg: str):
        """
        Executes the Tier 2-5 intelligence pipeline via unified AnalystService.
        In a production system, this should be offloaded to a task queue.
        """
        profile = self.archivist.load_profile(handle)
        
        # Get conversation history
        recent_history = self._fetch_recent_messages(handle, limit=10)
        
        # Run the full pipeline through unified service
        try:
            self._with_llm_lock(
                context=f"analyst_post:{handle}",
                fn=lambda: self.analyst.run_post_interaction_pipeline(
                    handle=handle,
                    profile=profile,
                    history=recent_history,
                    archivist=self.archivist,
                ),
            )
        except Exception as e:
            logger.error(f"[ANALYST] Post-interaction pipeline failed for {handle}: {e}")

def run() -> None:
    _configure_logging()
    logger = logging.getLogger("orchestrator")

    # ---- PLATFORM GUARD ----
    # iMessage (chat.db + AppleScript) is macOS-only.  Fail fast if enabled on wrong OS.
    if settings.ENABLE_IMESSAGE and sys.platform != "darwin":
        logger.error(
            "ENABLE_IMESSAGE=true but platform is %s (macOS required). "
            "Set ENABLE_IMESSAGE=false and ENABLE_WHATSAPP=true for cross-platform.",
            sys.platform,
        )
        return

    # Provider-specific key requirements.
    api_key: str | None
    if settings.LLM_PROVIDER == "gemini":
        api_key = settings.GEMINI_API_KEY
        if not api_key:
            logger.error("GEMINI_API_KEY is not set.")
            return
    elif settings.LLM_PROVIDER == "openai":
        api_key = settings.OPENAI_API_KEY
        if not api_key:
            logger.error("OPENAI_API_KEY is not set.")
            return
    elif settings.LLM_PROVIDER == "anthropic":
        api_key = settings.ANTHROPIC_API_KEY
        if not api_key:
            logger.error("ANTHROPIC_API_KEY is not set.")
            return
    elif settings.LLM_PROVIDER == "lotl":
        # LotL routes through the local controller; no API key required.
        api_key = ""
    elif settings.LLM_PROVIDER == "copilot":
        # Copilot via LotL
        api_key = "copilot" 
    else:
        logger.error("Unsupported LLM_PROVIDER: %s", settings.LLM_PROVIDER)
        return

    # ---- INSTANCE LOCK ----
    # Prevent two orchestrator processes from running simultaneously,
    # which would corrupt shared state and interleave LotL requests.
    try:
        with acquire_instance_lock() as lock_path:
            _run_main_loop(api_key, logger)
    except InstanceAlreadyRunning as exc:
        logger.error(str(exc))
        return


def _run_main_loop(api_key: str | None, logger: logging.Logger) -> None:
    """Inner loop extracted so the instance lock context manager wraps it."""

    watchers = []
    
    # 1. iMessage Watcher (Default)
    if settings.ENABLE_IMESSAGE:
        logger.info("[INIT] Enabling iMessage Watcher")
        w_imessage = MessageWatcher(target_handles=None)
        watchers.append(w_imessage)
        
    # 2. WhatsApp Watcher
    if settings.ENABLE_WHATSAPP:
        logger.info("[INIT] Enabling WhatsApp Watcher")
        w_whatsapp = WhatsAppWatcher()
        watchers.append(w_whatsapp)
        
    if not watchers:
        logger.error("No watchers enabled! Check settings.py (ENABLE_IMESSAGE / ENABLE_WHATSAPP)")
        return
        
    watcher = CompositeWatcher(watchers)
    watcher.initialize()
    
    bot = Orchestrator(api_key, watcher=watcher)

    cooldown_until = 0.0

    logger.info("Phase 2 Orchestrator running (The Triad + Bridge). Poll interval=%ss", settings.POLL_INTERVAL_SECONDS)

    while True:
        try:
            now = time.time()
            if now < cooldown_until:
                time.sleep(min(settings.POLL_INTERVAL_SECONDS, max(0.0, cooldown_until - now)))
                continue

            # ---- DRAIN SCHEDULED SENDS (non-blocking pacing) ----
            # This replaces the old blocking time.sleep(delay) approach.
            # Messages enqueued by handle_incoming are delivered here once
            # their pacing delay has elapsed.
            try:
                sent = bot.send_queue.drain(
                    send_fn=bot._send_message,
                    on_failure=lambda entry: (
                        bot.bridge.send_message(
                            settings.OPERATOR_HANDLE,
                            f"\u274c SEND FAILED [{entry.handle}]: exhausted {entry.max_retries} retries",
                        )
                        if settings.OPERATOR_HANDLE
                        else None
                    ),
                )
                if sent:
                    logger.info("[SEND_Q] Delivered %d scheduled messages", sent)
            except Exception as exc:
                logger.error("[SEND_Q] Drain error: %s", exc)

            new_messages = watcher.poll_new_messages()

            # Collect messages that couldn't be processed due to rate limit
            # so they can be retried on the next tick instead of being dropped.
            rate_limited = False

            for msg in new_messages:
                if rate_limited:
                    # Don't process remaining messages in this batch;
                    # they will be re-polled because we only advance rowid
                    # for successfully processed messages.
                    break

                # --- DEFERRED READ FOR WHATSAPP (Human-like behavior) ---
                if msg.text.startswith("__UNREAD_PENDING__:"):
                    read_delay = random.uniform(2.0, 8.0)
                    logger.info(f"[WHATSAPP] Waiting {read_delay:.1f}s before reading message from {msg.handle}")
                    time.sleep(read_delay)
                    
                    actual_text = watcher.read_message(msg.handle) if hasattr(watcher, 'read_message') else None
                    if not actual_text:
                        logger.warning(f"[WHATSAPP] Could not read message from {msg.handle}")
                        continue
                    msg = IncomingMessage(
                        message_rowid=msg.message_rowid,
                        handle=msg.handle,
                        text=actual_text,
                        service=msg.service,
                        date=msg.date
                    )

                logger.info("[INFO] New Message from %s: %s", msg.handle, msg.text[:100])

                history = watcher.fetch_recent_history(handle=msg.handle)
                
                try:
                    bot.handle_incoming(msg, history)
                except RateLimitError as exc:
                    cooldown_until = time.time() + exc.retry_after_seconds
                    logger.warning("[RATE_LIMIT] Backing off for %.1fs (remaining msgs preserved)", exc.retry_after_seconds)
                    rate_limited = True
                    # Do NOT break — the for-loop guard above skips remaining
                except Exception as e:
                    logger.exception("Error handling message from %s: %s", msg.handle, e)

            # Quiet-hours Option B: attempt deferred sends once quiet hours ends.
            if time.time() >= cooldown_until:
                try:
                    drained = bot.drain_deferred_outbox(max_per_tick=5)
                    if drained:
                        logger.info("[DEFERRED] Drained %s deferred handles", drained)
                except RateLimitError as exc:
                    cooldown_until = time.time() + exc.retry_after_seconds
                    logger.warning("[RATE_LIMIT] Backing off for %.1fs", exc.retry_after_seconds)

            # Check for Proactive Initiations every cycle (before sleep,
            # so proactive checks are not gated by the poll interval).
            bot._check_proactive_initiation()

            time.sleep(settings.POLL_INTERVAL_SECONDS)

        except KeyboardInterrupt:
            logger.info("Orchestrator stopped by user.")
            break
        except Exception as e:
            logger.exception("Top-level error in loop: %s", e)
            time.sleep(5)


if __name__ == "__main__":
    run()
