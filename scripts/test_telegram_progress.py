from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import telegram_progress as tp  # noqa: E402  (needs sys.path fixup above)

import isco_video_agent.orchestrator as orchestrator  # noqa: E402


class ProgressRenderTests(unittest.TestCase):
    def setUp(self) -> None:
        tp._state["completed"] = set()
        tp._state["current_stage"] = None

    def test_all_stages_pending_render_shortly_after_start(self) -> None:
        text = tp._render()
        self.assertIn("التخطيط... ⏳", text)
        self.assertIn("الصوت... ⏳", text)
        self.assertIn("المشاهد... ⏳", text)
        self.assertIn("التجميع... ⏳", text)

    def test_current_stage_shows_in_progress_marker(self) -> None:
        tp._state["current_stage"] = "voice"
        tp._state["completed"] = {"planning"}
        text = tp._render()
        self.assertIn("التخطيط... ✅", text)
        self.assertIn("الصوت... 🔵", text)
        self.assertIn("المشاهد... ⏳", text)


class OptionalSecretFileTests(unittest.TestCase):
    def test_missing_env_var_returns_empty_string(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SOME_MISSING_FILE_VAR", None)
            self.assertEqual(tp._read_secret_file_optional("SOME_MISSING_FILE_VAR"), "")

    def test_nonexistent_path_returns_empty_string_instead_of_raising(self) -> None:
        with patch.dict(os.environ, {"X_FILE": "/no/such/path/on/disk"}, clear=False):
            self.assertEqual(tp._read_secret_file_optional("X_FILE"), "")

    def test_reads_and_strips_file_contents(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "secret"
            path.write_text("  abc123  \n", encoding="utf-8")
            with patch.dict(os.environ, {"X_FILE": str(path)}, clear=False):
                self.assertEqual(tp._read_secret_file_optional("X_FILE"), "abc123")


class StartProgressNoOpTests(unittest.TestCase):
    """A Telegram outage or absent secrets must never touch the network or fail
    production - this is best-effort progress reporting, not a required dependency."""

    def test_no_network_call_when_bot_token_missing(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            chat_id_path = Path(d) / "chat_id"
            chat_id_path.write_text("12345", encoding="utf-8")
            env = {
                "TELEGRAM_BOT_TOKEN_FILE": "/no/such/token/file",
                "TELEGRAM_CHAT_ID_FILE": str(chat_id_path),
                "RUNNER_TEMP": d,
            }
            with patch.dict(os.environ, env, clear=False):
                with patch.object(tp, "_telegram_request") as mock_request:
                    tp.start_progress()
                    mock_request.assert_not_called()
            self.assertIsNone(tp._state["message_id"])

    def test_update_stage_is_a_no_op_before_start_progress_ever_succeeds(self) -> None:
        tp._state["message_id"] = None
        with patch.object(tp, "_telegram_request") as mock_request:
            tp.update_stage("planning")
            mock_request.assert_not_called()


class IsAuthorizedUserTests(unittest.TestCase):
    """Documents the fixed security rule for future command-receiving development;
    nothing calls this yet - install_progress_hooks() never wires it up."""

    def test_returns_false_when_allowed_id_secret_absent(self) -> None:
        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USER_ID_FILE": "/no/such/file"}, clear=False):
            self.assertFalse(tp.is_authorized_user(999))

    def test_returns_true_only_for_the_exact_configured_id(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "allowed"
            path.write_text("555444333", encoding="utf-8")
            with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USER_ID_FILE": str(path)}, clear=False):
                self.assertTrue(tp.is_authorized_user(555444333))
                self.assertFalse(tp.is_authorized_user(1))

    def test_malformed_configured_id_fails_closed_rather_than_crashing(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "allowed"
            path.write_text("not-a-number", encoding="utf-8")
            with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USER_ID_FILE": str(path)}, clear=False):
                self.assertFalse(tp.is_authorized_user(1))


class InstallProgressHooksTests(unittest.TestCase):
    """Covers the fix already established for product_proof_plan.py/task_level_planner_
    router.py: any new wrapper placed over orchestrator.build_plan must forward the
    _is_resilient_router marker, or orchestrator._verify_resilient_router_installed()
    raises even though the real router is correctly installed underneath."""

    def setUp(self) -> None:
        self._orig_build_plan = orchestrator.build_plan
        self._orig_synthesize_wav = orchestrator.synthesize_wav
        self._orig_prepare_clip = orchestrator.prepare_clip
        self._orig_mux = orchestrator.mux
        tp._state["completed"] = set()
        tp._state["current_stage"] = None
        tp._state["message_id"] = None

    def tearDown(self) -> None:
        orchestrator.build_plan = self._orig_build_plan
        orchestrator.synthesize_wav = self._orig_synthesize_wav
        orchestrator.prepare_clip = self._orig_prepare_clip
        orchestrator.mux = self._orig_mux

    def test_marker_attribute_survives_being_wrapped(self) -> None:
        def routed_build_plan(*a, **k):
            return "plan"

        routed_build_plan._is_resilient_router = True
        orchestrator.build_plan = routed_build_plan
        orchestrator.synthesize_wav = lambda *a, **k: None
        orchestrator.prepare_clip = lambda *a, **k: None
        orchestrator.mux = lambda *a, **k: None

        tp.install_progress_hooks()

        self.assertTrue(getattr(orchestrator.build_plan, "_is_resilient_router", False))

    def test_stages_marked_done_in_the_right_order(self) -> None:
        orchestrator.build_plan = lambda *a, **k: "plan"
        orchestrator.synthesize_wav = lambda *a, **k: None
        orchestrator.prepare_clip = lambda *a, **k: None
        orchestrator.mux = lambda *a, **k: "final.mp4"

        calls: list[str] = []
        with patch.object(tp, "update_stage", side_effect=lambda s: calls.append(f"update:{s}")):
            with patch.object(tp, "mark_stage_done", side_effect=lambda s: calls.append(f"done:{s}")):
                tp.install_progress_hooks()
                orchestrator.build_plan()
                orchestrator.synthesize_wav()
                orchestrator.synthesize_wav()  # a second section's TTS call must not re-signal "voice" started
                orchestrator.prepare_clip()
                orchestrator.prepare_clip()  # a second section's clip prep must not re-signal "visuals" started
                orchestrator.mux()

        self.assertEqual(
            calls,
            [
                "update:planning",
                "done:planning",
                "update:voice",
                "done:voice",
                "update:visuals",
                "done:visuals",
                "update:mux",
                "done:mux",
            ],
        )


if __name__ == "__main__":
    unittest.main()
