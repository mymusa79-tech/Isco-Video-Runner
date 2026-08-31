from __future__ import annotations

import unittest
from pathlib import Path


ENGINE_SHA = "fe576d91f604412a010fa6cd61ff66f839e67550"


class TelegramProductionWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.text = Path(".github/workflows/telegram-production-request.yml").read_text(encoding="utf-8")

    def test_workflow_is_explicit_admission_only(self):
        self.assertIn("workflow_dispatch:", self.text)
        self.assertNotIn("schedule:", self.text)
        self.assertNotIn("pull_request:", self.text)
        self.assertNotIn("push:", self.text)
        self.assertIn("actions: write", self.text)
        self.assertIn("contents: write", self.text)
        self.assertIn("timeout-minutes: 10", self.text)

    def test_exact_request_authorization_engine_and_runner_are_bound(self):
        for field in ("request_id:", "request_sha256:", "authorization_id:", "engine_sha:"):
            self.assertIn(field, self.text)
        self.assertIn(f"EXPECTED_ENGINE_SHA: {ENGINE_SHA}", self.text)
        self.assertIn('test "$(git rev-parse HEAD)" = "$GITHUB_SHA"', self.text)
        self.assertIn('test "${GITHUB_REF_NAME}" = "main"', self.text)
        self.assertIn('test "$REQUESTED_ENGINE_SHA" = "$EXPECTED_ENGINE_SHA"', self.text)
        self.assertIn("Restore exact durable Telegram reservation", self.text)
        self.assertIn("validate_ready_request(request)", self.text)
        self.assertIn("validate_dispatch_authorization(", self.text)
        self.assertIn('runner_sha=os.environ["GITHUB_SHA"]', self.text)
        self.assertIn('request.get("request_sha256") != expected', self.text)

    def test_gateway_does_not_own_engine_runtime_or_release(self):
        forbidden = (
            "repository: mymusa79-tech/Isco-Video-Agent",
            "run_v3_voice.py",
            "run_telegram_control_production.py",
            "run_control_production.py",
            "release_transaction.py",
            "piper-tts",
            "GROQ_API_KEY",
            "PEXELS_API_KEY",
            "PIXABAY_API_KEY",
            "Create GitHub Release",
        )
        for needle in forbidden:
            self.assertNotIn(needle, self.text)

    def test_only_canonical_v4_receives_the_reserved_request(self):
        self.assertIn("gh workflow run produce-resilient-v4.yml", self.text)
        self.assertIn('--ref main', self.text)
        self.assertIn('-f request_id="$REQUEST_ID"', self.text)
        self.assertIn('-f request_sha256="$REQUEST_SHA256"', self.text)
        self.assertIn('-f authorization_id="$AUTHORIZATION_ID"', self.text)
        self.assertIn('-f engine_sha="$ENGINE_SHA"', self.text)
        self.assertNotIn("gh workflow run telegram-production-request.yml", self.text)

    def test_admission_never_queues_behind_active_v4(self):
        capacity = self.text.index("Admission gate — never queue behind an active V4 run")
        dispatch = self.text.index("Dispatch exact reservation to the single V4 owner")
        block = self.text[capacity:dispatch]
        self.assertIn("produce-resilient-v4.yml/runs?per_page=20", block)
        self.assertIn('select(.status != "completed")', block)
        self.assertIn('echo "available=false"', block)
        self.assertIn("will not be queued behind it", block)
        self.assertIn("steps.capacity.outputs.available == 'true'", self.text)

    def test_verified_existing_release_is_reconciled_without_new_production(self):
        duplicate = self.text.index("Reject duplicate completed release without starting Production")
        reconcile = self.text.index("Reconcile an already completed release into the latest durable state")
        capacity = self.text.index("Admission gate — never queue behind an active V4 run")
        block = self.text[duplicate:capacity]
        self.assertLess(duplicate, reconcile)
        self.assertIn('gh release view "$RELEASE_TAG"', block)
        self.assertIn("telegram_release_identity.py", block)
        self.assertIn('--target-sha "$GITHUB_SHA"', block)
        self.assertIn("git fetch --no-tags origin control-plane-state", block)
        self.assertIn("telegram_production_queue.py consume", block)
        self.assertIn('--runner-sha "$GITHUB_SHA"', block)
        self.assertIn("telegram_v4_ingress.py complete", block)

    def test_failed_admission_reloads_latest_state_and_releases_reservation(self):
        start = self.text.index("Release reservation when admission or dispatch cannot start V4")
        end = self.text.index("Notify only when the request could not start")
        block = self.text[start:end]
        fetch = block.index("git fetch --no-tags origin control-plane-state")
        fail = block.index("telegram_v4_ingress.py fail")
        self.assertLess(fetch, fail)
        self.assertIn("workflow_dispatch_failed", block)
        self.assertIn("state: release failed Telegram V4 admission", block)
        self.assertIn("steps.capacity.outputs.available == 'false'", block)
        self.assertIn("steps.dispatch.outcome == 'failure'", block)

    def test_gateway_notification_never_claims_a_queued_wait(self):
        self.assertIn("لم أضع اختيارك في طابور انتظار", self.text)
        self.assertIn("لم يُترك في انتظار أو حجز صامت", self.text)

    def test_gateway_never_publishes_to_youtube(self):
        forbidden = (
            "youtube.videos().insert",
            "videos.insert",
            "upload_to_youtube",
            "youtube-upload",
            "publish_to_youtube",
            "YOUTUBE_REFRESH_TOKEN",
        )
        for needle in forbidden:
            self.assertNotIn(needle, self.text)

    def test_plaintext_admission_state_is_removed(self):
        self.assertIn("Remove plaintext admission state", self.text)
        self.assertIn('rm -rf "$RUNNER_TEMP/isco-control"', self.text)


if __name__ == "__main__":
    unittest.main()
