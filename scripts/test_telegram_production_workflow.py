from __future__ import annotations

import unittest
from pathlib import Path


def _canonical_production_engine_sha() -> str:
    text = Path(".github/workflows/produce-resilient-v4.yml").read_text(encoding="utf-8")
    prefix = "  EXPECTED_ENGINE_SHA: "
    pins = [line[len(prefix) :].strip() for line in text.splitlines() if line.startswith(prefix)]
    if len(pins) != 1:
        raise AssertionError(f"expected exactly one canonical production Engine pin, found {len(pins)}")
    sha = pins[0]
    if len(sha) != 40 or any(ch not in "0123456789abcdef" for ch in sha):
        raise AssertionError(f"canonical production Engine pin is not an exact lowercase SHA-1: {sha!r}")
    return sha


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
        engine_sha = _canonical_production_engine_sha()
        for field in ("request_id:", "request_sha256:", "authorization_id:", "engine_sha:"):
            self.assertIn(field, self.text)
        self.assertIn(f"EXPECTED_ENGINE_SHA: {engine_sha}", self.text)
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

    def test_only_canonical_v4_receives_the_reserved_request_with_tracked_dispatch(self):
        dispatch = self.text.index("Dispatch exact reservation to the single V4 owner")
        race = self.text.index("Reject post-dispatch concurrency race instead of waiting")
        block = self.text[dispatch:race]
        self.assertIn("actions/workflows/produce-resilient-v4.yml/dispatches", block)
        self.assertIn("X-GitHub-Api-Version: 2026-03-10", block)
        self.assertIn("return_run_details:true", block)
        self.assertIn('ref:"main"', block)
        self.assertIn("request_id:$request_id", block)
        self.assertIn("request_sha256:$request_sha256", block)
        self.assertIn("authorization_id:$authorization_id", block)
        self.assertIn("engine_sha:$engine_sha", block)
        self.assertIn(".workflow_run_id // empty", block)
        self.assertIn("workflow_run_id=$child_run_id", block)
        self.assertIn("workflow_run_url=$child_run_url", block)
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

    def test_post_dispatch_race_cancels_only_our_pending_child_and_releases_reservation(self):
        start = self.text.index("Reject post-dispatch concurrency race instead of waiting")
        end = self.text.index("Release reservation when admission or dispatch cannot start V4")
        block = self.text[start:end]
        self.assertIn("CHILD_RUN_ID: ${{ steps.dispatch.outputs.workflow_run_id }}", block)
        self.assertIn("for attempt in 1 2 3", block)
        self.assertIn("actions/runs/${CHILD_RUN_ID}", block)
        self.assertIn("produce-resilient-v4.yml/runs?per_page=20", block)
        self.assertIn(".id != $child", block)
        self.assertIn("actions/runs/${CHILD_RUN_ID}/cancel", block)
        self.assertIn('echo "accepted=false"', block)
        self.assertIn('echo "reason=concurrency_race"', block)
        self.assertNotIn("cancel-in-progress: true", block)

        release_start = end
        release_end = self.text.index("Notify only when the request could not start")
        release = self.text[release_start:release_end]
        self.assertIn("steps.race_guard.outcome == 'failure'", release)
        self.assertIn("steps.race_guard.outputs.accepted == 'false'", release)
        self.assertIn("telegram_v4_ingress.py fail", release)
        self.assertIn("state: release failed Telegram V4 admission", release)

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
        self.assertIn("steps.race_guard.outcome == 'failure'", block)
        self.assertIn("steps.race_guard.outputs.accepted == 'false'", block)

    def test_gateway_notification_never_claims_a_queued_wait(self):
        self.assertIn("RACE_ACCEPTED: ${{ steps.race_guard.outputs.accepted }}", self.text)
        self.assertIn("RACE_ACCEPTED", self.text)
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
