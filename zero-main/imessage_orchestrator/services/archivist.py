from __future__ import annotations

import json
import logging
import copy
import datetime
from typing import Any, Dict, List
from pathlib import Path
from config import prompts
from config.prompts import PROACTIVE_INITIATION_INJECTION
from utils.atomic import atomic_write_json

logger = logging.getLogger(__name__)

# ESV (Emotional State Vector) Configuration
MIN_SIGNIFICANT_INTENSITY = 0.2  # Minimum intensity for emotion to be included in context
STRONG_INTENSITY_THRESHOLD = 0.6  # Threshold for "strong" emotion label
MODERATE_INTENSITY_THRESHOLD = 0.4  # Threshold for "moderate" emotion label
HOURS_PER_DAY = 24  # Time conversion factor

# Advanced Red Team Schema V4.0 - Split Architecture
# 
# PART A: LLM_CONTEXT_SCHEMA - Fields sent to the delegate LLM
# These fields provide strategic context for response generation
LLM_CONTEXT_SCHEMA: Dict[str, Any] = {
    "identity_matrix": {
        "name": "Target_Subject",
        "city": "Unknown",
        "timezone": "America/New_York",
        "personal_information": {
            "occupation": "Unknown",
            "relationship_history": "Unknown",
            "notes": ""
        }
    },
    "psychometric_profile": {
        "attachment_style": "Unknown",
        "emotional_baseline": "Neutral",
        "love_language_primary": "Unknown",
        "vulnerability_stack": [],
        "emotional_events": []
    },
    "strategic_intel": {
        "operational_context": "",  # High-level narrative (e.g., "The Poland Recurrence")
        "current_vector": ""        # Specific active approach
    },
    "operational_state": {
        "current_phase": "Phase 1: Calibration",
        "narrative_arc": {
            "shared_secrets": [],
            "future_projections": []
        }
    },
    "knowledge_graph": [],
    "narrative_summaries": [],
    "operator_context": ""
}

# PART B: ORCHESTRATOR_STATE_SCHEMA - Orchestrator-only fields (NOT sent to LLM)
# These fields control Python-side behavior and timing logic
ORCHESTRATOR_STATE_SCHEMA: Dict[str, Any] = {
    "handle": "",  # Moved from identity_matrix
    "last_interaction_epoch": 0.0,  # Moved from identity_matrix
    "pacing_engine": {
        "average_latency_seconds": 60.0,
        "variable_reward_ratio": 0.5,
        "next_scheduled_event": 0,
        "active_hours_start": 9,
        "active_hours_end": 23,
        "initiation_enabled": False,
        "force_trigger": False,
        "quiet_hours": {
            "enabled": False,
            "start_hour": 23,
            "end_hour": 8,
            "ignore_user_messages": True
        }
    },
    "requires_approval": False,  # Shadow mode control
    "mute_agent": False,  # Agent silencing control
    "trajectory_metrics": {},  # Reserved for future use
    "strategic_state": {}  # Reserved for future use
}

# COMBINED: DEFAULT_PROFILE_TEMPLATE - Backward compatibility for load/merge
# This is used for loading existing profiles and merging defaults
# The build_context_payload() will filter out orchestrator-only fields before sending to LLM
DEFAULT_PROFILE_TEMPLATE: Dict[str, Any] = {
    "identity_matrix": {
        "handle": "",
        "name": "Target_Subject",
        "city": "Unknown",
        "timezone": "America/New_York",
        "last_interaction_epoch": 0.0,
        "personal_information": {
            "occupation": "Unknown",
            "relationship_history": "Unknown",
            "notes": ""
        }
    },
    "psychometric_profile": {
        "attachment_style": "Unknown",
        "emotional_baseline": "Neutral",
        "love_language_primary": "Unknown",
        "vulnerability_stack": [],
        "emotional_events": []
    },
    "strategic_intel": {
        "operational_context": "",
        "current_vector": ""
    },
    "operational_state": {
        "current_phase": "Phase 1: Calibration",
        "narrative_arc": {
            "shared_secrets": [],
            "future_projections": []
        }
    },
    "pacing_engine": {
        "average_latency_seconds": 60.0,
        "variable_reward_ratio": 0.5,
        "next_scheduled_event": 0,
        "active_hours_start": 9,
        "active_hours_end": 23,
        "initiation_enabled": False,
        "force_trigger": False,
        "quiet_hours": {
            "enabled": False,
            "start_hour": 23,
            "end_hour": 8,
            "ignore_user_messages": True
        }
    },
    "knowledge_graph": [],
    "narrative_summaries": [],
    "operator_context": "",
    "requires_approval": False,
    "mute_agent": False,
    "trajectory_metrics": {},
    "strategic_state": {}
}

class Archivist:
    """Context Manager: Parses refined JSON fields and injects them into the system prompt."""

    def __init__(self, contacts_dir: Path | str = "/Users/Shared/ProjectZero/contacts/") -> None:
        if isinstance(contacts_dir, str):
            # Fallback to local if absolute path is not meant to be used literally or permissions fail
            # But user requested this path. For this environment, I will use workspace relative path if possible, 
            # but the code supports passing it in.
            contacts_dir = Path(contacts_dir)
        self.base_path = contacts_dir
        
        try:
            self.base_path.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            # Fallback for restricted environment
            self.base_path = Path("imessage_orchestrator/data/contacts")
            self.base_path.mkdir(parents=True, exist_ok=True)
            logger.warning(f"Could not use requested path, falling back to {self.base_path}")

        self._archive_legacy_duplicate_profiles()

    def _archive_legacy_duplicate_profiles(self) -> None:
        """Moves legacy numeric-only duplicate profiles into a subfolder.

        Prevents operators accidentally editing the wrong profile when both
        `150...json` and `+150...json` exist. Preserves legacy files under
        `_legacy/` for audit/rollback.
        """
        try:
            files = list(self.base_path.glob("*.json"))
            stem_set = {f.stem for f in files}
            legacy_dir = self.base_path / "_legacy"

            for f in files:
                stem = f.stem
                if not stem.isdigit():
                    continue
                if ("+" + stem) not in stem_set:
                    continue

                legacy_dir.mkdir(parents=True, exist_ok=True)
                dest = legacy_dir / f.name
                if dest.exists():
                    ts = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
                    dest = legacy_dir / f"{stem}.{ts}.json"

                f.rename(dest)
                logger.warning(
                    f"Archived legacy duplicate profile {f.name} -> {dest.relative_to(self.base_path)}"
                )
        except Exception as e:
            logger.warning(f"Failed to archive legacy duplicate profiles: {e}")

    def _path_for(self, handle: str) -> Path:
        # Keep the + prefix - it's part of the handle identity
        safe_handle = handle.strip()
        return self.base_path / f"{safe_handle}.json"

    def _canonicalize_handle(self, handle: str) -> str:
        h = handle.strip()
        if h.startswith("+"):
            return h
        # If it looks like a phone number, normalize to +E.164-ish
        if h.isdigit():
            return "+" + h
        return h

    def load_profile(self, handle: str) -> Dict:
        canonical = self._canonicalize_handle(handle)
        file_path = self._path_for(canonical)
        legacy_path = self._path_for(handle)
        
        # Backward-compatible: if caller uses +handle but file exists without '+', load legacy.
        if not file_path.exists() and handle != canonical and legacy_path.exists():
            file_path = legacy_path

        if file_path.exists():
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    # Merge with default to ensure new fields exist
                    # Ensure identity handle uses canonical form
                    return self._merge_defaults(data, DEFAULT_PROFILE_TEMPLATE, canonical)
            except Exception as e:
                logger.warning(f"Failed to load profile for {handle}: {e}")
                return self._create_template(canonical)
        else:
            return self._create_template(canonical)

    def has_profile(self, handle: str) -> bool:
        """Check if a profile exists on disk for this handle."""
        canonical = self._canonicalize_handle(handle)
        return self._path_for(canonical).exists()

    def delete_profile(self, handle: str) -> bool:
        """Permanently remove a profile from disk."""
        canonical = self._canonicalize_handle(handle)
        path = self._path_for(canonical)
        if path.exists():
            try:
                path.unlink()
                logger.info(f"[ARCHIVIST] Deleted profile: {path}")
                return True
            except Exception as e:
                logger.error(f"[ARCHIVIST] Failed to delete {path}: {e}")
                return False
        return False


    def _create_template(self, handle: str) -> Dict:
        import copy # Ensure copy is imported locally just in case of weird scoping/reload issues
        profile = copy.deepcopy(DEFAULT_PROFILE_TEMPLATE)
        profile["identity_matrix"]["handle"] = handle
        now_ts = datetime.datetime.now().timestamp()
        profile["identity_matrix"]["last_interaction_epoch"] = now_ts
        return profile

    def get_all_handles(self) -> List[str]:
        """Returns a list of all handles with profiles."""
        stems = [f.stem for f in self.base_path.glob("*.json")]
        stem_set = set(stems)

        handles: list[str] = []
        seen: set[str] = set()

        for stem in stems:
            canonical = self._canonicalize_handle(stem)

            # If both 150... and +150... exist, prefer the + form.
            if stem.isdigit() and ("+" + stem) in stem_set:
                # If the legacy file has a force_trigger, mirror it over to the canonical file.
                try:
                    legacy = self.load_profile(stem)
                    canonical_profile = self.load_profile("+" + stem)
                    legacy_force = bool(legacy.get("pacing_engine", {}).get("force_trigger", False))
                    canonical_force = bool(canonical_profile.get("pacing_engine", {}).get("force_trigger", False))
                    if legacy_force and not canonical_force:
                        canonical_profile.setdefault("pacing_engine", {})["force_trigger"] = True
                        self.update_profile("+" + stem, canonical_profile)
                        legacy.setdefault("pacing_engine", {})["force_trigger"] = False
                        self.update_profile(stem, legacy)
                except Exception:
                    pass
                continue

            if canonical in seen:
                continue
            seen.add(canonical)
            handles.append(canonical)

        return handles

    def _compute_immediate_context(self, profile: Dict) -> Dict:
        """Compute live time awareness for the Delegate."""
        from datetime import datetime
        from zoneinfo import ZoneInfo
        
        ident = profile.get("identity_matrix", {})
        tz_name = ident.get("timezone", "America/New_York")

        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = ZoneInfo("America/New_York")

        now = datetime.now(tz)
        hour = now.hour
        day_name = now.strftime("%A")
        
        # Determine time of day bucket
        if 5 <= hour < 12:
            time_of_day = "morning"
        elif 12 <= hour < 17:
            time_of_day = "afternoon"
        elif 17 <= hour < 21:
            time_of_day = "evening"
        else:
            time_of_day = "night"
        
        # Calculate hours since last contact
        last_epoch = ident.get("last_interaction_epoch", 0)
        hours_since = (now.timestamp() - last_epoch) / 3600 if last_epoch else 999
        
        # Infer likely activity based on day/time
        # DEPRECATED: Hardcoded heuristics are less accurate than the LLM's intuition.
        # We pass raw time context and let the Delegate infer state.
        likely_activity = "Unknown" 
        
        is_weekend = day_name in ["Saturday", "Sunday"]

        # Suggest tone based on context
        if hours_since > 48:
            suggested_tone = "warm re-engagement"
        elif hours_since > 24:
            suggested_tone = "casual check-in"
        elif time_of_day == "night":
            suggested_tone = "intimate/low-key"
        elif time_of_day == "morning":
            suggested_tone = "light/brief"
        else:
            suggested_tone = "casual"
        
        return {
            "current_day_of_week": day_name,
            "current_time_of_day": time_of_day,
            "local_hour": hour,
            "hours_since_last_contact": round(hours_since, 1),
            "target_likely_activity": likely_activity,
            "suggested_tone": suggested_tone,
            "is_weekend": is_weekend
        }

    def _merge_defaults(self, data: Dict, template: Dict, handle: str, _depth: int = 0) -> Dict:
        """Recursively merge defaults so we don't break if schema evolves."""
        for k, v in template.items():
            if k not in data:
                data[k] = v
            elif isinstance(v, dict) and isinstance(data[k], dict):
                self._merge_defaults(data[k], v, handle, _depth + 1)
                
        # Ensure handle is set at root level
        if _depth == 0 and "identity_matrix" in data:
            data["identity_matrix"]["handle"] = handle
        return data

    def _filter_for_llm_context(self, profile: Dict) -> Dict:
        """
        Filter profile to only include fields that should be sent to the LLM.
        Removes orchestrator-only fields like pacing_engine, requires_approval, etc.
        
        This prevents wasting tokens on internal state that the delegate doesn't need.
        """
        
        # Create a filtered copy
        filtered = {}
        
        
        # Include identity_matrix but strip orchestrator-only fields
        if "identity_matrix" in profile:
            im = copy.deepcopy(profile["identity_matrix"])
            # Remove orchestrator-only fields
            im.pop("handle", None)  # Not needed by LLM
            im.pop("last_interaction_epoch", None)  # Not needed by LLM
            filtered["identity_matrix"] = im
        
        # Include psychometric_profile (all fields are LLM-relevant)
        if "psychometric_profile" in profile:
            filtered["psychometric_profile"] = copy.deepcopy(profile["psychometric_profile"])
        
        # Include strategic_intel (all fields are LLM-relevant)
        if "strategic_intel" in profile:
            filtered["strategic_intel"] = copy.deepcopy(profile["strategic_intel"])
        
        # Include operational_state but strip orchestrator-only fields
        if "operational_state" in profile:
            op = copy.deepcopy(profile["operational_state"])
            # Remove deprecated/unused metrics
            op.pop("limerence_index", None)  # Deprecated
            op.pop("compliance_score", None)  # Deprecated
            op.pop("active_tactic", None)  # Deprecated
            filtered["operational_state"] = op
        
        # Include knowledge_graph (LLM-relevant)
        if "knowledge_graph" in profile:
            filtered["knowledge_graph"] = copy.deepcopy(profile["knowledge_graph"])
        
        # Include narrative_summaries (LLM-relevant)
        if "narrative_summaries" in profile:
            filtered["narrative_summaries"] = copy.deepcopy(profile["narrative_summaries"])
        
        # Include operator_context (LLM-relevant)
        if "operator_context" in profile:
            filtered["operator_context"] = profile["operator_context"]
        
        # Explicitly EXCLUDE orchestrator-only fields:
        # - pacing_engine (timing logic only)
        # - requires_approval (orchestrator control only)
        # - mute_agent (orchestrator control only)
        # - trajectory_metrics (internal tracking only)
        # - strategic_state (internal tracking only)
        # - linguistic_mirror (deprecated, never used)
        # - immediate_context (computed dynamically, not stored)
        
        return filtered

    def _get_active_emotional_context(self, profile: Dict, max_age_days: float = 7.0) -> str:
        """
        Calculate active emotions with time-based decay.
        
        Emotions decay exponentially based on their decay_days (half-life).
        Returns a formatted string for injection into delegate context.
        
        Args:
            profile: The contact profile
            max_age_days: Ignore emotions older than this many days
            
        Returns:
            Formatted string describing active emotional carryover, or empty string
        """
        events = profile.get("psychometric_profile", {}).get("emotional_events", [])
        if not events:
            return ""
        
        now = datetime.datetime.now()
        active = []
        
        for event in events:
            try:
                detected_at = datetime.datetime.fromisoformat(event.get("detected_at", ""))
                age_days = (now - detected_at).total_seconds() / datetime.timedelta(days=1).total_seconds()
                
                # Skip if too old
                if age_days > max_age_days:
                    continue
                
                # Calculate decayed intensity using half-life formula
                # intensity_now = intensity_original * (0.5 ** (age_days / decay_days))
                decay_days = float(event.get("decay_days", 3.0))
                original_intensity = float(event.get("intensity", 0.5))
                
                if decay_days <= 0:
                    decay_days = 3.0  # Default half-life
                    
                decayed_intensity = original_intensity * (0.5 ** (age_days / decay_days))
                
                # Only include if still significant
                if decayed_intensity >= MIN_SIGNIFICANT_INTENSITY:
                    emotion = event.get("emotion", "unknown")
                    context = event.get("context", "")
                    age_str = f"{age_days:.1f} days ago" if age_days >= 1 else f"{age_days * HOURS_PER_DAY:.0f} hours ago"
                    
                    active.append({
                        "emotion": emotion,
                        "current_intensity": round(decayed_intensity, 2),
                        "original_intensity": original_intensity,
                        "age": age_str,
                        "context": context
                    })
            except (ValueError, TypeError):
                continue
        
        if not active:
            return ""
        
        # Format for delegate injection
        lines = ["## EMOTIONAL CARRYOVER (Object Permanence)"]
        lines.append("The following emotional states are still active from recent interactions:")
        lines.append("")
        
        for item in sorted(active, key=lambda x: x["current_intensity"], reverse=True):
            intensity_label = "strong" if item["current_intensity"] >= STRONG_INTENSITY_THRESHOLD else "moderate" if item["current_intensity"] >= MODERATE_INTENSITY_THRESHOLD else "fading"
            lines.append(f"- **{item['emotion'].title()}** ({intensity_label}, {item['age']}): {item['context']}")
        
        lines.append("")
        lines.append("CONSTRAINT: Acknowledge their emotional state. Do not ignore recent vulnerability.")
        
        return "\n".join(lines)

    def build_context_payload(
        self, 
        profile: Dict,
        recent_messages: List[Dict] = None,
        analyst_report: str = None,
        is_proactive: bool = False
    ) -> Dict:
        """Builds the context payload for the Delegate.
        
        Architecture:
        1. Tier 1 Context Analyst runs FIRST (external to this method)
        2. Analyst produces a neutral context report (JSON string)
        3. This method injects: Time + Filtered Profile JSON + Messages + Analyst Report
        
        The profile is filtered to remove orchestrator-only fields (pacing_engine, etc.)
        to avoid wasting tokens on internal state that the delegate doesn't need.
        
        Args:
            profile: The target's full profile (JSON)
            recent_messages: Last N messages with timestamps
            analyst_report: Pre-computed context report from Tier 1 Analyst (JSON string)
            is_proactive: If True, inject initiation mode instructions (force_trigger/proactive)
            
        Returns:
            Dict with system_instruction and contact fields
        """
        import pytz
        
        base_prompt = prompts.GLOBAL_PERSONA_SYSTEM_PROMPT
        
        # --- PROACTIVE INITIATION INJECTION ---
        if is_proactive:
            base_prompt += "\n" + PROACTIVE_INITIATION_INJECTION
        
        # --- CURRENT TIME AWARENESS ---
        now = datetime.datetime.now()
        target_tz_str = profile.get("identity_matrix", {}).get("timezone", "America/New_York")
        try:
            target_tz = pytz.timezone(target_tz_str)
            target_now = datetime.datetime.now(target_tz)
        except:
            target_now = now
        
        day_name = target_now.strftime("%A")
        hour = target_now.hour
        
        if 5 <= hour < 12:
            time_of_day = "morning"
        elif 12 <= hour < 17:
            time_of_day = "afternoon"
        elif 17 <= hour < 21:
            time_of_day = "evening"
        else:
            time_of_day = "night"
        
        context_injection = f"\n\n---\n\n## CURRENT TIME\nIt is {day_name} {time_of_day} ({target_now.strftime('%I:%M %p')}) in their timezone.\n"
        
        # --- TIER 1 CONTEXT REPORT (if available) ---
        if analyst_report:
            context_injection += "\n## CONTEXT REPORT (Tier 1 Analyst, JSON)\n```json\n"
            context_injection += str(analyst_report).strip() + "\n"
            context_injection += "```\n"
        
        # --- EMOTIONAL CARRYOVER (ESV with decay) ---
        emotional_context = self._get_active_emotional_context(profile)
        if emotional_context:
            context_injection += f"\n{emotional_context}\n"
        
        # --- FILTERED JSON CONTEXT (LLM-relevant fields only) ---
        # Remove orchestrator-only fields to save tokens
        filtered_profile = self._filter_for_llm_context(profile)
        
        context_injection += "\n## CONTACT PROFILE (JSON)\n```json\n"
        context_injection += json.dumps(filtered_profile, indent=2, default=str)
        context_injection += "\n```\n"
        
        # --- RECENT MESSAGES (Raw, with timestamps) ---
        if recent_messages:
            # Compute a rough length target based on THEIR recent messages.
            their_lengths: list[int] = []
            context_injection += f"\n## RECENT CONVERSATION (Last {len(recent_messages)} Messages)\n"
            for msg in recent_messages:
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
                    sender = "ME" if s in {"you", "me", "myself", "operator"} else "THEY"
                elif "role" in msg:
                    r = str(msg.get("role") or "").strip().lower()
                    sender = "ME" if r in {"assistant", "me"} else "THEY"
                else:
                    sender = "ME" if _is_from_me(msg.get("is_from_me")) else "THEY"
                ts_str = msg.get('time_ago', 'Recently')
                text = msg.get('text', '')
                if sender == "THEY":
                    their_lengths.append(len(str(text or "").strip()))
                context_injection += f"[{ts_str}] {sender}: {text}\n"

            if their_lengths:
                avg = int(sum(their_lengths) / max(1, len(their_lengths)))
                # Give a small band so it doesn't feel mechanical.
                low = max(20, int(avg * 0.7))
                high = max(low + 10, int(avg * 1.2))
                context_injection += (
                    "\n## LENGTH MIRRORING\n"
                    f"Target length: ~{avg} characters. Aim for {low}–{high} characters unless context demands otherwise.\n"
                )
        
        context_injection += "\n---\nRespond naturally to the last message. Be brief. No reasoning output.\n"
        
        return {
            "system_instruction": base_prompt + context_injection,
            "contact": profile  # Pass full profile for potential fallback usage
        }

    def update_profile(self, handle: str, data: Dict):
        file_path = self._path_for(handle)
        atomic_write_json(file_path, data, indent=4)

    def store_interaction(self, handle: str, user_msg: str, agent_msg: str) -> Dict:
        """
        Updates profile stats (last interaction time).
        Note: We no longer store text in 'recent_interactions' inside JSON to avoid duplication 
        with the primary chat database (Bridge/Watcher).
        """
        import datetime
        profile = self.load_profile(handle)
        
        # Update last interaction epoch
        if "identity_matrix" in profile:
            profile["identity_matrix"]["last_interaction_epoch"] = datetime.datetime.now().timestamp()
        
        self.update_profile(handle, profile)
        return profile
