"""Tests for the audit fixes: atomic writes, proactive ordering, and audit logging."""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
import tempfile
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

    def _make_orchestrator(self):
        """Create a minimal Orchestrator with mocked dependencies."""
        with patch("orchestrator.AnalystService"), \
             patch("orchestrator.LotLClient"), \
             patch("orchestrator.SendQueue"), \
             patch("orchestrator.Archivist") as MockArchivist, \
             patch("orchestrator.iMessageBridge") as MockBridge, \
             patch("orchestrator.WhatsAppBridge"):

            mock_archivist = MockArchivist.return_value
            mock_archivist.load_profile.return_value = {
                "identity_matrix": {"handle": "+1234567890"},
                "pacing_engine": {},
            }

            from orchestrator import Orchestrator

            # Patch settings to avoid side effects
            with patch("orchestrator.settings") as mock_settings:
                mock_settings.STATE_FILE = Path(tempfile.mkdtemp()) / "state.json"
                mock_settings.SEND_QUEUE_FILE = mock_settings.STATE_FILE.parent / "send_queue.json"
                mock_settings.OPERATOR_HANDLE = None
                mock_settings.LLM_PROVIDER = "lotl"
                mock_settings.LOTL_BASE_URL = "http://localhost:3000"
                mock_settings.ENABLE_IMESSAGE = False
                mock_settings.ENABLE_WHATSAPP = False

                orch = Orchestrator.__new__(Orchestrator)
                orch.archivist = mock_archivist
                orch.bridge = MockBridge.return_value
                orch.bridges = {"iMessage": MockBridge.return_value}
                orch.delegate = MagicMock()
                orch._llm_lock = __import__("threading").Lock()
                orch.pending_approvals = {}
                orch.analyst = MagicMock()

                return orch

    def test_audit_log_on_analyst_leak(self) -> None:
        """When _require_safe_reply detects analyst leak, audit logger records it."""
        orch = self._make_orchestrator()

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
        assert "BLOCKED" in record.getMessage()
        assert "+1234567890" in record.getMessage()

        audit_logger.removeHandler(handler)

    def test_safe_reply_passes_clean_text(self) -> None:
        """Clean text should pass through _require_safe_reply unchanged."""
        orch = self._make_orchestrator()
        result = orch._require_safe_reply("+1234567890", "Hey, how's it going?")
        assert result == "Hey, how's it going?"


# ---------------------------------------------------------------------------
# Module 1: proactive initiation ordering
# ---------------------------------------------------------------------------

class TestProactiveOrdering:
    """Verify proactive initiation runs before sleep in main loop."""

    def test_proactive_before_sleep(self) -> None:
        """In _run_main_loop source, _check_proactive_initiation must appear before time.sleep."""
        import inspect
        from orchestrator import _run_main_loop

        source = inspect.getsource(_run_main_loop)
        # Find the positions of both calls within the while-loop body
        proactive_pos = source.find("_check_proactive_initiation()")
        sleep_pos = source.find("time.sleep(settings.POLL_INTERVAL_SECONDS)")

        assert proactive_pos > 0, "_check_proactive_initiation not found in _run_main_loop"
        assert sleep_pos > 0, "time.sleep not found in _run_main_loop"
        assert proactive_pos < sleep_pos, (
            "_check_proactive_initiation should appear before "
            "time.sleep(POLL_INTERVAL_SECONDS) in the main loop"
        )


# ---------------------------------------------------------------------------
# Module 4: LotL client error classification
# ---------------------------------------------------------------------------

class TestLotLErrorClassification:
    """Verify LotLClient fails fast on non-recoverable errors."""

    def test_captcha_error_fails_fast(self) -> None:
        """CAPTCHA errors should not be retried — fail immediately."""
        from services.lotl_client import LotLClient
        import httpx

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
