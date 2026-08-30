from __future__ import annotations

import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import scripts.telegram_publish_gate as gate
from scripts.orchestration_telegram_ingress_outbox import ApprovalDecision, ReleaseCandidateDigest
from scripts.telegram_release_approval import approval_id_for_candidate

H1 = "a" * 64
H2 = "b" * 64
H3 = "c" * 64
H4 = "d" * 64


def candidate() -> ReleaseCandidateDigest:
    return ReleaseCandidateDigest(
        run_id="123",
        final_mp4_sha256=H1,
        delivery_manifest_sha256=H2,
        capability_manifest_sha256=H3,
        release_asset_set_digest=H4,
    )


class FormattingTests(unittest.TestCase):
    def test_duration_and_warning_rules_are_preserved(self) -> None:
        self.assertEqual(gate._format_duration(125), "02:05")
        quality = {"plan_source": "product_proof_fallback", "av_delta_seconds": .6, "av_sync_max_delta_seconds": 1.0}
        warnings = gate.build_warnings(quality, None)
        self.assertEqual(len(warnings), 2)

    def test_caption_binds_visible_candidate_fingerprint_and_review_url(self) -> None:
        c = candidate()
        text = gate.build_caption(
            {"video_stream_duration": 125, "plan_source": "gemini"},
            {"topic": "موضوع"},
            [],
            c,
            "https://github.example/run/123",
        )
        self.assertIn(c.digest[:12], text)
        self.assertIn("https://github.example/run/123", text)
        self.assertIn("هذه النسخة بالضبط", text)


class OutboxRequestTests(unittest.TestCase):
    def test_request_uses_digest_bound_callbacks(self) -> None:
        c = candidate()
        request = gate.build_outbox_request(candidate=c, caption="review", created_at="2026-08-29T20:00:00+00:00")
        self.assertEqual(request["approval_id"], approval_id_for_candidate(c))
        self.assertEqual(request["candidate_digest"], c.digest)
        self.assertEqual(request["method"], "sendMessage")
        buttons = request["payload"]["reply_markup"]["inline_keyboard"][0]
        self.assertEqual(buttons[0]["callback_data"].split(":", 1)[1], approval_id_for_candidate(c))
        self.assertEqual(buttons[1]["callback_data"].split(":", 1)[1], approval_id_for_candidate(c))

    def test_dispatch_requires_github_token(self) -> None:
        with self.assertRaises(gate.PublishApprovalConfigError):
            gate.dispatch_outbox_request("o/r", {"x": 1}, "")

    def test_dispatch_targets_single_outbox_workflow(self) -> None:
        response = type("R", (), {"status_code": 204, "text": ""})()
        with patch.object(gate.requests, "post", return_value=response) as post:
            gate.dispatch_outbox_request("o/r", {"schema_version": 1}, "token", ref="main")
        url = post.call_args.args[0]
        self.assertIn("telegram-outbox-send.yml/dispatches", url)
        self.assertEqual(post.call_args.kwargs["json"]["ref"], "main")
        encoded = post.call_args.kwargs["json"]["inputs"]["outbox_request_b64"]
        self.assertEqual(json.loads(base64.b64decode(encoded)), {"schema_version": 1})


class ProjectionTests(unittest.TestCase):
    def test_private_projection_is_read_through_authenticated_contents_api(self) -> None:
        payload = {"schema_version": 1, "release_approvals": []}
        envelope = {"encoding": "base64", "content": base64.b64encode(json.dumps(payload).encode()).decode()}
        response = type(
            "R",
            (),
            {"status_code": 200, "raise_for_status": lambda self: None, "json": lambda self: envelope},
        )()
        with patch.object(gate.requests, "get", return_value=response) as get:
            result = gate._read_projection("o/r", "token", 7)
        self.assertEqual(result, payload)
        self.assertIn("api.github.com/repos/o/r/contents/state/telegram-status.json", get.call_args.args[0])
        self.assertIn("Bearer token", get.call_args.kwargs["headers"]["authorization"])
        self.assertEqual(get.call_args.kwargs["params"]["ref"], "control-plane-state")

    def test_missing_projection_is_treated_as_no_decision(self) -> None:
        response = type("R", (), {"status_code": 404})()
        with patch.object(gate.requests, "get", return_value=response):
            self.assertEqual(gate._read_projection("o/r", "token", 1), {})

    def test_poll_accepts_only_exact_candidate_receipt(self) -> None:
        c = candidate()
        projection = {
            "schema_version": 1,
            "release_approvals": [
                {
                    "approval_id": approval_id_for_candidate(c),
                    "candidate_digest": c.digest,
                    "decision": ApprovalDecision.APPROVED.value,
                    "decided_at": "2026-08-29T20:00:00+00:00",
                }
            ],
        }
        with patch.object(gate, "_read_projection", return_value=projection), patch.object(
            gate.time, "monotonic", side_effect=[0.0, 0.0]
        ):
            result = gate.poll_for_decision("o/r", c, "token", timeout_seconds=10)
        self.assertEqual(result["decision"], "approved")

    def test_timeout_is_fail_closed(self) -> None:
        c = candidate()
        with patch.object(gate, "_read_projection", return_value={}), patch.object(
            gate.time, "monotonic", side_effect=[0.0, 10.0]
        ):
            result = gate.poll_for_decision("o/r", c, "token", timeout_seconds=5)
        self.assertEqual(result["decision"], "timeout")
        self.assertEqual(gate.effective_decision_after_timeout(), "rejected")


class RequestApprovalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "quality-final.json").write_text(json.dumps({"video_stream_duration": 60, "plan_source": "gemini"}), encoding="utf-8")
        (self.root / "plan.json").write_text(json.dumps({"topic": "x"}), encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_disabled_gate_has_no_dispatch(self) -> None:
        with patch.dict(os.environ, {"REQUIRE_PUBLISH_APPROVAL": "false"}, clear=False), patch.object(
            gate, "dispatch_outbox_request"
        ) as dispatch:
            result = gate.request_publish_approval(
                out_dir=self.root,
                run_id="123",
                run_url="https://run",
                repository="o/r",
                github_token="token",
            )
        dispatch.assert_not_called()
        self.assertEqual(result, {"decision": "disabled", "effective_decision": "approved"})

    def test_enabled_gate_queues_outbox_then_waits_for_webhook_receipt(self) -> None:
        c = candidate()
        with patch.dict(os.environ, {"REQUIRE_PUBLISH_APPROVAL": "true"}, clear=False), patch.object(
            gate, "build_release_candidate", return_value=c
        ), patch.object(gate, "dispatch_outbox_request") as dispatch, patch.object(
            gate, "poll_for_decision", return_value={"decision": "approved", "decided_at": "t"}
        ) as poll:
            result = gate.request_publish_approval(
                out_dir=self.root,
                run_id="123",
                run_url="https://run",
                repository="o/r",
                github_token="token",
            )
        dispatch.assert_called_once()
        poll.assert_called_once_with("o/r", c, "token")
        self.assertEqual(result["effective_decision"], "approved")
        self.assertEqual(result["candidate_digest"], c.digest)

    def test_enabled_gate_timeout_never_auto_publishes(self) -> None:
        c = candidate()
        with patch.dict(os.environ, {"REQUIRE_PUBLISH_APPROVAL": "true"}, clear=False), patch.object(
            gate, "build_release_candidate", return_value=c
        ), patch.object(gate, "dispatch_outbox_request"), patch.object(
            gate, "poll_for_decision", return_value={"decision": "timeout", "decided_at": "t"}
        ):
            result = gate.request_publish_approval(
                out_dir=self.root,
                run_id="123",
                run_url="https://run",
                repository="o/r",
                github_token="token",
            )
        self.assertEqual(result["effective_decision"], "rejected")


if __name__ == "__main__":
    unittest.main()
