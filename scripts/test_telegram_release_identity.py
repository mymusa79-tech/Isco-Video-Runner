from __future__ import annotations

import json
import subprocess
import unittest

from scripts.telegram_release_identity import verify_existing_release


SHA = "a" * 40
TAG = "telegram-request-abc"


def _payload(*, target: str = SHA, draft: bool = False, missing: set[str] | None = None) -> dict:
    names = {
        "final.mp4",
        "delivery-manifest.json",
        "plan.json",
        "quality-final.json",
        "final-master-qc.json",
        "final-critic.json",
        "ai-budget.json",
        "production-manifest.json",
        "rights-manifest.json",
        "gold-enforce-report.json",
        "sibling-short-plan.json",
        "sibling-short-results.json",
        "short-01.mp4",
        "short-02.mp4",
    }
    names -= missing or set()
    return {
        "tagName": TAG,
        "isDraft": draft,
        "targetCommitish": target,
        "assets": [
            {"name": name, "size": 2048, "digest": "sha256:" + ("b" * 64)}
            for name in sorted(names)
        ],
    }


def _run_for(payload: dict):
    def run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(payload), stderr="")

    return run


class TelegramReleaseIdentityTests(unittest.TestCase):
    def test_complete_release_passes_with_digest_and_target_evidence(self) -> None:
        result = verify_existing_release(
            repository="owner/repo",
            tag=TAG,
            target_sha=SHA,
            request={"kind": "long", "approval_scope": "long_plus_sibling_shorts"},
            run=_run_for(_payload()),
        )
        self.assertEqual(result["target_sha"], SHA)
        self.assertGreaterEqual(result["asset_count"], 14)

    def test_partial_release_is_not_idempotent_success(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            verify_existing_release(
                repository="owner/repo",
                tag=TAG,
                target_sha=SHA,
                request={"kind": "long"},
                run=_run_for(_payload(missing={"final-master-qc.json"})),
            )

    def test_wrong_target_is_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "target"):
            verify_existing_release(
                repository="owner/repo",
                tag=TAG,
                target_sha=SHA,
                request={"kind": "short"},
                run=_run_for(_payload(target="c" * 40)),
            )

    def test_draft_release_is_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "published"):
            verify_existing_release(
                repository="owner/repo",
                tag=TAG,
                target_sha=SHA,
                request={"kind": "short"},
                run=_run_for(_payload(draft=True)),
            )

    def test_missing_digest_is_rejected(self) -> None:
        payload = _payload()
        payload["assets"][0]["digest"] = ""
        with self.assertRaisesRegex(RuntimeError, "SHA256"):
            verify_existing_release(
                repository="owner/repo",
                tag=TAG,
                target_sha=SHA,
                request={"kind": "short"},
                run=_run_for(payload),
            )

    def test_long_plus_shorts_requires_actual_short_assets(self) -> None:
        payload = _payload(missing={"short-01.mp4", "short-02.mp4"})
        with self.assertRaisesRegex(RuntimeError, "sibling Short assets"):
            verify_existing_release(
                repository="owner/repo",
                tag=TAG,
                target_sha=SHA,
                request={"kind": "long", "approval_scope": "long_plus_sibling_shorts"},
                run=_run_for(payload),
            )


if __name__ == "__main__":
    unittest.main()
