"""Tests for the audit fixes: atomic writes, proactive ordering, and audit logging."""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure the orchestrator package is on sys.path
_ORCH_ROOT = Path(__file__).resolve().parents[1]
if str(_ORCH_ROOT) not in sys.path:
    sys.path.insert(0, str(_ORCH_ROOT))


# ---------------------------------------------------------------------------
# Module 3: atomic_write_json
# ---------------------------------------------------------------------------

class TestAtomicWriteJson:
    """Verify utils.atomic.atomic_write_json guarantees."""

    def test_basic_write(self, tmp_path: Path) -> None:
        from utils.atomic import atomic_write_json

        target = tmp_path / "test.json"
        data = {"key": "value", "nested": [1, 2, 3]}
        atomic_write_json(target, data)

        assert target.exists()
        loaded = json.loads(target.read_text(encoding="utf-8"))
        assert loaded == data

    def test_overwrite_existing(self, tmp_path: Path) -> None:
        from utils.atomic import atomic_write_json

        target = tmp_path / "test.json"
        atomic_write_json(target, {"old": True})
        atomic_write_json(target, {"new": True})

        loaded = json.loads(target.read_text(encoding="utf-8"))
        assert loaded == {"new": True}

    def test_no_orphan_tmp_on_success(self, tmp_path: Path) -> None:
        from utils.atomic import atomic_write_json

        target = tmp_path / "test.json"
        atomic_write_json(target, {"ok": True})

        tmp_file = target.with_suffix(".json.tmp")
        assert not tmp_file.exists()

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        from utils.atomic import atomic_write_json

        target = tmp_path / "sub" / "deep" / "test.json"
        atomic_write_json(target, {"created": True})
        assert target.exists()


# ---------------------------------------------------------------------------
# Module 3: watcher.py uses atomic_write_json for save_state
# ---------------------------------------------------------------------------

class TestWatcherSaveState:
    """Verify watcher.save_state uses atomic_write_json."""

    def test_save_state_uses_atomic(self, tmp_path: Path) -> None:
        """save_state should delegate to atomic_write_json (not raw json.dump)."""
        with patch("services.watcher.atomic_write_json") as mock_atomic:
            from services.watcher import iMessageWatcher

            state_file = tmp_path / "state.json"
            w = iMessageWatcher.__new__(iMessageWatcher)
            w.state_file = state_file
            w._state = {"last_message_rowid": 42}

            w.save_state()

            mock_atomic.assert_called_once_with(state_file, {"last_message_rowid": 42})


# ---------------------------------------------------------------------------
# Module 7: blocked-message audit logging
# ---------------------------------------------------------------------------

class TestBlockedMessageAudit:
    """Verify _require_safe_reply writes to the audit logger on leak detection."""

    @contextmanager
    def _make_orchestrator(self, tmp_path: Path):
        """Create a minimal Orchestrator with mocked dependencies.

        Yields the orchestrator while patches are active so that
        ``settings`` remains mocked during assertions.
        """
        with patch("orchestrator.AnalystService"), \
             patch("orchestrator.LotLClient"), \
             patch("orchestrator.SendQueue"), \
             patch("orchestrator.Archivist") as MockArchivist, \
             patch("orchestrator.iMessageBridge") as MockBridge, \
             patch("orchestrator.WhatsAppBridge"), \
             patch("orchestrator.settings") as mock_settings:

            mock_archivist = MockArchivist.return_value
            mock_archivist.load_profile.return_value = {
                "identity_matrix": {"handle": "+1234567890"},
                "pacing_engine": {},
            }

            mock_settings.STATE_FILE = tmp_path / "state.json"
            mock_settings.SEND_QUEUE_FILE = tmp_path / "send_queue.json"
            mock_settings.OPERATOR_HANDLE = None
            mock_settings.LLM_PROVIDER = "lotl"
            mock_settings.LOTL_BASE_URL = "http://localhost:3000"
            mock_settings.ENABLE_IMESSAGE = False
            mock_settings.ENABLE_WHATSAPP = False

            from orchestrator import Orchestrator

            orch = Orchestrator.__new__(Orchestrator)
            orch.archivist = mock_archivist
            orch.bridge = MockBridge.return_value
            orch.bridges = {"iMessage": MockBridge.return_value}
            orch.delegate = MagicMock()
            orch._llm_lock = threading.Lock()
            orch.pending_approvals = {}
            orch.analyst = MagicMock()

            yield orch

    def test_audit_log_on_analyst_leak(self, tmp_path: Path) -> None:
        """When _require_safe_reply detects analyst leak, audit logger records it."""
        with self._make_orchestrator(tmp_path) as orch:
            # Set up an audit logger with a handler we can inspect
            audit_logger = logging.getLogger("orchestrator.audit")
            audit_logger.handlers.clear()
            audit_logger.propagate = False
            handler = logging.handlers.MemoryHandler(capacity=100)
            audit_logger.addHandler(handler)
            audit_logger.setLevel(logging.DEBUG)

            leaked_text = "⏰ TIME CHECK: morning\n📊 DYNAMICS: high engagement"

            with pytest.raises(ValueError, match="leakage detected"):
                orch._require_safe_reply("+1234567890", leaked_text, _regen_attempt=1)

            # Check that the audit handler received a record
            handler.flush()
            assert len(handler.buffer) > 0
            record = handler.buffer[0]
            msg = record.getMessage()
            assert "BLOCKED" in msg
            assert "+1234567890" in msg
            # Newlines should be escaped (no raw newlines in the log line)
            assert "\n" not in msg

            audit_logger.removeHandler(handler)

    def test_safe_reply_passes_clean_text(self, tmp_path: Path) -> None:
        """Clean text should pass through _require_safe_reply unchanged."""
        with self._make_orchestrator(tmp_path) as orch:
            result = orch._require_safe_reply("+1234567890", "Hey, how's it going?")
            assert result == "Hey, how's it going?"


# ---------------------------------------------------------------------------
# Module 1: proactive initiation ordering
# ---------------------------------------------------------------------------

class TestProactiveOrdering:
    """Verify proactive initiation runs before sleep in main loop."""

    def test_proactive_before_sleep(self) -> None:
        """_check_proactive_initiation must be called before time.sleep in _run_main_loop."""
        import ast
        import inspect
        from orchestrator import _run_main_loop

        source = inspect.getsource(_run_main_loop)
        tree = ast.parse(source)

        # Walk the AST to find call positions for the two operations.
        proactive_line = None
        sleep_line = None

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                # bot._check_proactive_initiation()
                if isinstance(node.func, ast.Attribute) and node.func.attr == "_check_proactive_initiation":
                    proactive_line = node.lineno
                # time.sleep(settings.POLL_INTERVAL_SECONDS)
                if (isinstance(node.func, ast.Attribute)
                        and node.func.attr == "sleep"
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id == "time"
                        and node.args
                        and isinstance(node.args[0], ast.Attribute)
                        and node.args[0].attr == "POLL_INTERVAL_SECONDS"):
                    sleep_line = node.lineno

        assert proactive_line is not None, "_check_proactive_initiation not found in AST"
        assert sleep_line is not None, "time.sleep(settings.POLL_INTERVAL_SECONDS) not found in AST"
        assert proactive_line < sleep_line, (
            f"_check_proactive_initiation (line {proactive_line}) should appear before "
            f"time.sleep(POLL_INTERVAL_SECONDS) (line {sleep_line}) in the main loop"
        )


# ---------------------------------------------------------------------------
# Module 4: LotL client error classification
# ---------------------------------------------------------------------------

class TestLotLErrorClassification:
    """Verify LotLClient fails fast on non-recoverable errors."""

    def test_captcha_error_fails_fast(self) -> None:
        """CAPTCHA errors should not be retried — fail immediately."""
        from services.lotl_client import LotLClient

        client = LotLClient(base_url="http://localhost:9999", timeout=5)

        # Mock httpx.Client to return a "captcha" error on first attempt
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {
            "success": False,
            "error": "CAPTCHA verification required",
        }

        call_count = 0

        def mock_post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return mock_response

        with patch("httpx.Client") as MockClient:
            mock_ctx = MagicMock()
            mock_ctx.post = mock_post
            MockClient.return_value.__enter__ = MagicMock(return_value=mock_ctx)
            MockClient.return_value.__exit__ = MagicMock(return_value=False)

            with pytest.raises(RuntimeError, match="(?i)captcha"):
                client.chat("test prompt")

        # Should have been called only once (fail-fast, no retries)
        assert call_count == 1, f"Expected 1 attempt (fail-fast), got {call_count}"

    def test_auth_error_fails_fast(self) -> None:
        """Sign-in / auth errors should not be retried."""
        from services.lotl_client import LotLClient

        client = LotLClient(base_url="http://localhost:9999", timeout=5)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {
            "success": False,
            "error": "verify it's you - sign in required",
        }

        call_count = 0

        def mock_post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return mock_response

        with patch("httpx.Client") as MockClient:
            mock_ctx = MagicMock()
            mock_ctx.post = mock_post
            MockClient.return_value.__enter__ = MagicMock(return_value=mock_ctx)
            MockClient.return_value.__exit__ = MagicMock(return_value=False)

            with pytest.raises(RuntimeError):
                client.chat("test prompt")

        assert call_count == 1, f"Expected 1 attempt (fail-fast), got {call_count}"

    def test_busy_error_retries(self) -> None:
        """Busy/rate-limit errors should be retried."""
        from services.lotl_client import LotLClient

        client = LotLClient(base_url="http://localhost:9999", timeout=5)

        call_count = 0

        def mock_post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.raise_for_status.return_value = None
            mock_response.json.return_value = {
                "success": False,
                "error": "LotL Server Busy",
                "busy": True,
            }
            return mock_response

        with patch("httpx.Client") as MockClient:
            mock_ctx = MagicMock()
            mock_ctx.post = mock_post
            MockClient.return_value.__enter__ = MagicMock(return_value=mock_ctx)
            MockClient.return_value.__exit__ = MagicMock(return_value=False)

            with patch("time.sleep"):  # Skip actual delays
                with pytest.raises(RuntimeError):
                    client.chat("test prompt")

        # Should have retried all 5 attempts for a busy error
        assert call_count == 5, f"Expected 5 attempts (retries), got {call_count}"
