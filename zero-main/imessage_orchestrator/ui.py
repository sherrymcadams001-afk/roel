"""
Project Zero - Operations Console
Control-first UI for WhatsApp agent orchestration.
"""
import streamlit as st
import json
import re
from pathlib import Path
import os
import urllib.request
import urllib.error
from collections import deque
from datetime import datetime

# Allow running via `streamlit run <path>/ui.py` from any CWD.
import sys
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from services.bridge import iMessageBridge
from services.whatsapp_bridge import WhatsAppBridge
from services.archivist import Archivist
from config import settings

# Configuration Paths
BASE_DIR = Path(__file__).parent
PROMPTS_FILE = BASE_DIR / "config" / "prompts.py"
CONTACTS_DIR = BASE_DIR / "data" / "contacts"
UNVERIFIED_DIR = CONTACTS_DIR / "_unverified"
LOG_FILE = BASE_DIR / "data" / "logs" / "imessage_orchestrator.log"
APPROVALS_FILE = BASE_DIR / "data" / "pending_approvals.json"

st.set_page_config(page_title="P0 Ops Console", layout="wide", page_icon="🎯")

def load_prompts():
    """Extracts prompt variables from the python file using regex."""
    if not PROMPTS_FILE.exists():
        return "", ""
    
    content = PROMPTS_FILE.read_text(encoding="utf-8")
    
    # Simple regex (dotall)
    persona_match = re.search(r'GLOBAL_PERSONA_SYSTEM_PROMPT\s*=\s*"""(.*?)"""', content, re.DOTALL)
    analyst_match = re.search(r'ANALYST_SYSTEM_PROMPT\s*=\s*"""(.*?)"""', content, re.DOTALL)
    
    persona = persona_match.group(1).strip() if persona_match else ""
    analyst = analyst_match.group(1).strip() if analyst_match else ""
    
    return persona, analyst


def _to_triple_quoted_body(text: str) -> str:
    """Return text safe to embed inside a Python triple-quoted string."""
    t = str(text or "")
    # Prevent accidental termination of the literal.
    return t.replace('"""', r'\"\"\"')

def save_prompts(persona_text, analyst_text):
    """Update prompt bodies in-place without deleting other exports."""

    persona_body = _to_triple_quoted_body(persona_text)
    analyst_body = _to_triple_quoted_body(analyst_text)

    # If prompts.py doesn't exist, fall back to creating a minimal safe file.
    if not PROMPTS_FILE.exists():
        new_content = (
            "# Core Identity: Project Zero (P0)\n"
            "# This prompt defines the Agent's personality, goals, and speaking style.\n"
            "# The Archivist will append specific Target Context (Name, Phase, etc.) to this prompt.\n\n"
            f"GLOBAL_PERSONA_SYSTEM_PROMPT = \"\"\"{persona_body}\"\"\"\n\n"
            "# Analyst: Updates the psychological profile based on interaction\n"
            "# This prompt instructs the Analyst Loop on how to read the conversation and update the JSON state.\n\n"
            f"ANALYST_SYSTEM_PROMPT = \"\"\"{analyst_body}\"\"\"\n\n"
            "# Deprecated: Merged into GLOBAL_PERSONA_SYSTEM_PROMPT\n"
            "TEXTING_STYLE_GUIDE = \"\"\n"
        )
        PROMPTS_FILE.write_text(new_content, encoding="utf-8")
        return

    content = PROMPTS_FILE.read_text(encoding="utf-8")

    def _replace_prompt(src: str, var_name: str, new_body: str) -> str:
        pattern = rf'({var_name}\s*=\s*"""\s*)(.*?)(\s*"""\s*)'
        repl = rf'\1{new_body}\3'
        out, n = re.subn(pattern, repl, src, flags=re.DOTALL)
        if n == 0:
            # Append if missing.
            out = out.rstrip() + f"\n\n{var_name} = \"\"\"{new_body}\"\"\"\n"
        return out

    updated = content
    updated = _replace_prompt(updated, "GLOBAL_PERSONA_SYSTEM_PROMPT", persona_body)
    updated = _replace_prompt(updated, "ANALYST_SYSTEM_PROMPT", analyst_body)

    PROMPTS_FILE.write_text(updated, encoding="utf-8")

def get_profile_files():
    if not CONTACTS_DIR.exists():
        CONTACTS_DIR.mkdir(parents=True, exist_ok=True)
    return sorted([f.name for f in CONTACTS_DIR.glob("*.json")])

def load_profile(filename):
    path = CONTACTS_DIR / filename
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data
    except Exception as e:
        st.error(f"Error loading {filename}: {e}")
        return {}

def save_profile(filename, data):
    path = CONTACTS_DIR / filename
    try:
        path.write_text(json.dumps(data, indent=4, ensure_ascii=False), encoding="utf-8")
        st.success(f"Saved {filename}")
    except Exception as e:
        st.error(f"Error saving {filename}: {e}")


def _safe_read_json(path: Path, default):
    try:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _tail_text_file(path: Path, max_lines: int = 200) -> str:
    if not path.exists():
        return ""
    try:
        dq: deque[str] = deque(maxlen=max_lines)
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                dq.append(line.rstrip("\n"))
        return "\n".join(dq)
    except Exception:
        return ""


def _http_get_json(url: str, timeout_sec: float = 3.0):
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return True, json.loads(raw)
            except Exception:
                return True, {"raw": raw}
    except urllib.error.URLError as e:
        return False, {"error": str(e)}
    except TimeoutError as e:
        return False, {"error": f"timeout: {e}"}
    except Exception as e:
        return False, {"error": str(e)}


def _http_post_json(url: str, payload: dict, timeout_sec: float = 120.0):
    """POST JSON to url, return (ok, response_dict)."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return True, json.loads(raw)
            except Exception:
                return True, {"raw": raw}
    except urllib.error.URLError as e:
        return False, {"error": str(e)}
    except Exception as e:
        return False, {"error": str(e)}


def _save_approvals(data: dict):
    """Persist pending_approvals.json"""
    try:
        APPROVALS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


def _get_unverified_contacts() -> list[dict]:
    """Return list of unknown/unverified contact profiles."""
    if not UNVERIFIED_DIR.exists():
        return []
    results = []
    for p in UNVERIFIED_DIR.glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            data["_filename"] = p.name
            results.append(data)
        except Exception:
            pass
    return results


def _delete_unverified_contact(filename: str) -> bool:
    """Remove an unverified contact file."""
    try:
        (UNVERIFIED_DIR / filename).unlink(missing_ok=True)
        return True
    except Exception:
        return False


def _promote_to_allowlist(filename: str, profile_data: dict) -> bool:
    """Move unverified contact to verified allowlist."""
    try:
        CONTACTS_DIR.mkdir(parents=True, exist_ok=True)
        target = CONTACTS_DIR / filename
        profile_data.pop("_filename", None)
        profile_data["requires_approval"] = False
        profile_data["mute_agent"] = False
        target.write_text(json.dumps(profile_data, indent=4), encoding="utf-8")
        (UNVERIFIED_DIR / filename).unlink(missing_ok=True)
        return True
    except Exception:
        return False


# --- UI LAYOUT ---

st.title("🎯 Project Zero | Operations Console")

# ============== TOP METRICS BAR ==============
lotl_base_url = os.getenv("LOTL_BASE_URL", settings.LOTL_BASE_URL).rstrip("/")
ok_h, health = _http_get_json(f"{lotl_base_url}/health")
ok_r, ready = _http_get_json(f"{lotl_base_url}/ready", timeout_sec=5.0)
lotl_ok = ok_h and ok_r and ready.get("ok", False)

approvals = _safe_read_json(APPROVALS_FILE, {})
contacts = get_profile_files()
unknowns = _get_unverified_contacts()

m1, m2, m3, m4, m5 = st.columns(5)
with m1:
    if lotl_ok:
        st.success(f"🟢 LotL")
    else:
        st.error(f"🔴 LotL")
with m2:
    st.metric("⏳ Pending", len(approvals))
with m3:
    st.metric("📇 Contacts", len(contacts))
with m4:
    st.metric("❓ Unknown", len(unknowns))
with m5:
    st.caption(f"Provider: **{settings.LLM_PROVIDER}**")

st.markdown("---")


def _is_e164_phone(handle: str) -> bool:
    # E.164: + followed by 7-15 digits
    return bool(re.fullmatch(r"\+[1-9]\d{6,14}", (handle or "").strip()))


def _handle_to_filename(handle: str) -> str:
    h = (handle or "").strip()
    if not h:
        return ""
    return f"{h}.json" if not h.endswith(".json") else h


# ============== MAIN TABS (CONTROL-FIRST) ==============
tab_approvals, tab_contacts, tab_send, tab_logs, tab_config = st.tabs([
    "📋 Approval Queue",
    "📇 Contacts",
    "📤 Send Message",
    "📜 Logs",
    "⚙️ Config"
])

# ============== TAB 1: APPROVAL QUEUE (PRIMARY) ==============
with tab_approvals:
    st.header("Pending Approvals")
    st.caption("Messages requiring human approval before sending.")
    
    if st.button("🔄 Refresh Queue"):
        st.rerun()
    
    if not approvals:
        st.info("✅ No pending approvals. Queue is clear.")
    else:
        for handle, item in list(approvals.items()):
            with st.container(border=True):
                col_info, col_actions = st.columns([3, 1])
                
                with col_info:
                    st.markdown(f"### {handle}")
                    st.caption(f"⏰ {item.get('timestamp', 'N/A')} | Trigger: {item.get('trigger', 'inbound')}")
                    
                    # Show classification if from unknown contact triage
                    if "classification" in item:
                        cls = item["classification"]
                        if cls in {"spam", "scam"}:
                            st.error(f"⚠️ Classification: **{cls.upper()}**")
                        else:
                            st.warning(f"Classification: {cls}")
                    
                    if "risk_flags" in item:
                        st.caption(f"Risk: {', '.join(item['risk_flags']) or 'None'}")
                
                draft = str(item.get("draft", ""))
                edited_draft = st.text_area(
                    "Draft Message",
                    value=draft,
                    height=100,
                    key=f"draft_{handle}",
                    label_visibility="collapsed"
                )
                
                c1, c2, c3 = st.columns(3)
                
                with c1:
                    if st.button("✅ Approve & Send", key=f"approve_{handle}", type="primary"):
                        if settings.ENABLE_WHATSAPP and not settings.ENABLE_IMESSAGE:
                            bridge = WhatsAppBridge()
                        else:
                            bridge = iMessageBridge()
                        
                        success = bridge.send_message(handle, edited_draft)
                        if success:
                            st.success(f"✅ Sent to {handle}")
                            
                            # Promote to allowlist
                            try:
                                settings.CONTACTS_DIR.mkdir(parents=True, exist_ok=True)
                                profile_path = settings.CONTACTS_DIR / f"{handle}.json"
                                archivist = Archivist(contacts_dir=settings.CONTACTS_DIR)
                                
                                if not profile_path.exists():
                                    profile = archivist.load_profile(handle)
                                    if settings.ENABLE_WHATSAPP and not settings.ENABLE_IMESSAGE:
                                        profile.setdefault("identity_matrix", {})["service"] = "WhatsApp"
                                    profile["requires_approval"] = False
                                    profile["mute_agent"] = False
                                    archivist.update_profile(handle, profile)
                                
                                archivist.store_interaction(handle, "(Approved)", edited_draft)
                                
                                # Clean up unverified if exists
                                _delete_unverified_contact(f"{handle}.json")
                            except Exception as e:
                                st.warning(f"Sent but profile update failed: {e}")
                        else:
                            st.error(f"❌ Send failed for {handle}")
                        
                        del approvals[handle]
                        _save_approvals(approvals)
                        st.rerun()
                
                with c2:
                    if st.button("❌ Deny", key=f"deny_{handle}"):
                        del approvals[handle]
                        _save_approvals(approvals)
                        st.warning(f"Denied {handle}")
                        st.rerun()
                
                with c3:
                    if st.button("🚫 Block", key=f"block_{handle}", type="secondary"):
                        # Remove from queue and delete any unverified profile
                        del approvals[handle]
                        _save_approvals(approvals)
                        _delete_unverified_contact(f"{handle}.json")
                        st.error(f"Blocked {handle}")
                        st.rerun()


# ============== TAB 2: CONTACTS ==============
with tab_contacts:
    st.header("Contact Management")
    
    col_verified, col_unknown = st.columns(2)
    
    with col_verified:
        st.subheader("✅ Verified Contacts")
        st.caption("Allowlisted handles - agent can auto-reply.")
        
        # Add new contact
        with st.expander("➕ Add New Contact"):
            new_handle = st.text_input("Phone (E.164)", placeholder="+15551234567", key="add_handle")
            new_name = st.text_input("Name", placeholder="John Doe", key="add_name")
            if st.button("Add Contact", type="primary", key="add_btn"):
                handle_clean = (new_handle or "").strip()
                if not _is_e164_phone(handle_clean):
                    st.error("Must be E.164 format: +15551234567")
                else:
                    fname = _handle_to_filename(handle_clean)
                    if (CONTACTS_DIR / fname).exists():
                        st.warning("Contact already exists")
                    else:
                        save_profile(fname, {
                            "identity_matrix": {
                                "handle": handle_clean,
                                "name": (new_name or "Unknown").strip(),
                                "city": "Unknown",
                                "timezone": "America/New_York",
                            },
                            "psychometric_profile": {},
                            "operational_state": {"current_phase": "Calibration"},
                            "pacing_engine": {
                                "average_latency_seconds": 60.0,
                                "variable_reward_ratio": 0.5,
                                "initiation_enabled": False,
                            },
                            "mute_agent": False,
                            "requires_approval": False,
                        })
                        st.success(f"Added {handle_clean}")
                        st.rerun()
        
        # List contacts with quick toggles
        for f in contacts:
            with st.container(border=True):
                data = load_profile(f)
                identity = data.get("identity_matrix", {})
                handle = identity.get("handle", Path(f).stem)
                name = identity.get("name", "Unknown")
                
                c1, c2, c3, c4 = st.columns([2, 1, 1, 1])
                
                with c1:
                    st.markdown(f"**{name}**")
                    st.caption(handle)
                
                with c2:
                    muted = data.get("mute_agent", False)
                    if st.toggle("🔇 Mute", value=muted, key=f"mute_{f}"):
                        if not muted:
                            data["mute_agent"] = True
                            save_profile(f, data)
                    elif muted:
                        data["mute_agent"] = False
                        save_profile(f, data)
                
                with c3:
                    shadow = data.get("requires_approval", False)
                    if st.toggle("👁️ Shadow", value=shadow, key=f"shadow_{f}"):
                        if not shadow:
                            data["requires_approval"] = True
                            save_profile(f, data)
                    elif shadow:
                        data["requires_approval"] = False
                        save_profile(f, data)
                
                with c4:
                    proactive = data.get("pacing_engine", {}).get("initiation_enabled", False)
                    if st.toggle("🚀 Proactive", value=proactive, key=f"proactive_{f}"):
                        if not proactive:
                            data.setdefault("pacing_engine", {})["initiation_enabled"] = True
                            save_profile(f, data)
                    elif proactive:
                        data.setdefault("pacing_engine", {})["initiation_enabled"] = False
                        save_profile(f, data)
    
    with col_unknown:
        st.subheader("❓ Unknown Contacts")
        st.caption("Unverified senders awaiting review.")
        
        if not unknowns:
            st.info("No unknown contacts pending.")
        else:
            for u in unknowns:
                with st.container(border=True):
                    identity = u.get("identity_matrix", {})
                    handle = identity.get("handle", "Unknown")
                    fname = u.get("_filename", "")
                    
                    st.markdown(f"**{handle}**")
                    if "first_seen" in u:
                        st.caption(f"First seen: {u['first_seen']}")
                    
                    c1, c2 = st.columns(2)
                    with c1:
                        if st.button("✅ Promote", key=f"promote_{fname}", type="primary"):
                            if _promote_to_allowlist(fname, u):
                                st.success(f"Promoted {handle} to allowlist")
                                st.rerun()
                            else:
                                st.error("Promotion failed")
                    
                    with c2:
                        if st.button("🗑️ Delete", key=f"delete_{fname}"):
                            if _delete_unverified_contact(fname):
                                st.warning(f"Deleted {handle}")
                                st.rerun()


# ============== TAB 3: SEND MESSAGE ==============
with tab_send:
    st.header("Manual Message Send")
    
    col_wa, col_lotl = st.columns(2)
    
    with col_wa:
        st.subheader("📱 WhatsApp Direct")
        st.caption("Send a message via WhatsApp bridge.")
        
        wa_handle = st.text_input("Recipient (E.164)", placeholder="+15551234567", key="wa_handle")
        wa_message = st.text_area("Message", height=150, key="wa_msg", placeholder="Type your message...")
        
        if st.button("📤 Send WhatsApp", type="primary", key="wa_send"):
            if not wa_handle or not wa_message:
                st.warning("Enter recipient and message")
            else:
                bridge = WhatsAppBridge()
                if bridge.send_message(wa_handle.strip(), wa_message.strip()):
                    st.success(f"✅ Sent to {wa_handle}")
                else:
                    st.error("❌ Send failed")
    
    with col_lotl:
        st.subheader("🤖 LotL Direct")
        st.caption("Send a prompt directly to the AI backend.")
        
        lotl_prompt = st.text_area("Prompt", height=150, key="lotl_prompt", placeholder="Enter prompt...")
        lotl_timeout = st.number_input("Timeout (s)", min_value=30, max_value=300, value=120, key="lotl_timeout")
        
        if st.button("🚀 Send to LotL", type="primary", key="lotl_send", disabled=not lotl_ok):
            if lotl_prompt.strip():
                with st.spinner("Waiting for response..."):
                    ok, resp = _http_post_json(
                        f"{lotl_base_url}/aistudio",
                        {"prompt": lotl_prompt.strip()},
                        timeout_sec=float(lotl_timeout),
                    )
                if ok and resp.get("ok"):
                    st.success("Response received")
                    st.text_area("Response", value=resp.get("response", ""), height=200, key="lotl_resp")
                else:
                    st.error("Request failed")
                    st.json(resp)
            else:
                st.warning("Enter a prompt")


# ============== TAB 4: LOGS ==============
with tab_logs:
    st.header("System Logs")
    
    col_ctrl, col_filter = st.columns([1, 3])
    with col_ctrl:
        if st.button("🔄 Refresh Logs"):
            st.rerun()
        log_lines = st.number_input("Lines to show", min_value=50, max_value=500, value=150)
    
    with col_filter:
        log_filter = st.text_input("Filter (regex)", placeholder="ERROR|WARNING|handle")
    
    log_tail = _tail_text_file(LOG_FILE, max_lines=int(log_lines))
    
    if log_filter:
        try:
            pattern = re.compile(log_filter, re.IGNORECASE)
            filtered_lines = [line for line in log_tail.split("\n") if pattern.search(line)]
            log_tail = "\n".join(filtered_lines)
        except re.error:
            st.warning("Invalid regex pattern")
    
    if not log_tail:
        st.info(f"No log content at {LOG_FILE}")
    else:
        st.code(log_tail, language="text")


# ============== TAB 5: CONFIG ==============
with tab_config:
    st.header("Agent Identity & Voice")
    st.info("These settings act as the fallback personality when specific profile context is missing, and define how the Analyst interprets behavior.")
    
    current_persona, current_analyst = load_prompts()
    
    col1, col2 = st.columns(2)
    
    with col1:
        st.subheader("Global Persona (Delegate)")
        persona_input = st.text_area(
            "Define the agent's core identity, goals, and constraints.", 
            value=current_persona, 
            height=400,
            key="persona_edit"
        )
        
    with col2:
        st.subheader("Analyst Logic (Profile Updates)")
        analyst_input = st.text_area(
            "Define how the agent analyzes interactions to update profiles.",
            value=current_analyst,
            height=400,
            key="analyst_edit"
        )
    
    if st.button("💾 Save System Configuration", type="primary", key="save_config"):
        save_prompts(persona_input, analyst_input)
        st.success("Configuration updated successfully.")
    
    st.divider()
    
    # Runtime settings display
    st.subheader("Runtime Settings")
    st.json({
        "LLM_PROVIDER": settings.LLM_PROVIDER,
        "ENABLE_IMESSAGE": settings.ENABLE_IMESSAGE,
        "ENABLE_WHATSAPP": settings.ENABLE_WHATSAPP,
        "LOTL_BASE_URL": settings.LOTL_BASE_URL,
        "TARGET_HANDLES_COUNT": len(settings.get_target_handles()),
        "AUTO_REPLY_UNKNOWN_CONTACTS": settings.AUTO_REPLY_UNKNOWN_CONTACTS,
    })
    
    with st.expander("LotL Details"):
        st.json({"health": health, "ready": ready})

