from __future__ import annotations

import unittest
from pathlib import Path


WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"

CANONICAL_OWNER = WORKFLOWS / "verify-private-engine.yml"
SPECIALIZED = (
    WORKFLOWS / "verify-human-editorial-intent-m7.yml",
    WORKFLOWS / "verify-m11-live-integration.yml",
    WORKFLOWS / "verify-voice-identity-observer-v1.yml",
)
PRODUCTION = WORKFLOWS / "produce-resilient-v4.yml"

ENGINE_FULL = "python -m unittest discover -s tests -q"
RUNNER_DISCOVERY = "find scripts -maxdepth 1 -type f -name 'test_*.py'"
DEPENDENCY_AUDIT = "pip-audit --no-deps -r requirements-lock.txt"
DEPENDENCY_AUDIT_PACKAGE = "pip-audit==2.10.1"
DEPENDENCY_AUDIT_ARGS = "--no-deps -r requirements-lock.txt"


class CIFullRegressionOwnershipTests(unittest.TestCase):
    """Freeze canonical Full Regression ownership and the T4 Production split."""

    def test_private_engine_is_the_only_full_regression_owner(self) -> None:
        owner = CANONICAL_OWNER.read_text(encoding="utf-8")
        self.assertIn(ENGINE_FULL, owner)
        self.assertIn(RUNNER_DISCOVERY, owner)
        self.assertIn(DEPENDENCY_AUDIT, owner)

        for path in SPECIALIZED:
            with self.subTest(workflow=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertNotIn(ENGINE_FULL, text)
                self.assertNotIn(RUNNER_DISCOVERY, text)
                self.assertNotIn(DEPENDENCY_AUDIT_PACKAGE, text)

        production = PRODUCTION.read_text(encoding="utf-8")
        self.assertNotIn(ENGINE_FULL, production)
        self.assertNotIn(RUNNER_DISCOVERY, production)

    def test_specialized_workflows_keep_their_unique_evidence(self) -> None:
        m7 = SPECIALIZED[0].read_text(encoding="utf-8")
        self.assertIn("tests.test_human_editorial_intent", m7)
        self.assertIn("scripts.test_m7_live_binding", m7)

        m11 = SPECIALIZED[1].read_text(encoding="utf-8")
        self.assertIn("tests.test_cinematic_m11_runtime", m11)
        self.assertIn("Real M11 FFmpeg renderer smoke", m11)
        self.assertIn("scripts.test_m11_live_binding", m11)

        voice = SPECIALIZED[2].read_text(encoding="utf-8")
        self.assertIn("scripts.test_voice_identity_observer", voice)
        self.assertIn("Assert immutable approved voice-reference provenance", voice)
        self.assertIn("ECAPA real-model smoke", voice)

    def test_production_keeps_live_dependency_audit_and_mutable_preflights(self) -> None:
        production = PRODUCTION.read_text(encoding="utf-8")
        self.assertIn(DEPENDENCY_AUDIT_PACKAGE, production)
        self.assertIn(DEPENDENCY_AUDIT_ARGS, production)
        for needle in (
            "Require protected exact-SHA production certification",
            "Restore encrypted cross-run memory",
            "Require healthy restored cross-run memory",
            "Verify local voice fallback before cloud production",
            "Verify production environment and release namespace",
            "Verify complete provider readiness",
            "Certify provider-portable planning envelope",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, production)


if __name__ == "__main__":
    unittest.main()
