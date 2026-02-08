# Project Zero

Autonomous iMessage orchestration system with multi-tiered AI analysis.

## Prerequisites

- macOS (required for iMessage integration)
- Python 3.10+
- Node.js 18+
- Google Chrome
- Full Disk Access granted to Terminal/Python (System Settings → Privacy & Security)

## Installation

### 1. Clone the repository
```bash
git clone https://github.com/sherrymcadams001-afk/zero.git
cd zero
```

### 2. Install the LotL controller
The LotL (Living-off-the-Land) controller routes LLM requests through Google AI Studio via Chrome CDP, bypassing API quotas.

```bash
cd lotl
npm install
cd ..
```

### 3. Set up Python environment
```bash
cd imessage_orchestrator
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install streamlit
cd ..
```

### 4. Configure target handles
Edit `imessage_orchestrator/config/settings.py`:
- Set `TARGET_HANDLES` to the phone numbers/emails to monitor
- Set `OPERATOR_HANDLE` to your phone number for approvals

## Running

```bash
./start-orchestrator.sh
```

This will:
1. Launch Chrome with CDP enabled (port 9222)
2. Start the LotL controller (port 3000)
3. Wait for you to log into Google AI Studio
4. Start the Streamlit dashboard (port 8501)
5. Run the orchestrator

## Architecture

- **Orchestrator** (`imessage_orchestrator/orchestrator.py`): Main state machine handling message flow
- **AnalystService**: 5-tier intelligence pipeline (pre-response, facts, summary, trajectory, strategic)
- **Delegate**: LLM response generation
- **Bridge**: iMessage send via AppleScript
- **Watcher**: chat.db polling for new messages
- **LotL Client**: Routes requests through logged-in AI Studio session

## Dashboard

Access the control panel at http://localhost:8501 to:
- Edit contact profiles
- Enable/disable shadow mode (approval required)
- Mute contacts
- Trigger proactive messages
- Monitor system status
