from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import scripts.telegram_publish_gate as gate


class FormatDurationTests(unittest.TestCase):
    def test_formats_minutes_and_seconds(self) -> None:
        self.assertEqual(gate._format_duration(125), "02:05")

    def test_rounds_to_nearest_second(self) -> None:
        self.assertEqual(gate._format_duration(59.6), "01:00")


class BuildWarningsTests(unittest.TestCase):
    """Covers the exact three warning rules agreed in the design - nothing vaguer."""

    def test_no_warnings_on_a_clean_run(self) -> None:
        quality = {"plan_source": "gemini", "av_delta_seconds": 0.1, "av_sync_max_delta_seconds": 1.0}
        telemetry = {"attempts": [{"provider": "gemini", "result": "success"}]}
        self.assertEqual(gate.build_warnings(quality, telemetry), [])

    def test_fallback_plan_source_warns(self) -> None:
        quality = {"plan_source": "product_proof_fallback"}
        warnings = gate.build_warnings(quality, None)
        self.assertEqual(len(warnings), 1)
        self.assertIn("احتياطي ثابت", warnings[0])

    def test_av_delta_below_half_threshold_does_not_warn(self) -> None:
        quality = {"av_delta_seconds": 0.2, "av_sync_max_delta_seconds": 1.0}
        self.assertEqual(gate.build_warnings(quality, None), [])

    def test_av_delta_above_half_threshold_warns(self) -> None:
        quality = {"av_delta_seconds": 0.6, "av_sync_max_delta_seconds": 1.0}
        warnings = gate.build_warnings(quality, None)
        self.assertEqual(len(warnings), 1)
        self.assertIn("تزامن", warnings[0])

    def test_failed_providers_before_success_are_listed_and_deduped(self) -> None:
        telemetry = {
            "attempts": [
                {"provider": "gemini", "result": "429"},
                {"provider": "gemini", "result": "circuit-open"},
                {"provider": "groq", "result": "invalid_json"},
                {"provider": "groq", "result": "success"},
            ]
        }
        warnings = gate.build_warnings({}, telemetry)
        self.assertEqual(len(warnings), 1)
        self.assertIn("gemini", warnings[0])
        self.assertIn("groq", warnings[0])
        # circuit-open and the final success must not appear as "failures".
        self.assertEqual(warnings[0].count("gemini"), 1)

    def test_missing_telemetry_is_fine(self) -> None:
        self.assertEqual(gate.build_warnings({}, None), [])


class BuildCaptionTests(unittest.TestCase):
    def test_includes_duration_topic_source_and_question(self) -> None:
        quality = {"video_stream_duration": 125, "plan_source": "gemini"}
        plan = {"topic": "كيف تنهض بعد أن سقطت كثيرًا؟"}
        caption = gate.build_caption(quality, plan, [])
        self.assertIn("02:05", caption)
        self.assertIn("كيف تنهض بعد أن سقطت كثيرًا؟", caption)
        self.assertIn("gemini", caption)
        self.assertIn("هل تريد نشر هذا الفيديو؟", caption)
        self.assertNotIn("تحذيرات", caption)

    def test_includes_warnings_section_when_present(self) -> None:
        caption = gate.build_caption({}, {}, ["تحذير تجريبي"])
        self.assertIn("⚠️ تحذيرات:", caption)
        self.assertIn("- تحذير تجريبي", caption)


class GenerateThumbnailTests(unittest.TestCase):
    def test_seeks_to_ten_percent_of_duration(self) -> None:
        with patch.object(gate.subprocess, "run") as mock_run:
            gate.generate_thumbnail(Path("final.mp4"), 200.0, Path("thumb.jpg"))
        args = mock_run.call_args[0][0]
        self.assertIn("-ss", args)
        self.assertEqual(args[args.index("-ss") + 1], "20.00")

    def test_seek_floor_is_one_second_for_short_videos(self) -> None:
        with patch.object(gate.subprocess, "run") as mock_run:
            gate.generate_thumbnail(Path("final.mp4"), 3.0, Path("thumb.jpg"))
        args = mock_run.call_args[0][0]
        self.assertEqual(args[args.index("-ss") + 1], "1.00")


class PrimeOffsetTests(unittest.TestCase):
    def test_empty_queue_returns_zero(self) -> None:
        with patch.object(gate, "_telegram_api", return_value=[]):
            self.assertEqual(gate._prime_offset("tok"), 0)

    def test_returns_one_past_the_highest_pending_update_id(self) -> None:
        with patch.object(gate, "_telegram_api", return_value=[{"update_id": 5}, {"update_id": 9}]):
            self.assertEqual(gate._prime_offset("tok"), 10)


class SendApprovalRequestTests(unittest.TestCase):
    def test_embeds_run_id_in_both_callback_data_values_and_returns_message_id(self) -> None:
        captured = {}

        def fake_api(token, method, payload=None, files=None):
            captured["method"] = method
            captured["payload"] = payload
            return {"message_id": 77}

        with tempfile.TemporaryDirectory() as d:
            thumb = Path(d) / "thumb.jpg"
            thumb.write_bytes(b"fake-jpeg")
            with patch.object(gate, "_telegram_api", side_effect=fake_api):
                message_id = gate.send_approval_request("tok", "chat1", thumb, "caption text", "run-123")

        self.assertEqual(message_id, 77)
        self.assertEqual(captured["method"], "sendPhoto")
        keyboard = json.loads(captured["payload"]["reply_markup"])
        buttons = keyboard["inline_keyboard"][0]
        self.assertEqual(buttons[0]["callback_data"], "approve:run-123")
        self.assertEqual(buttons[1]["callback_data"], "reject:run-123")


class HandleUpdateTests(unittest.TestCase):
    def _callback_update(self, *, message_id=42, data="approve:run-1", user_id=555, update_id=1):
        return {
            "update_id": update_id,
            "callback_query": {
                "id": "cbq1",
                "data": data,
                "from": {"id": user_id},
                "message": {"message_id": message_id},
            },
        }

    def test_non_callback_update_is_ignored(self) -> None:
        self.assertIsNone(gate._handle_update("tok", {"update_id": 1, "message": {}}, 42, "run-1"))

    def test_callback_for_a_different_message_is_ignored(self) -> None:
        update = self._callback_update(message_id=999)
        self.assertIsNone(gate._handle_update("tok", update, 42, "run-1"))

    def test_callback_data_for_a_different_run_is_ignored(self) -> None:
        update = self._callback_update(data="approve:some-other-run")
        self.assertIsNone(gate._handle_update("tok", update, 42, "run-1"))

    def test_authorized_approve_returns_decision(self) -> None:
        update = self._callback_update(data="approve:run-1", user_id=555)
        with patch.object(gate, "is_authorized_user", return_value=True), \
                patch.object(gate, "_telegram_api") as mock_api:
            result = gate._handle_update("tok", update, 42, "run-1")
        self.assertEqual(result["decision"], "approved")
        self.assertEqual(result["decided_by"], 555)
        mock_api.assert_called_once_with("tok", "answerCallbackQuery", payload={"callback_query_id": "cbq1"})

    def test_authorized_reject_returns_decision(self) -> None:
        update = self._callback_update(data="reject:run-1", user_id=555)
        with patch.object(gate, "is_authorized_user", return_value=True), \
                patch.object(gate, "_telegram_api"):
            result = gate._handle_update("tok", update, 42, "run-1")
        self.assertEqual(result["decision"], "rejected")

    def test_unauthorized_click_is_rejected_with_an_alert_and_returns_none(self) -> None:
        update = self._callback_update(data="approve:run-1", user_id=999)
        with patch.object(gate, "is_authorized_user", return_value=False), \
                patch.object(gate, "_telegram_api") as mock_api:
            result = gate._handle_update("tok", update, 42, "run-1")
        self.assertIsNone(result)
        mock_api.assert_called_once()
        self.assertEqual(mock_api.call_args[0][1], "answerCallbackQuery")
        self.assertEqual(mock_api.call_args[1]["payload"]["show_alert"], "true")


class PollForDecisionTests(unittest.TestCase):
    def test_returns_the_first_valid_decision_found(self) -> None:
        update = {"update_id": 1, "callback_query": {"id": "c1"}}
        with patch.object(gate, "_prime_offset", return_value=0), \
                patch.object(gate.time, "monotonic", side_effect=[0.0, 0.0]), \
                patch.object(gate, "_telegram_api", return_value=[update]), \
                patch.object(gate, "_handle_update", return_value={"decision": "approved", "decided_by": 555, "decided_at": "t"}):
            result = gate.poll_for_decision("tok", 42, "run-1", timeout_seconds=1800)
        self.assertEqual(result["decision"], "approved")

    def test_unauthorized_click_does_not_end_polling(self) -> None:
        update = {"update_id": 1, "callback_query": {"id": "c1"}}
        # Loop iteration 1 (unauthorized, keeps going), loop iteration 2 (authorized).
        with patch.object(gate, "_prime_offset", return_value=0), \
                patch.object(gate.time, "monotonic", side_effect=[0.0, 0.0, 0.0]), \
                patch.object(gate, "_telegram_api", return_value=[update]), \
                patch.object(gate, "_handle_update", side_effect=[None, {"decision": "rejected", "decided_by": 555, "decided_at": "t"}]):
            result = gate.poll_for_decision("tok", 42, "run-1", timeout_seconds=1800)
        self.assertEqual(result["decision"], "rejected")

    def test_times_out_when_deadline_passes_with_no_decision(self) -> None:
        # start=0.0, first loop check=0.0 (< deadline 5), second loop check=10.0 (>= deadline).
        with patch.object(gate, "_prime_offset", return_value=0), \
                patch.object(gate.time, "monotonic", side_effect=[0.0, 0.0, 10.0]), \
                patch.object(gate, "_telegram_api", return_value=[]):
            result = gate.poll_for_decision("tok", 42, "run-1", timeout_seconds=5)
        self.assertEqual(result["decision"], "timeout")
        self.assertIsNone(result["decided_by"])


class FinalizeDecisionTests(unittest.TestCase):
    def test_approved_edits_caption_only_no_extra_message(self) -> None:
        with patch.object(gate, "_telegram_api") as mock_api:
            gate.finalize_decision("tok", "chat1", 42, "approved", "approved", "https://example/run")
        self.assertEqual(mock_api.call_count, 1)
        self.assertEqual(mock_api.call_args[0][1], "editMessageCaption")

    def test_rejected_edits_caption_and_sends_a_download_link_message(self) -> None:
        with patch.object(gate, "_telegram_api") as mock_api:
            gate.finalize_decision("tok", "chat1", 42, "rejected", "rejected", "https://example/run")
        self.assertEqual(mock_api.call_count, 2)
        methods = [call.args[1] for call in mock_api.call_args_list]
        self.assertEqual(methods, ["editMessageCaption", "sendMessage"])
        send_payload = mock_api.call_args_list[1].kwargs["payload"]
        self.assertIn("https://example/run", send_payload["text"])

    def test_timeout_with_hold_default_behaves_like_rejected(self) -> None:
        with patch.object(gate, "_telegram_api") as mock_api:
            gate.finalize_decision("tok", "chat1", 42, "timeout", "rejected", "https://example/run")
        self.assertEqual(mock_api.call_count, 2)

    def test_timeout_with_publish_default_behaves_like_approved(self) -> None:
        with patch.object(gate, "_telegram_api") as mock_api:
            gate.finalize_decision("tok", "chat1", 42, "timeout", "approved", "https://example/run")
        self.assertEqual(mock_api.call_count, 1)


class RequestPublishApprovalTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.out_dir = Path(self._tmpdir.name)
        (self.out_dir / "quality-final.json").write_text(
            json.dumps({"video_stream_duration": 60, "plan_source": "gemini"}), encoding="utf-8"
        )
        (self.out_dir / "plan.json").write_text(json.dumps({"topic": "x"}), encoding="utf-8")
        (self.out_dir / "final.mp4").write_bytes(b"fake video bytes")

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_disabled_by_default_skips_everything_and_makes_no_network_calls(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("REQUIRE_PUBLISH_APPROVAL", None)
            with patch.object(gate, "_telegram_api") as mock_api, \
                    patch.object(gate, "generate_thumbnail") as mock_thumb:
                result = gate.request_publish_approval(out_dir=self.out_dir, run_id="run-1", run_url="https://x")
        mock_api.assert_not_called()
        mock_thumb.assert_not_called()
        self.assertEqual(result, {"decision": "disabled", "effective_decision": "approved"})

    def test_enabled_but_missing_bot_token_fails_loud(self) -> None:
        with patch.dict(os.environ, {"REQUIRE_PUBLISH_APPROVAL": "true"}, clear=False):
            os.environ.pop("TELEGRAM_BOT_TOKEN_FILE", None)
            with self.assertRaises(gate.PublishApprovalConfigError):
                gate.request_publish_approval(out_dir=self.out_dir, run_id="run-1", run_url="https://x")

    def test_enabled_and_fully_configured_runs_the_full_flow_and_returns_approved(self) -> None:
        with tempfile.TemporaryDirectory() as secrets_dir:
            token_path = Path(secrets_dir) / "token"
            token_path.write_text("bot-token", encoding="utf-8")
            chat_path = Path(secrets_dir) / "chat"
            chat_path.write_text("chat-1", encoding="utf-8")
            allowed_path = Path(secrets_dir) / "allowed"
            allowed_path.write_text("555", encoding="utf-8")

            env = {
                "REQUIRE_PUBLISH_APPROVAL": "true",
                "TELEGRAM_BOT_TOKEN_FILE": str(token_path),
                "TELEGRAM_CHAT_ID_FILE": str(chat_path),
                "TELEGRAM_ALLOWED_USER_ID_FILE": str(allowed_path),
            }
            with patch.dict(os.environ, env, clear=False), \
                    patch.object(gate, "generate_thumbnail") as mock_thumb, \
                    patch.object(gate, "send_approval_request", return_value=42) as mock_send, \
                    patch.object(gate, "poll_for_decision", return_value={"decision": "approved", "decided_by": 555, "decided_at": "t"}), \
                    patch.object(gate, "finalize_decision") as mock_finalize:
                result = gate.request_publish_approval(out_dir=self.out_dir, run_id="run-1", run_url="https://x")

        mock_thumb.assert_called_once()
        mock_send.assert_called_once()
        mock_finalize.assert_called_once_with("bot-token", "chat-1", 42, "approved", "approved", "https://x")
        self.assertEqual(result["effective_decision"], "approved")

    def test_timeout_resolves_via_configured_default_action(self) -> None:
        with tempfile.TemporaryDirectory() as secrets_dir:
            token_path = Path(secrets_dir) / "token"
            token_path.write_text("bot-token", encoding="utf-8")
            chat_path = Path(secrets_dir) / "chat"
            chat_path.write_text("chat-1", encoding="utf-8")
            allowed_path = Path(secrets_dir) / "allowed"
            allowed_path.write_text("555", encoding="utf-8")

            env = {
                "REQUIRE_PUBLISH_APPROVAL": "true",
                "PUBLISH_APPROVAL_TIMEOUT_ACTION": "publish",
                "TELEGRAM_BOT_TOKEN_FILE": str(token_path),
                "TELEGRAM_CHAT_ID_FILE": str(chat_path),
                "TELEGRAM_ALLOWED_USER_ID_FILE": str(allowed_path),
            }
            with patch.dict(os.environ, env, clear=False), \
                    patch.object(gate, "generate_thumbnail"), \
                    patch.object(gate, "send_approval_request", return_value=42), \
                    patch.object(gate, "poll_for_decision", return_value={"decision": "timeout", "decided_by": None, "decided_at": "t"}), \
                    patch.object(gate, "finalize_decision"):
                result = gate.request_publish_approval(out_dir=self.out_dir, run_id="run-1", run_url="https://x")

        self.assertEqual(result["decision"], "timeout")
        self.assertEqual(result["effective_decision"], "approved")

    def test_timeout_defaults_to_hold_when_action_env_unset(self) -> None:
        with tempfile.TemporaryDirectory() as secrets_dir:
            token_path = Path(secrets_dir) / "token"
            token_path.write_text("bot-token", encoding="utf-8")
            chat_path = Path(secrets_dir) / "chat"
            chat_path.write_text("chat-1", encoding="utf-8")
            allowed_path = Path(secrets_dir) / "allowed"
            allowed_path.write_text("555", encoding="utf-8")

            env = {
                "REQUIRE_PUBLISH_APPROVAL": "true",
                "TELEGRAM_BOT_TOKEN_FILE": str(token_path),
                "TELEGRAM_CHAT_ID_FILE": str(chat_path),
                "TELEGRAM_ALLOWED_USER_ID_FILE": str(allowed_path),
            }
            with patch.dict(os.environ, env, clear=False):
                os.environ.pop("PUBLISH_APPROVAL_TIMEOUT_ACTION", None)
                with patch.object(gate, "generate_thumbnail"), \
                        patch.object(gate, "send_approval_request", return_value=42), \
                        patch.object(gate, "poll_for_decision", return_value={"decision": "timeout", "decided_by": None, "decided_at": "t"}), \
                        patch.object(gate, "finalize_decision"):
                    result = gate.request_publish_approval(out_dir=self.out_dir, run_id="run-1", run_url="https://x")

        self.assertEqual(result["effective_decision"], "rejected")


class MainTests(unittest.TestCase):
    def test_writes_github_output_and_decision_file(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            original_cwd = os.getcwd()
            try:
                os.chdir(d)
                Path("output").mkdir()
                out_dir = Path("output/run-1")
                out_dir.mkdir()
                out_dir_abs = out_dir.resolve()
                github_output_path = Path(d) / "github_output.txt"

                env = {
                    "GITHUB_RUN_ID": "123",
                    "GITHUB_REPOSITORY": "owner/repo",
                    "GITHUB_OUTPUT": str(github_output_path),
                }
                with patch.dict(os.environ, env, clear=False), \
                        patch.object(gate, "request_publish_approval", return_value={
                            "decision": "disabled", "effective_decision": "approved",
                        }) as mock_request:
                    gate.main()
            finally:
                os.chdir(original_cwd)

            mock_request.assert_called_once()
            call_kwargs = mock_request.call_args.kwargs
            self.assertEqual(call_kwargs["run_id"], "123")
            self.assertEqual(call_kwargs["run_url"], "https://github.com/owner/repo/actions/runs/123")

            output_text = github_output_path.read_text(encoding="utf-8")
            self.assertIn("decision=disabled", output_text)
            self.assertIn("effective_decision=approved", output_text)

            decision_file = out_dir_abs / "publish-decision.json"
            written = json.loads(decision_file.read_text(encoding="utf-8"))
            self.assertEqual(written["decision"], "disabled")

    def test_raises_when_no_output_directory_exists(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            original_cwd = os.getcwd()
            try:
                os.chdir(d)
                with self.assertRaises(RuntimeError):
                    gate.main()
            finally:
                os.chdir(original_cwd)


if __name__ == "__main__":
    unittest.main()
