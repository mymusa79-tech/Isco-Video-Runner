from __future__ import annotations

import unittest
from pathlib import Path


ENGINE_SHA = "38cda419dbe126e5b2690ab3cfd3e46ffc6a1474"


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

    def test_exact_immutable_approval_and_second_action_authorization_are_required(self):
        self.assertIn("request_id:", self.text)
        self.assertIn("request_sha256:", self.text)
        self.assertIn("authorization_id:", self.text)
        self.assertIn("Restore exact encrypted Telegram approval and dispatch authorization", self.text)
        self.assertIn("state/control-panel.json.enc", self.text)
        self.assertIn("validate_ready_request(request)", self.text)
        self.assertIn("validate_dispatch_authorization(state, request_id, expected, authorization_id)", self.text)
        self.assertIn('request.get("request_sha256") != expected', self.text)
        self.assertIn("approved-request.json", self.text)

    def test_authorization_is_consumed_and_persisted_once_before_any_production(self):
        restore = self.text.index("Restore exact encrypted Telegram approval and dispatch authorization")
        consume = self.text.index("Consume and persist one-time dispatch authorization")
        idempotency = self.text.index("Idempotency guard")
        engine = self.text.index("Checkout exact private Engine")
        production = self.text.index("Run exact approved Telegram production")
        self.assertLess(restore, consume)
        self.assertLess(consume, idempotency)
        self.assertLess(consume, engine)
        self.assertLess(consume, production)
        self.assertIn("telegram_production_queue.py consume", self.text)
        self.assertIn('--authorization-id "$AUTHORIZATION_ID"', self.text)
        self.assertIn('--workflow-run-id "$GITHUB_RUN_ID"', self.text)
        self.assertIn("state: consume Telegram production authorization", self.text)
        self.assertIn("control-panel.consumed.json.enc", self.text)
        self.assertIn("One-time Telegram authorization consumption produced no durable state delta", self.text)
        self.assertIn("persist-credentials: true", self.text)

    def test_runner_and_engine_are_exactly_bound(self):
        self.assertIn(f"EXPECTED_ENGINE_SHA: {ENGINE_SHA}", self.text)
        self.assertIn('test "$(git rev-parse HEAD)" = "$GITHUB_SHA"', self.text)
        self.assertIn('test "${GITHUB_REF_NAME}" = "main"', self.text)
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

    def test_delivery_stays_staged_until_real_release_step(self):
        validate = self.text.index("Validate exact staged delivery package")
        finalize = self.text.index("finalize_release_manifest(")
        release = self.text.index('gh release create "$RELEASE_TAG"')
        self.assertLess(validate, finalize)
        self.assertLess(finalize, release)
        self.assertIn('data.get("release_state") != "staged"', self.text)
        self.assertIn('data.get("release_tag") is not None', self.text)
        self.assertIn('data.get("delivery_url") is not None', self.text)

    def test_successful_topic_is_recorded_only_after_delivery_release_and_before_success_notification(self):
        release = self.text.index('gh release create "$RELEASE_TAG"')
        used = self.text.index("Record successful topic in encrypted used-topic history")
        notify = self.text.index("Notify Telegram final status")
        self.assertLess(release, used)
        self.assertLess(used, notify)
        self.assertIn("if: success() && steps.request.outcome == 'success'", self.text)
        self.assertIn("ui._mark_request_used(", self.text)
        self.assertIn("control-panel.used.json.enc", self.text)
        self.assertIn("state: record successful Telegram topic", self.text)
        self.assertIn("git push origin HEAD:control-plane-state", self.text)
        self.assertNotIn("continue-on-error: true\n        env:\n          STATE_ENCRYPTION_KEY: ${{ secrets.STATE_ENCRYPTION_KEY }}\n          RELEASE_TAG", self.text)

    def test_used_topic_persistence_is_idempotent_for_an_existing_release(self):
        used_block_start = self.text.index("Record successful topic in encrypted used-topic history")
        used_block_end = self.text.index("Checkout agent-state writer")
        block = self.text[used_block_start:used_block_end]
        self.assertNotIn("already_released != 'true'", block)
        self.assertIn("prior_used_at", block)
        self.assertIn("Used-topic history already contains this completed request", block)

    def test_final_master_qc_is_required_for_validation_diagnostics_and_release(self):
        self.assertIn('master_qc = root / "final-master-qc.json"', self.text)
        self.assertIn('qc.get("status") == "pass"', self.text)
        self.assertIn('qc.get("full_decode_ok") is True', self.text)
        self.assertIn('qc.get("final_media_mutated") is False', self.text)
        self.assertIn('not list(qc.get("blocking_findings") or [])', self.text)
        self.assertIn('embedded.get("file") != "final-master-qc.json"', self.text)
        self.assertIn("Upload Telegram production diagnostics on failure", self.text)
        self.assertGreaterEqual(self.text.count("engine/output/*/final-master-qc.json"), 1)
        self.assertIn('"final-master-qc.json", "final-critic.json"', self.text)

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

    def test_plaintext_control_and_runtime_material_are_removed(self):
        self.assertIn("Remove plaintext control secrets and state", self.text)
        self.assertIn('rm -rf "$RUNNER_TEMP/isco-secrets"', self.text)
        self.assertIn('rm -rf "$RUNNER_TEMP/isco-control"', self.text)
        self.assertIn('rm -rf "$RUNNER_TEMP/isco-state"', self.text)


if __name__ == "__main__":
    unittest.main()
