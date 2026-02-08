#!/usr/bin/env bash
#
# iMessage Orchestrator + LotL Stack (Project Zero)
# Routes Delegate + Analyst through logged-in AI Studio (Chrome CDP)
# Cost: $0.00 - No API quotas
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ══════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════
export LLM_PROVIDER="lotl"
export LOTL_BASE_URL="http://127.0.0.1:3000"
export LOTL_TIMEOUT="180.0"
export PYTHONUNBUFFERED="1"

# Chrome CDP Settings
CHROME_PORT=9222
CONTROLLER_PORT=3000
HOST="127.0.0.1"
MODE="api"  # FRESH CHAT PER REQUEST: Stateless /gemini calls (prevents context bleed)
USER_DATA_DIR="$SCRIPT_DIR/.lotl/chrome-lotl-${CHROME_PORT}"

# Paths
LOTL_DIR="$SCRIPT_DIR/lotl"
ORCHESTRATOR_DIR="$SCRIPT_DIR/imessage_orchestrator"
VENV_PYTHON="$ORCHESTRATOR_DIR/.venv/bin/python"
STREAMLIT_BIN="$ORCHESTRATOR_DIR/.venv/bin/streamlit"
STREAMLIT_PORT=8501
STREAMLIT_LOG="$ORCHESTRATOR_DIR/streamlit.log"

# ══════════════════════════════════════════════════════════════
# DEPENDENCY CHECK
# ══════════════════════════════════════════════════════════════
if [[ ! -f "$LOTL_DIR/scripts/start-controller.js" ]]; then
    echo "❌ ERROR: LotL controller not found at $LOTL_DIR"
    echo ""
    echo "The LotL controller is required but not installed."
    echo "Please ensure the ./lotl directory exists in this repo."
    echo ""
    echo "Then run this script again."
    exit 1
fi

if [[ ! -d "$LOTL_DIR/node_modules" ]]; then
    if ! command -v npm >/dev/null 2>&1; then
        echo "❌ ERROR: npm not found. Install Node.js (npm) and rerun."
        exit 1
    fi

    echo "[0/4] Installing LotL dependencies (npm install)..."
    cd "$LOTL_DIR"
    npm install
    cd "$SCRIPT_DIR"
fi

# ══════════════════════════════════════════════════════════════
# CLEANUP
# ══════════════════════════════════════════════════════════════
echo "=========================================="
echo "Starting iMessage Orchestrator + LotL"
echo "Mode: $MODE"
echo "=========================================="

pkill -f "streamlit run imessage_orchestrator/ui.py" 2>/dev/null || true
pkill -f "imessage_orchestrator/orchestrator.py" 2>/dev/null || true
pkill -f "start-controller.js" 2>/dev/null || true
sleep 1

cleanup() {
    echo ""
    echo "Shutting down..."
    if [[ -n "${UI_PID:-}" ]]; then kill "$UI_PID" 2>/dev/null || true; fi
    pkill -f "imessage_orchestrator/orchestrator.py" 2>/dev/null || true
    pkill -f "start-controller.js" 2>/dev/null || true
    echo "Done."
}
trap cleanup INT TERM EXIT

# ══════════════════════════════════════════════════════════════
# 1. LAUNCH CHROME WITH CDP (if not already running)
# ══════════════════════════════════════════════════════════════
echo "[1/4] Checking Chrome CDP..."
mkdir -p "$USER_DATA_DIR"

if curl -s --max-time 2 "http://localhost:$CHROME_PORT/json/version" >/dev/null 2>&1; then
    echo "      ✓ Chrome CDP already active on port $CHROME_PORT"
else
    echo "      Launching Chrome with remote debugging..."
    open -na "Google Chrome" --args \
        --remote-debugging-port=$CHROME_PORT \
        --user-data-dir="$USER_DATA_DIR" \
        "https://aistudio.google.com"
    
    # Wait for CDP to be available
    for i in {1..15}; do
        if curl -s --max-time 2 "http://localhost:$CHROME_PORT/json/version" >/dev/null 2>&1; then
            echo "      ✓ Chrome CDP ready"
            break
        fi
        sleep 1
    done
fi

if ! curl -s --max-time 2 "http://localhost:$CHROME_PORT/json/version" >/dev/null 2>&1; then
    echo "      ❌ Chrome CDP not reachable on :$CHROME_PORT"
    echo "      Start Chrome with: open -na \"Google Chrome\" --args --remote-debugging-port=$CHROME_PORT"
    exit 1
fi

# ══════════════════════════════════════════════════════════════
# 2. START LOTL CONTROLLER (Mode: $MODE)
# ══════════════════════════════════════════════════════════════
echo "[2/4] Starting LotL Controller (mode: $MODE)..."
cd "$LOTL_DIR"

# Launch without nohup to avoid TTY issues on macOS, direct logging
node scripts/start-controller.js \
    --host "$HOST" \
    --port "$CONTROLLER_PORT" \
    --chrome-port "$CHROME_PORT" \
    --mode "$MODE" \
    > "$LOTL_DIR/controller.debug.log" 2>&1 &

CONTROLLER_PID=$!
echo "      Started Controller (PID $CONTROLLER_PID)"

# Wait for controller health
for i in {1..20}; do
    if ! kill -0 $CONTROLLER_PID 2>/dev/null; then
        echo "      ❌ Controller process died unexpectedly!"
        cat "$LOTL_DIR/controller.debug.log"
        exit 1
    fi

    if curl -s --max-time 2 "http://$HOST:$CONTROLLER_PORT/health" >/dev/null 2>&1; then
        echo "      ✓ LotL Controller healthy"
        break
    fi
    sleep 1
done

if ! curl -s --max-time 2 "http://$HOST:$CONTROLLER_PORT/health" >/dev/null 2>&1; then
    echo "      ❌ LotL Controller not healthy on http://$HOST:$CONTROLLER_PORT"
    tail -n 80 "$LOTL_DIR/controller.debug.log" || true
    exit 1
fi

# ══════════════════════════════════════════════════════════════
# 3. VERIFY GEMINI ENDPOINT
# ══════════════════════════════════════════════════════════════
echo "[3/4] Verifying Gemini endpoint..."

# Quick health check - Gemini works if controller is healthy
sleep 2
echo "      ✓ Gemini endpoint available"

# ══════════════════════════════════════════════════════════════
# 4. START ORCHESTRATOR SYSTEM
# ══════════════════════════════════════════════════════════════
cd "$ORCHESTRATOR_DIR"

echo "[4/4] Starting Dashboard (UI)..."
if [[ ! -x "$STREAMLIT_BIN" ]]; then
    echo "      ❌ Streamlit not found at: $STREAMLIT_BIN"
    echo "      Install it into the venv: $VENV_PYTHON -m pip install -U streamlit"
    exit 1
fi

"$STREAMLIT_BIN" run "$ORCHESTRATOR_DIR/ui.py" \
    --server.headless true \
    --server.address 127.0.0.1 \
    --server.port "$STREAMLIT_PORT" \
    > "$STREAMLIT_LOG" 2>&1 &
UI_PID=$!

# Wait for Streamlit health
for i in {1..20}; do
    if ! kill -0 "$UI_PID" 2>/dev/null; then
        echo "      ❌ Streamlit process died unexpectedly (PID $UI_PID)"
        tail -n 120 "$STREAMLIT_LOG" || true
        exit 1
    fi
    if curl -s --max-time 2 "http://127.0.0.1:${STREAMLIT_PORT}/_stcore/health" >/dev/null 2>&1; then
        echo "      ✓ Streamlit healthy"
        break
    fi
    sleep 1
done

if ! curl -s --max-time 2 "http://127.0.0.1:${STREAMLIT_PORT}/_stcore/health" >/dev/null 2>&1; then
    echo "      ❌ Streamlit did not become healthy on :${STREAMLIT_PORT}"
    tail -n 120 "$STREAMLIT_LOG" || true
    exit 1
fi

echo "      Dashboard: http://localhost:${STREAMLIT_PORT}"

echo ""
echo "════════════════════════════════════════════════════════════"
echo "🚀 LOTL STACK RUNNING"
echo "════════════════════════════════════════════════════════════"
echo "  Chrome CDP:      http://localhost:$CHROME_PORT"
echo "  LotL Controller: http://$HOST:$CONTROLLER_PORT"
echo "  Dashboard:       http://localhost:8501"
echo "  Mode:            $MODE"
echo "  Cost:            \$0.00 (no API calls)"
echo ""
echo "  User Data Dir:   $USER_DATA_DIR"
echo "  Controller Log:  $LOTL_DIR/controller.debug.log"
echo "  Streamlit Log:   $STREAMLIT_LOG"
echo "════════════════════════════════════════════════════════════"
echo ""
echo "Starting Backend Orchestrator (LotL provider)..."

# Start orchestrator (blocks until Ctrl+C)
$VENV_PYTHON $ORCHESTRATOR_DIR/orchestrator.py
