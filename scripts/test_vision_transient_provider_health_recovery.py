from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from isco_video_agent.ai_budget import Capability, Priority, TaskSpec
from scripts import provider_health_registry as health
from scripts import run181_vision_mesh_closure as closure
from scripts import task_level_planner_router as planner_router
from scripts import text_audit_provider_mesh as text_mesh
from scripts import vision_provider_reliability as legacy
from scripts import vision_stage_contract_v2 as v2


_PASS = {
    "status": "pass",
    "relevance": 0.93,
    "visual_quality": 0.90,
    "identifiable_person": False,
    "sensitive_trait_implication_risk": False,
    "prominent_logo_or_brand": False,
    "cultural_conflict": False,
    "cultural_islamic_suitability_risk": False,
    "advertiser_conflict": False,
    "obvious_synthetic_or_visual_artifact": False,
    "reason": "safe and relevant",
}


def _spec() -> TaskSpec:
    return TaskSpec(
        task_id="VISUAL_AUDIT_TRANSIENT_HEALTH",
        kind="VISUAL_AUDIT",
        priority=Priority.P0,
        capability=Capability.VISION,
        max_provider_attempts=1,
        schema_repair_allowed=False,
        local_fallback=False,
        semantic_block_is_final=False,
    )


def _preview(root: str) -> Path:
    path = Path(root) / "preview.mp4"
    path.write_bytes(b"preview")
    return path


class ProviderFailureTaxonomyTests(unittest.TestCase):
    def setUp(self) -> None:
        health.reset_provider_health()
        closure._GROQ_MODEL_CERTIFIED.set(None)

    def test_structural_invalid_is_observable_but_never_provider_unavailable(self) -> None:
        health.publish_provider_unavailable(
            "groq",
            model=closure.GROQ_VISION_MODEL,
            quota_domain=closure.GROQ_VISION_QUOTA_DOMAIN,
            reason="STRUCTURAL_INVALID response schema missing field",
            source="vision_stage",
        )
        self.assertIsNone(
            health.provider_unavailable(
                "groq",
                model=closure.GROQ_VISION_MODEL,
                quota_domain=closure.GROQ_VISION_QUOTA_DOMAIN,
            )
        )
        [row] = health.snapshot_provider_health()
        self.assertEqual(row["status"], "degraded")
        self.assertEqual(row["failure_class"], health.FAILURE_STRUCTURAL)

    def test_auth_model_and_daily_failures_stay_hard(self) -> None:
        cases = (
            ("HTTP_401 unauthorized", health.FAILURE_HARD),
            ("model not found in live catalog", health.FAILURE_MODEL_HARD),
            ("429 requests per day daily limit reached", health.FAILURE_HARD),
        )
        for reason, expected in cases:
            with self.subTest(reason=reason):
                health.reset_provider_health()
                health.publish_provider_unavailable(
                    "groq",
                    model=closure.GROQ_VISION_MODEL,
                    quota_domain=closure.GROQ_VISION_QUOTA_DOMAIN,
                    reason=reason,
                    source="vision_stage",
                )
                first = health.provider_unavailable(
                    "groq",
                    model=closure.GROQ_VISION_MODEL,
                    quota_domain=closure.GROQ_VISION_QUOTA_DOMAIN,
                )
                second = health.provider_unavailable(
                    "groq",
                    model=closure.GROQ_VISION_MODEL,
                    quota_domain=closure.GROQ_VISION_QUOTA_DOMAIN,
                )
                self.assertIsNotNone(first)
                self.assertIsNotNone(second)
                self.assertEqual(first.failure_class, expected)

    def test_preflight_hard_wildcard_dominates_later_exact_structural_evidence(self) -> None:
        health.publish_provider_failure(
            "openrouter",
            model="*",
            quota_domain="*",
            reason="key spend capacity exhausted",
            source="provider_preflight",
            failure_class=health.FAILURE_HARD,
        )
        health.publish_provider_failure(
            "openrouter",
            model="openrouter/free",
            quota_domain="vision",
            reason="STRUCTURAL_INVALID schema mismatch",
            source="vision_stage",
            failure_class=health.FAILURE_STRUCTURAL,
        )
        evidence = health.provider_unavailable(
            "openrouter",
            model="openrouter/free",
            quota_domain="vision",
        )
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence.failure_class, health.FAILURE_HARD)
        self.assertEqual(evidence.model, "*")

    def test_rate_limit_honors_retry_after_before_half_open(self) -> None:
        with mock.patch.object(health.time, "monotonic", side_effect=[100.0, 104.9, 105.1]):
            health.publish_provider_failure(
                "gemini",
                model="gemini-3.7-flash",
                quota_domain="generate_content",
                reason="HTTP_429 retry-after=5",
                source="planning_telemetry",
                failure_class=health.FAILURE_RATE_LIMITED,
                retry_after_seconds=5.0,
            )
            blocked = health.provider_unavailable(
                "gemini",
                model="gemini-3.7-flash",
                quota_domain="generate_content",
            )
            admitted = health.provider_unavailable(
                "gemini",
                model="gemini-3.7-flash",
                quota_domain="generate_content",
            )
        self.assertIsNotNone(blocked)
        self.assertIsNone(admitted)

    def test_runtime_openrouter_wildcard_503_is_recoverable_not_run_hard(self) -> None:
        health.publish_provider_unavailable(
            "openrouter",
            model="*",
            quota_domain="*",
            reason="PROVIDER_TRANSIENT HTTP_503 service unavailable",
            source="vision_stage",
        )
        diagnostic = health.provider_unavailable(
            "openrouter",
            model="openrouter/free",
            quota_domain="vision",
        )
        half_open = health.provider_unavailable(
            "openrouter",
            model="openrouter/free",
            quota_domain="vision",
        )
        self.assertIsNotNone(diagnostic)
        self.assertEqual(diagnostic.failure_class, health.FAILURE_TRANSIENT)
        self.assertIsNone(half_open)


class BoundedTransientCircuitTests(unittest.TestCase):
    def setUp(self) -> None:
        health.reset_provider_health()
        closure._GROQ_MODEL_CERTIFIED.set(None)

    def _publish_503(self) -> None:
        health.publish_provider_unavailable(
            "groq",
            model=closure.GROQ_VISION_MODEL,
            quota_domain=closure.GROQ_VISION_QUOTA_DOMAIN,
            reason="PROVIDER_TRANSIENT HTTP_503 currently over capacity",
            source="vision_stage",
        )

    def _lookup(self):
        return health.provider_unavailable(
            "groq",
            model=closure.GROQ_VISION_MODEL,
            quota_domain=closure.GROQ_VISION_QUOTA_DOMAIN,
        )

    def test_503_gets_two_bounded_half_open_recovery_probes_then_stays_open(self) -> None:
        self._publish_503()
        self.assertIsNotNone(self._lookup())
        self.assertIsNone(self._lookup())

        self._publish_503()
        self.assertIsNotNone(self._lookup())
        self.assertIsNotNone(self._lookup())
        self.assertIsNone(self._lookup())

        self._publish_503()
        for _ in range(5):
            evidence = self._lookup()
            self.assertIsNotNone(evidence)
            self.assertEqual(evidence.failure_count, 3)

    def test_success_after_half_open_closes_consecutive_failure_history(self) -> None:
        self._publish_503()
        self.assertIsNotNone(self._lookup())
        self.assertIsNone(self._lookup())
        self.assertIsNone(self._lookup())
        self._publish_503()
        [row] = health.snapshot_provider_health()
        self.assertEqual(row["failure_count"], 1)

    def test_new_vision_scope_resets_transient_family(self) -> None:
        first_scope = object()
        second_scope = object()
        health.bind_provider_health_to_vision_scope(first_scope)
        self._publish_503()
        self.assertTrue(health.snapshot_provider_health())
        self.assertTrue(health.bind_provider_health_to_vision_scope(second_scope))
        self.assertEqual(health.snapshot_provider_health(), [])


class Run181TransientRecoveryIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        health.reset_provider_health()
        closure._GROQ_MODEL_CERTIFIED.set(None)

    def _route(self, *args, **kwargs):
        with mock.patch.object(planner_router, "get_telemetry", return_value=[]), mock.patch.object(
            text_mesh,
            "_AUDIT_ROUTE_TELEMETRY",
            [],
        ):
            return closure._route_visual_audit_v3(*args, **kwargs)

    def test_groq_503_no_longer_poison_next_visual_candidate(self) -> None:
        health.publish_provider_failure(
            "gemini",
            model="gemini-3.7-flash",
            quota_domain=closure.GEMINI_GENERATION_QUOTA_DOMAIN,
            reason="HTTP_401 auth unavailable in test",
            source="test",
            failure_class=health.FAILURE_HARD,
        )
        health.publish_provider_failure(
            "openrouter",
            model="*",
            quota_domain="*",
            reason="spend capacity exhausted",
            source="provider_preflight",
            failure_class=health.FAILURE_HARD,
        )
        transient = v2.VisionStageError(
            v2.VisionErrorCode.PROVIDER_TRANSIENT,
            "HTTP_503 message=qwen is currently over capacity",
            provider="groq",
            requested_model=closure.GROQ_VISION_MODEL,
        )

        with tempfile.TemporaryDirectory() as root, legacy.vision_provider_circuit_scope(), mock.patch.object(
            closure,
            "_run_groq_attempt",
            side_effect=[transient, dict(_PASS)],
        ) as groq, mock.patch.object(
            v2,
            "_run_openrouter_attempt",
        ) as openrouter:
            with self.assertRaises(legacy.VisionProviderMeshUnavailableError):
                self._route(
                    None,
                    _spec(),
                    "gemini",
                    "gemini-3.7-flash",
                    mock.Mock(),
                    "gem-key",
                    _preview(root),
                    narration_context="ctx",
                    intended_visual="intent-1",
                )

            result = self._route(
                None,
                _spec(),
                "gemini",
                "gemini-3.7-flash",
                mock.Mock(),
                "gem-key",
                _preview(root),
                narration_context="ctx",
                intended_visual="intent-2",
            )

        self.assertEqual(result["status"], "pass")
        self.assertEqual(groq.call_count, 2)
        openrouter.assert_not_called()

    def test_legacy_gemini_503_gets_only_two_half_open_probes(self) -> None:
        with legacy.vision_provider_circuit_scope():
            state = legacy._state()
            for expected_probe in (1, 2):
                state.gemini_open = True
                state.gemini_reason = "PROVIDER_TRANSIENT HTTP_503 service unavailable"
                self.assertIsNone(
                    health.provider_unavailable(
                        "gemini",
                        model="gemini-3.7-flash",
                        quota_domain=closure.GEMINI_GENERATION_QUOTA_DOMAIN,
                    )
                )
                self.assertFalse(state.gemini_open)
                self.assertEqual(
                    health._CONSECUTIVE_FAILURES.get()[
                        ("gemini", "gemini-3.7-flash", closure.GEMINI_GENERATION_QUOTA_DOMAIN)
                    ],
                    expected_probe,
                )

            state.gemini_open = True
            state.gemini_reason = "PROVIDER_TRANSIENT HTTP_503 service unavailable"
            self.assertIsNone(
                health.provider_unavailable(
                    "gemini",
                    model="gemini-3.7-flash",
                    quota_domain=closure.GEMINI_GENERATION_QUOTA_DOMAIN,
                )
            )
            self.assertTrue(state.gemini_open)


if __name__ == "__main__":
    unittest.main()
