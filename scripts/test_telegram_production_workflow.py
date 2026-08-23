from __future__ import annotations

import unittest
from pathlib import Path


ENGINE_SHA = "64ab711bc904e9581c3cc6c8280d1321ae738eb1"


class TelegramProductionWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.text = Path(".github/workflows/telegram-production-request.yml").read_text(encoding="utf-8")

    def test_workflow_is_dispatch_only_and_serialized_with_canonical_production(self):
        self.assertIn("workflow_dispatch:", self.text)
        self.assertNotIn("schedule:", self.text)
        self.assertNotIn("pull_request:", self.text)
        self.assertNotIn("push:", self.text)
        self.assertIn("group: isco-video-resilient-v4", self.text)
        self.assertIn("cancel-in-progress: false", self.text)

    def test_exact_immutable_approval_is_required_before_any_production(self):
        self.assertIn("request_id:", self.text)
        self.assertIn("request_sha256:", self.text)
        self.assertIn("Restore exact encrypted Telegram approval state", self.text)
        self.assertIn("state/control-panel.json.enc", self.text)
        self.assertIn("validate_ready_request(request)", self.text)
        self.assertIn('request.get("request_sha256") != expected', self.text)
        self.assertIn("approved-request.json", self.text)

    def test_engine_is_exactly_pinned_to_current_certified_engine(self):
        self.assertIn(f"EXPECTED_ENGINE_SHA: {ENGINE_SHA}", self.text)
        self.assertIn('test "$REQUESTED_ENGINE_SHA" = "$EXPECTED_ENGINE_SHA"', self.text)
        self.assertIn("ref: ${{ inputs.engine_sha }}", self.text)
        self.assertIn('test "$(git rev-parse HEAD)" = "$REQUESTED_ENGINE_SHA"', self.text)

    def test_production_enablement_exists_only_inside_explicit_target(self):
        self.assertIn('CONTROL_PLANE_PRODUCTION_ENABLED: "true"', self.text)
        self.assertIn("python ../scripts/run_telegram_control_production.py", self.text)
        self.assertNotIn("python ../scripts/run_v3_voice.py", self.text)

    def test_idempotent_release_prevents_successful_request_reproduction(self):
        self.assertIn("Idempotency guard", self.text)
        self.assertIn('gh release view "$RELEASE_TAG"', self.text)
        self.assertIn('already_released=true', self.text)
        self.assertIn("Create one deterministic delivery release", self.text)
        self.assertIn('gh release create "$RELEASE_TAG"', self.text)

    def test_delivery_is_manual_youtube_only(self):
        self.assertIn('youtube_publish_mode") != "manual_in_youtube_studio"', self.text)
        self.assertIn('publication_performed") is not False', self.text)
        self.assertIn("YouTube publication is manual", self.text)
        forbidden = (
            "youtube.videos().insert",
            "videos.insert",
            "upload_to_youtube",
            "youtube-upload",
            "publish_to_youtube",
        )
        for needle in forbidden:
            self.assertNotIn(needle, self.text)

    def test_plaintext_control_material_is_removed(self):
        self.assertIn("Remove plaintext control secrets and state", self.text)
        self.assertIn('rm -rf "$RUNNER_TEMP/isco-secrets"', self.text)
        self.assertIn("approved-request.json", self.text)


if __name__ == "__main__":
    unittest.main()
