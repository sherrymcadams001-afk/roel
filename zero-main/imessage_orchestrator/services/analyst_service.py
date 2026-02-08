"""
Unified Analyst Service - All intelligence tiers in one place.

This consolidates:
- services/context_analyst.py (Tier 1 pre-response analysis)
- services/analyst.py (Tier 2-5 post-interaction analysis)
- orchestrator._run_post_interaction_analysis() (sampling logic)

Architecture:
- Tier 1 (Pre-Response): Neutral context report before delegate generates
- Tier 2 (Facts): Extract concrete facts to knowledge graph
- Tier 3 (Summary): Compress conversation into narrative
- Tier 4 (Trajectory): Analyze relationship trends
- Tier 5 (Strategic): High-level planning

Single entry point for all intelligence operations with consistent provider handling.
"""

from __future__ import annotations
import json
import logging
import time
import random
import datetime
import os
from typing import Dict, List

import pytz

from services.lotl_client import LotLClient
from config import settings
from config.prompts_unknown_contact import (
    UNKNOWN_CONTACT_ALLOWED_CLASSIFICATIONS,
    UNKNOWN_CONTACT_TRIAGE_SYSTEM_PROMPT,
)
from config import prompts_analyst_fact_extraction
from config import prompts_analyst_summarization
from config import prompts_analyst_trajectory
from config import prompts_analyst_strategic

logger = logging.getLogger(__name__)

# ESV (Emotional State Vector) Configuration
EMOTIONAL_EVENT_RETENTION_DAYS = 14  # Days to keep emotional events before pruning
DEFAULT_FALLBACK_DATE = "2000-01-01"  # Fallback date for events missing timestamp


# =============================================================================
# TIER 1 ANALYST SYSTEM PROMPT
# =============================================================================

ANALYST_SYSTEM_PROMPT = """[SYSTEM ROLE]
You are a clinical psychologist and relationship analyst.

[FUNCTION]
You generate a neutral, semantically dense CONTEXT REPORT for a downstream text-message generator.

[HARD CONSTRAINTS]
- Do NOT give instructions, advice, tactics, or step-by-step directions.
- Do NOT address the downstream model, the user, or "the delegate".
- Do NOT echo the input prompt. Do NOT include headers like "ANALYSIS REQUEST", "YOUR TASK", etc.
- Output MUST be valid JSON only (no markdown fences).

[OUTPUT SCHEMA]
Return a single JSON object with these keys:
- time_context: { target_local_time, operator_local_time, day_of_week, time_of_day }
- conversation_state: { last_inbound_summary, silence_gap_hint, emotional_tone, rapport_level }
- salient_memory: [strings]  (details worth remembering / continuity anchors)
- potential_sensitivities: [strings] (topics/phrases that might land poorly)
- language_style_observations: [strings] (how they text)
]"""


class AnalystService:
    """
    Unified Analyst Service - All intelligence tiers in one place.
    
    Architecture:
    - Tier 1 (Pre-Response): Neutral context report before delegate generates
    - Tier 2 (Facts): Extract concrete facts to knowledge graph
    - Tier 3 (Summary): Compress conversation into narrative
    - Tier 4 (Trajectory): Analyze relationship trends
    - Tier 5 (Strategic): High-level planning
    """
    
    def __init__(self, api_key: str | None = None, lotl_client: LotLClient = None):
        """
        Initialize unified analyst service.
        
        Args:
            api_key: API key for Gemini/OpenAI providers
            lotl_client: LotL client for Tier 1 analysis (optional)
        """
        self.provider = settings.LLM_PROVIDER
        self.api_key = api_key or settings.GEMINI_API_KEY
        self.lotl = lotl_client
        
        # Initialize provider
        self._init_provider()
        
        if not lotl_client:
            logger.warning("[ANALYST] No LotL client - Tier 1 LLM analysis DISABLED")
    
    def _init_provider(self):
        """Initialize the configured LLM provider."""
        if self.provider == "gemini" and self.api_key:
            import google.generativeai as genai
            configure_kwargs = {"api_key": self.api_key}
            base_url = os.environ.get("GEMINI_BASE_URL")
            if base_url:
                if base_url.startswith("https://"):
                    base_url = base_url.replace("https://", "")
                if base_url.endswith("/"):
                    base_url = base_url[:-1]
                configure_kwargs["client_options"] = {"api_endpoint": base_url}
                configure_kwargs["transport"] = "rest"
            genai.configure(**configure_kwargs)
        elif self.provider == "lotl":
            logger.info("Analyst using LotL provider (Chrome Controller)")
        elif not self.api_key and self.provider != "lotl":
            logger.warning("API Key missing for Analyst.")
    
    # =========================================================================
    # TIER 1: PRE-RESPONSE ANALYSIS (runs before every delegate call)
    # =========================================================================
    
    def analyze_pre_response(
        self, 
        profile: Dict, 
        messages: List[Dict],
        operator_location: str = None
    ) -> str:
        """
        Tier 1: Generate a neutral context report before generating response.
        
        Uses LLM with Google Search grounding (via LotL) to verify current time,
        analyze extended conversation history, and provide tactical guidance.
        
        Args:
            profile: Full contact profile including narrative_summaries, strategic_intel
            messages: Extended chat history (20+ recent interactions with timestamps)
            operator_location: Operator's location (defaults to settings.OPERATOR_LOCATION)
            
        Returns:
            JSON report string to inject into the delegate system prompt
        """
        if operator_location is None:
            operator_location = getattr(settings, 'OPERATOR_LOCATION', 'Los Angeles, CA')
            
        try:
            # Extract key profile data
            identity = profile.get("identity_matrix", {})
            contact_location = identity.get("location", identity.get("city", "Unknown"))
            contact_name = identity.get("name", profile.get("name", "Contact"))
            
            # Build the analyst prompt
            analyst_prompt = self._build_tier1_prompt(
                profile=profile,
                messages=messages,
                contact_name=contact_name,
                contact_location=contact_location,
                operator_location=operator_location
            )
            
            # Send to LotL for LLM analysis (Google Search grounding enabled)
            if self.lotl and self.lotl.is_available():
                logger.info(f"[ANALYST T1] Sending to LLM ({len(messages)} interactions)...")
                
                try:
                    # Normal Mode: Sequential execution, shared context is fine/managed by lock
                    target_platform = 'gemini'
                    if self.provider == 'copilot':
                        target_platform = 'copilot'
                    
                    raw_report = self.lotl.chat(
                        analyst_prompt, 
                        timeout=180,  # Gemini via LotL needs more time for big prompts
                        fresh=True,
                        platform=target_platform
                    )
                    
                    report = self._format_report(raw_report, contact_name)
                    if report and len(report.strip()) > 50:
                        logger.info(f"[ANALYST T1] LLM report complete ({len(report)} chars)")
                        return report
                    else:
                        logger.warning("[ANALYST T1] LLM returned empty/short response, using fallback")
                        
                except Exception as e:
                    logger.error(f"[ANALYST T1] LLM analysis failed: {e}")
            else:
                logger.warning("[ANALYST T1] LotL not available, using Python-only fallback")
            
            # Fallback to Python-only analysis
            return self._python_fallback_analysis(profile, messages, contact_location, operator_location)
            
        except Exception as e:
            logger.error(f"[ANALYST T1] Analysis failed: {e}")
            return self._minimal_fallback(profile)
    
    def _build_tier1_prompt(
        self,
        profile: Dict,
        messages: List[Dict],
        contact_name: str,
        contact_location: str,
        operator_location: str
    ) -> str:
        """Build the full prompt for Tier 1 LLM analysis with historical context."""
        parts = []
        
        # System instruction
        parts.append(ANALYST_SYSTEM_PROMPT)
        parts.append("\n" + "=" * 60 + "\n")
        
        # Context header with timezone info
        parts.append("## ANALYSIS REQUEST")
        parts.append(f"Contact: {contact_name}")
        
        identity = profile.get("identity_matrix", {})
        contact_timezone = identity.get("timezone", "America/New_York")
        parts.append(f"Their Location: {contact_location} (Timezone: {contact_timezone})")
        parts.append(f"Your Location: {operator_location}")
        parts.append("")
        
        # Unverified / Belief Status check
        unverified = profile.get("unverified", {})
        if unverified:
            parts.append("## STATUS: UNVERIFIED / PROBATION")
            try:
                first_seen = datetime.datetime.fromtimestamp(unverified.get('first_seen_epoch', 0)).strftime('%Y-%m-%d %H:%M:%S')
            except Exception:
                first_seen = "Unknown"
            parts.append(f"First Seen: {first_seen}")
            belief_json = json.dumps(unverified.get('belief_state', {}), indent=2)
            parts.append(f"Current Belief State: {belief_json}")
            parts.append("WARNING: Identity is not confirmed. Look for clues to confirm Gender and Intent.")
            parts.append("")
        
        # Time verification request
        parts.append("## STEP 1: VERIFY CURRENT TIME")
        parts.append(f"Use Google Search to find the EXACT current time in {contact_location} ({contact_timezone}) right now.")
        parts.append(f"ALSO calculate the current time for the operator in {operator_location}.")
        parts.append("Include the day of week and any notable events (holidays, etc.).")
        parts.append("")
        
        # Historical context - Prior 2 days summaries
        narrative_summaries = profile.get("narrative_summaries", [])
        if narrative_summaries:
            parts.append("## HISTORICAL CONTEXT (Prior 2 Days Summaries)")
            # Get most recent summaries (last 2-3)
            recent_summaries = narrative_summaries[-3:] if len(narrative_summaries) >= 3 else narrative_summaries
            for summary in recent_summaries:
                date_str = summary.get("date", "Unknown Date")
                summary_text = summary.get("summary", "")
                if isinstance(summary_text, dict):
                    summary_text = json.dumps(summary_text, separators=(',', ':'))
                parts.append(f"- [{date_str}]: {summary_text}")
            parts.append("")
        
        # Operational context
        strategic_intel = profile.get("strategic_intel", {})
        operational_context = strategic_intel.get("operational_context", "")
        if operational_context:
            parts.append("## OPERATIONAL CONTEXT")
            parts.append(operational_context)
            parts.append("")
        
        # Operational state
        op_state = profile.get("operational_state", {})
        if op_state:
            parts.append("## OPERATIONAL STATE")
            parts.append(f"- Current Phase: {op_state.get('current_phase', 'Unknown')}")
            parts.append("")
        
        # Extended conversation history
        parts.append("## EXTENDED CONVERSATION HISTORY (Recent Interactions)")
        for msg in messages:
            # Support both field naming conventions:
            # - is_from_me (legacy from fetch_recent_history)
            # - sender/role (from fetch_last_messages_with_timestamps)
            def _is_from_me(value: object) -> bool:
                if isinstance(value, bool):
                    return value
                if isinstance(value, (int, float)):
                    return int(value) == 1
                if isinstance(value, str):
                    v = value.strip().lower()
                    return v in {"1", "true", "yes", "y"}
                return False

            if "sender" in msg:
                s = str(msg.get("sender") or "").strip().lower()
                role = "ME" if s in {"you", "me", "myself", "operator"} else "THEY"
            elif "role" in msg:
                r = str(msg.get("role") or "").strip().lower()
                role = "ME" if r in {"assistant", "me"} else "THEY"
            else:
                role = "ME" if _is_from_me(msg.get("is_from_me")) else "THEY"
            time_ago = msg.get("time_ago", "")
            text = msg.get("text", "")
            if time_ago:
                parts.append(f"[{time_ago}] {role}: {text}")
            else:
                parts.append(f"{role}: {text}")
        parts.append("")
        
        parts.append("## YOUR TASK")
        parts.append("Return ONLY valid JSON matching the OUTPUT SCHEMA in the system prompt.")
        parts.append("No advice. No instructions. No markdown. No extra text.")
        
        return "\n".join(parts)
    
    @staticmethod
    def _extract_json_object(text: str) -> str | None:
        s = (text or "").strip()
        if not s:
            return None

        # Fast path: already a JSON object.
        if s.startswith("{") and s.endswith("}"):
            return s

        # Attempt to find the first JSON object region.
        start = s.find("{")
        end = s.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        candidate = s[start : end + 1].strip()
        if candidate.startswith("{") and candidate.endswith("}"):
            return candidate
        return None

    def _format_report(self, raw: str, contact_name: str) -> str:
        """Sanitize and normalize Tier-1 output into a JSON report string."""
        raw = (raw or "").strip()

        # Strip common grounding/UI artifacts and prompt-echo headers.
        drop_substrings = [
            "Google Search Suggestions",
            "Display of Search Suggestions is required",
            "Learn more weather forecast",
            "search suggestions",
        ]
        drop_headers = [
            "## ANALYSIS REQUEST",
            "## STEP 1: VERIFY CURRENT TIME",
            "## HISTORICAL CONTEXT",
            "## OPERATIONAL CONTEXT",
            "## RECENT CONVERSATION",
            "## YOUR TASK",
            "[System Role:",
            "[SYSTEM ROLE]",
        ]

        lines: list[str] = []
        for line in raw.splitlines():
            if any(s.lower() in line.lower() for s in drop_substrings):
                continue
            if any(line.strip().startswith(h) for h in drop_headers):
                continue
            lines.append(line)
        cleaned = "\n".join(lines).strip()

        json_text = self._extract_json_object(cleaned) or self._extract_json_object(raw)
        if json_text:
            try:
                obj = json.loads(json_text)
                if isinstance(obj, dict):
                    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
            except Exception:
                pass

        # Last resort: wrap as a report payload so delegate gets something structured.
        fallback = {
            "time_context": {},
            "conversation_state": {"last_inbound_summary": "", "silence_gap_hint": "", "emotional_tone": "", "rapport_level": ""},
            "salient_memory": [],
            "potential_sensitivities": [],
            "language_style_observations": [],
            "raw_report": cleaned or raw or f"[Tier 1 Analyst] Context report unavailable for {contact_name}.",
        }
        return json.dumps(fallback, ensure_ascii=False, separators=(",", ":"))
        
        # Strip Google Grounding artifacts
        google_markers = [
            "Google Search Suggestions", 
            "Display of Search Suggestions is required",
            "Learn more weather forecast",
            "search suggestions", 
        ]
        
        lines = advice.split('\n')
        clean_lines = []
        for line in lines:
            if any(m.lower() in line.lower() for m in google_markers):
                continue
            clean_lines.append(line)
            
        advice = '\n'.join(clean_lines).strip()
        
        if not advice:
            return f"[Tier 1 Analyst] Analyzing conversation with {contact_name}..."
        return advice
    
    def _python_fallback_analysis(
        self, 
        profile: Dict, 
        messages: List[Dict],
        contact_location: str,
        operator_location: str
    ) -> str:
        """Python-only fallback when LLM is unavailable (JSON report)."""
        identity = profile.get("identity_matrix", {})
        contact_name = identity.get("name", "Contact")
        contact_timezone = identity.get("timezone", "America/New_York")
        
        # Calculate current time
        try:
            tz = pytz.timezone(contact_timezone)
            now = datetime.datetime.now(tz)
            day_name = now.strftime("%A")
            time_str = now.strftime("%I:%M %p")
            hour = now.hour
            
            if 5 <= hour < 12:
                time_of_day = "morning"
            elif 12 <= hour < 17:
                time_of_day = "afternoon"
            elif 17 <= hour < 21:
                time_of_day = "evening"
            else:
                time_of_day = "night"
        except Exception:
            now = datetime.datetime.now()
            day_name = now.strftime("%A")
            time_str = now.strftime("%I:%M %p")
            time_of_day = "unknown"
        
        last_inbound_text = ""
        if messages:
            last = messages[-1]
            last_inbound_text = str(last.get("text") or "").strip()

        report = {
            "time_context": {
                "target_local_time": f"{time_str} ({contact_location})",
                "operator_local_time": "",
                "day_of_week": day_name,
                "time_of_day": time_of_day,
            },
            "conversation_state": {
                "last_inbound_summary": (last_inbound_text[:180] + "…") if len(last_inbound_text) > 180 else last_inbound_text,
                "silence_gap_hint": "",
                "emotional_tone": "unknown",
                "rapport_level": "unknown",
            },
            "salient_memory": [],
            "potential_sensitivities": [],
            "language_style_observations": [],
            "raw_report": "Python fallback (LLM unavailable).",
        }

        return json.dumps(report, ensure_ascii=False, separators=(",", ":"))
    
    def _minimal_fallback(self, profile: Dict) -> str:
        """Minimal fallback when all analysis fails (JSON report)."""
        identity = profile.get("identity_matrix", {})
        name = identity.get("name", "them")
        report = {
            "time_context": {},
            "conversation_state": {"last_inbound_summary": "", "silence_gap_hint": "", "emotional_tone": "", "rapport_level": ""},
            "salient_memory": [],
            "potential_sensitivities": [],
            "language_style_observations": [],
            "raw_report": f"Context report unavailable for {name}.",
        }
        return json.dumps(report, ensure_ascii=False, separators=(",", ":"))
    
    # =========================================================================
    # TIER 2: FACT EXTRACTION
    # =========================================================================
    
    def extract_facts(self, conversation_history: str) -> Dict:
        """
        Tier 2: Extract concrete facts to populate knowledge graph.
        
        Args:
            conversation_history: Formatted conversation text
            
        Returns:
            Dict with "facts" key containing list of extracted facts
        """
        return self._run_analysis(
            prompts_analyst_fact_extraction.SYSTEM_PROMPT,
            f"CONVERSATION LOG:\n{conversation_history}"
        )
    
    # =========================================================================
    # TIER 3: SUMMARIZATION
    # =========================================================================
    
    def summarize_period(self, conversation_history: str) -> Dict:
        """
        Tier 3: Compress logs into narrative summary.
        
        Args:
            conversation_history: Formatted conversation text
            
        Returns:
            Dict with "period_summary" key containing compressed narrative
        """
        return self._run_analysis(
            prompts_analyst_summarization.SYSTEM_PROMPT,
            f"CONVERSATION LOG:\n{conversation_history}"
        )
    
    # =========================================================================
    # TIER 4: TRAJECTORY
    # =========================================================================
    
    def analyze_trajectory(self, historical_metrics: str, current_window: str) -> Dict:
        """
        Tier 4: Analyze relationship health/velocity trends.
        
        Args:
            historical_metrics: JSON of previous trajectory data
            current_window: Recent conversation text
            
        Returns:
            Dict with trajectory metrics (sentiment_slope, etc.)
        """
        content = (
            f"HISTORICAL BASELINE:\n{historical_metrics}\n\n"
            f"CURRENT WINDOW:\n{current_window}"
        )
        return self._run_analysis(prompts_analyst_trajectory.SYSTEM_PROMPT, content)
    
    # =========================================================================
    # TIER 5: STRATEGIC
    # =========================================================================
    
    def generate_strategic_report(self, profile_summary: str, recent_history: str) -> Dict:
        """
        Tier 5: High-level executive strategy and game theory.
        
        Args:
            profile_summary: JSON of psychometric profile
            recent_history: Recent conversation text
            
        Returns:
            Dict with strategic recommendations
        """
        content = (
            f"SUBJECT PROFILE:\n{profile_summary}\n\n"
            f"RECENT INTERACTION:\n{recent_history}"
        )
        return self._run_analysis(prompts_analyst_strategic.SYSTEM_PROMPT, content)
    
    # =========================================================================
    # BATCH ANALYSIS (Post-Interaction Pipeline)
    # =========================================================================
    
    def run_post_interaction_pipeline(
        self, 
        handle: str, 
        profile: Dict, 
        history: List[Dict],
        archivist
    ) -> Dict:
        """
        Runs Tier 2-5 with sampling strategy.
        Called after successful message send.
        
        Sampling:
        - Tier 2 (Facts): Every interaction
        - Tier 3 (Summary): Every 10 interactions
        - Tier 4 (Trajectory): Every 20 interactions
        - Tier 5 (Strategic): Every 20 interactions
        
        Args:
            handle: Contact handle
            profile: Full contact profile
            history: Recent message history
            archivist: Archivist instance for saving profile
            
        Returns:
            Updated profile dict
        """
        # Filter out system messages and artifacts to prevent prompt pollution
        clean_history = []
        for m in history:
            txt = m.get('text', '')
            # Filter internal approval requests, refused drafts, and system noise
            if "⚠️ APPROVAL" in txt or "COMMAND OVERRIDE" in txt:
                continue
            # Filter "Chain of Thought" leaks
            if "Thinking" in txt and ("Defining" in txt or "Assessing" in txt):
                continue
            clean_history.append(m)

        history_text = "\n".join([f"{m['role'].upper()}: {m['text']}" for m in clean_history])
        
        # --- TIER 2: FACT EXTRACTION (Always Run) ---
        try:
            extraction = self.extract_facts(history_text)
            new_facts = extraction.get("facts", [])
            
            # Simple append logic for now - a real KG would deduplicate
            if "knowledge_graph" not in profile:
                profile["knowledge_graph"] = []
            
            if new_facts:
                profile["knowledge_graph"].extend(new_facts)
                logger.info(f"[ANALYST T2] Extracted {len(new_facts)} new facts")
            
            # NEW: Process emotional events
            new_emotions = extraction.get("emotional_events", [])
            if new_emotions:
                if "psychometric_profile" not in profile:
                    profile["psychometric_profile"] = {}
                if "emotional_events" not in profile["psychometric_profile"]:
                    profile["psychometric_profile"]["emotional_events"] = []
                
                # Add timestamp to each new event (timezone-aware UTC)
                now_utc = datetime.datetime.now(pytz.UTC)
                now_iso = now_utc.isoformat()
                for event in new_emotions:
                    event["detected_at"] = now_iso
                    profile["psychometric_profile"]["emotional_events"].append(event)
                
                # Prune old events using the same timezone-aware reference time
                cutoff = now_utc - datetime.timedelta(days=EMOTIONAL_EVENT_RETENTION_DAYS)
                pruned_events = []
                for event in profile["psychometric_profile"]["emotional_events"]:
                    raw_detected_at = event.get("detected_at", DEFAULT_FALLBACK_DATE)
                    try:
                        detected_at_dt = datetime.datetime.fromisoformat(raw_detected_at)  # type: ignore[arg-type]
                    except (TypeError, ValueError):
                        logger.warning(
                            "Invalid detected_at timestamp %r in emotional_events; using DEFAULT_FALLBACK_DATE.",
                            raw_detected_at,
                        )
                        try:
                            detected_at_dt = datetime.datetime.fromisoformat(DEFAULT_FALLBACK_DATE)  # type: ignore[arg-type]
                        except (TypeError, ValueError):
                            # As a last resort, keep the event without pruning it out, but avoid crashing.
                            pruned_events.append(event)
                            continue
                    if detected_at_dt > cutoff:
                        pruned_events.append(event)
                profile["psychometric_profile"]["emotional_events"] = pruned_events
                
                logger.info(f"[ANALYST T2] Added {len(new_emotions)} emotional events")
        except Exception as e:
            logger.error(f"Tier 2 failed: {e}")

        # --- TIER 3 & 4 (Sampling Strategy) ---
        # Deterministic, monotonic interaction counter stored on the profile
        raw_interaction_count = profile.get("interaction_count", 0)
        if isinstance(raw_interaction_count, int) and raw_interaction_count >= 0:
            interaction_count = raw_interaction_count + 1
        else:
            # Initialize if missing or malformed
            interaction_count = 1
        profile["interaction_count"] = interaction_count

        # Summarize every 10 messages
        if interaction_count % 10 == 0:
            try:
                logger.info(f"Running Tier 3 Summarization for {handle}...")
                summary_data = self.summarize_period(history_text)
                
                if "narrative_summaries" not in profile:
                    profile["narrative_summaries"] = []
                
                profile["narrative_summaries"].append({
                    "date": datetime.datetime.now().isoformat(),
                    "summary": summary_data.get("period_summary", "")
                })
            except Exception as e:
                logger.error(f"Tier 3 failed: {e}")

        # Analyze Trajectory every 20 messages
        if interaction_count % 20 == 0:
            try:
                logger.info(f"Running Tier 4 Trajectory Analysis for {handle}...")
                # Get last trajectory if exists
                last_traj = profile.get("trajectory_metrics", {})
                
                new_traj = self.analyze_trajectory(
                    json.dumps(last_traj),  # Historical
                    history_text             # Current Window
                )
                profile["trajectory_metrics"] = new_traj
                logger.info(f"[ANALYST T4] Sentiment Slope: {new_traj.get('sentiment_slope')}")
            except Exception as e:
                logger.error(f"Tier 4 failed: {e}")

        # --- TIER 5: STRATEGIC (Update Strategy Board) ---
        if interaction_count % 20 == 0:
            try:
                logger.info(f"Running Tier 5 Strategic Analysis for {handle}...")
                strategy = self.generate_strategic_report(
                    json.dumps(profile.get("psychometric_profile", {})), 
                    history_text
                )
                profile["strategic_state"] = strategy
            except Exception as e:
                logger.error(f"Tier 5 failed: {e}")

        # Final Save
        archivist.update_profile(handle, profile)
        logger.info(f"[ANALYST] Pipeline complete for {handle}")
        
        return profile
    
    # =========================================================================
    # PROVIDER HELPERS
    # =========================================================================
    
    def _run_analysis(self, system_prompt: str, user_content: str) -> Dict:
        """Helper to route analysis requests to the configured provider."""
        if self.provider in {"lotl", "copilot"}:
            # Default to Copilot (which now has fallback to Gemini -> AI Studio)
            platform = "copilot"
            return self._lotl_analyze(system_prompt, user_content, {}, platform=platform)
        return self._gemini_analyze(system_prompt, user_content, {})

    def analyze_unknown_contact(self, *, handle: str, inbound_text: str) -> Dict:
        """Triage an unknown inbound contact.

        Goal: generate operator-visible safety triage + a suggested first reply.
        This is intentionally non-manipulative: it focuses on spam/scam detection
        and a polite, low-risk response draft.
        """
        system_prompt = UNKNOWN_CONTACT_TRIAGE_SYSTEM_PROMPT

        user_content = (
            f"UNKNOWN CONTACT HANDLE: {handle}\n"
            f"INBOUND MESSAGE: {inbound_text}\n"
        )

        result = self._run_analysis(system_prompt, user_content)
        if not isinstance(result, dict):
            return {
                "classification": "unknown",
                "confidence": 0.0,
                "risk_flags": ["analysis_failed"],
                "belief_state": {
                    "p_male": 0.5, "p_female": 0.5, "p_dating": 0.0, "p_business": 0.0, "p_friendship": 0.0, "age_estimate": "unknown"
                },
                "suggested_reply": "Hey — good to hear from you. Hope your day’s going well.",
                "notes": "- analysis failed; defaulting to safe neutral reply",
            }

        # Defensive normalization
        classification = str(result.get("classification", "unknown"))
        if classification not in UNKNOWN_CONTACT_ALLOWED_CLASSIFICATIONS:
            classification = "unknown"
        try:
            confidence = float(result.get("confidence", 0.0))
        except Exception:
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))
        risk_flags = result.get("risk_flags")
        if not isinstance(risk_flags, list):
            risk_flags = []

        demographics_raw = result.get("demographics", {})
        if not isinstance(demographics_raw, dict):
            demographics_raw = {}

        # Construct probabilistic belief state with robust fallbacks
        def _get_prob(key: str) -> float:
            try:
                val = float(demographics_raw.get(key, 0.0))
                return max(0.0, min(1.0, val))
            except (ValueError, TypeError):
                return 0.0

        belief_state = {
            "p_male": _get_prob("p_male"),
            "p_female": _get_prob("p_female"),
            "p_dating": _get_prob("p_dating"),
            "p_business": _get_prob("p_business"),
            "p_friendship": _get_prob("p_friendship"),
            "age_estimate": str(demographics_raw.get("age_estimate", "unknown"))
        }
        
        suggested_reply = str(result.get("suggested_reply") or "").strip()
        if not suggested_reply:
            suggested_reply = "Hey — good to hear from you. Hope your day’s going well."

        notes = str(result.get("notes") or "").strip()
        if not notes:
            notes = "- no additional notes"

        return {
            "classification": classification,
            "confidence": confidence,
            "risk_flags": [str(x) for x in risk_flags if str(x).strip()],
            "belief_state": belief_state,
            "suggested_reply": suggested_reply,
            "notes": notes,
        }
    
    def _gemini_analyze(self, prompt: str, user_content: str, profile: Dict) -> Dict:
        """Gemini API analysis."""
        import google.generativeai as genai
        
        model_name = settings.GEMINI_MODEL or "gemini-2.5-flash"
        model = genai.GenerativeModel(
            model_name,
            system_instruction=prompt,
            generation_config={"response_mime_type": "application/json"}
        )

        try:
            response = model.generate_content(user_content)
            text = response.text
            # Strip markdown if present
            if text.startswith("```json"):
                text = text[7:-3]
            elif text.startswith("```"):
                text = text[3:-3]
                
            result = json.loads(text)
            logger.info("Analyst successfully completed analysis")
            return result
            
        except Exception as e:
            if "429" in str(e):
                logger.warning("Analyst hit Rate Limit")
            logger.error(f"Analyst failed: {e}")
            return profile if profile else {}
    
    def _lotl_analyze(self, prompt: str, user_content: str, profile: Dict, *, platform: str = "gemini") -> Dict:
        """
        LotL (Living off the Land) analysis - routes through AI Studio via Chrome.
        Bypasses API quotas entirely.
        """
        if not self.lotl:
            logger.error("LotL client not configured")
            return profile if profile else {}
        
        if not self.lotl.is_available():
            logger.error(f"LotL Controller not available at {settings.LOTL_BASE_URL}")
            return profile if profile else {}
        
        # Combine system prompt and user content
        full_prompt = (
            f"SYSTEM:\n{prompt}\n\n"
            f"USER REQUEST:\n{user_content}\n\n"
            f"IMPORTANT: Return ONLY valid JSON. No markdown, no explanation."
        )
        
        def _should_retry(err_or_text: str) -> bool:
            t = str(err_or_text or "").strip().lower()
            if not t:
                return False
            known = [
                "stop generation before creating a new chat",
                "verify it's you",
                "unusual traffic",
                "captcha",
                "sign in",
                "something went wrong",
            ]
            return any(k in t for k in known)

        def _strip_fences(text: str) -> str:
            t = str(text or "").strip()
            if t.startswith("```json"):
                t = t[7:]
            if t.startswith("```"):
                t = t[3:]
            if t.endswith("```"):
                t = t[:-3]
            return t.strip()

        response_text: str | None = None

        # Attempt 1
        try:
            response_text = str(self.lotl.chat(full_prompt, platform=platform)).strip()
        except (TimeoutError, RuntimeError) as exc:
            # Attempt 2: retry on the SAME tab/session.
            # IMPORTANT: Do NOT use per-request session IDs or fresh tabs here.
            # In normal mode the controller reuses a single logged-in Chrome tab;
            # unique sessionIds can bypass the controller's per-session lock and
            # cause overlapping UI automation (interrupting active generations).
            if isinstance(exc, TimeoutError) or _should_retry(str(exc)):
                try:
                    import time
                    time.sleep(5)
                    # Use specific session for analysis context
                    response_text = str(
                        self.lotl.chat(
                            full_prompt, 
                            session_id="tier1_analyst",
                            platform=platform,
                        )
                    ).strip()
                except Exception as e2:
                    logger.error(f"Analyst (LotL) retry failed: {e2}")
                    return profile if profile else {}
            else:
                try:
                    # Final attempt with explicit session
                    response_text = str(
                        self.lotl.chat(
                            full_prompt, 
                            session_id="tier1_analyst",
                            platform=platform,
                        )
                    ).strip()
                except:
                   logger.error(f"Analyst (LotL) failed: {exc}")
                   return profile if profile else {}
        except Exception as exc:
            logger.error(f"Analyst (LotL) failed: {exc}")
            return profile if profile else {}

        try:
            result = json.loads(_strip_fences(response_text or ""))
            logger.info("Analyst (LotL) successfully completed analysis")
            return result
        except Exception as exc:
            logger.error(f"Analyst (LotL) returned invalid JSON: {exc}")
            return profile if profile else {}
