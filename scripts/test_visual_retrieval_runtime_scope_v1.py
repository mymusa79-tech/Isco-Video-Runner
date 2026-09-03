from __future__ import annotations

import unittest
from unittest import mock

from scripts import provider_health_registry as health
from scripts import run181_vision_mesh_closure as run181
from scripts import visual_retrieval_adjudication_v1 as v1
from scripts import visual_retrieval_runtime_scope_v1 as scope
from scripts import vision_stage_contract_v2 as contract


class VisualRetrievalRuntimeScopeV1Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        v1.install_visual_retrieval_adjudication_v1()
        scope.install_visual_retrieval_runtime_scope_v1()

    def setUp(self) -> None:
        health.reset_provider_health()
        v1._GROQ_CAPACITY.set(None)

    def tearDown(self) -> None:
        health.reset_provider_health()
        v1._GROQ_CAPACITY.set(None)

    def test_scope_is_false_by_default_and_nested_safe(self) -> None:
        self.assertFalse(scope.active())
        with scope.visual_retrieval_runtime_scope():
            self.assertTrue(scope.active())
            with scope.visual_retrieval_runtime_scope():
                self.assertTrue(scope.active())
            self.assertTrue(scope.active())
        self.assertFalse(scope.active())

    def test_conditional_function_preserves_legacy_outside_and_v1_inside(self) -> None:
        def legacy(value):
            return f"legacy:{value}"

        def active(value):
            return f"v1:{value}"

        active._test_original = legacy
        wrapped = scope._conditional_function(
            active,
            original_attr="_test_original",
            marker="_test_scope_marker",
        )
        self.assertEqual(wrapped("x"), "legacy:x")
        with scope.visual_retrieval_runtime_scope():
            self.assertEqual(wrapped("x"), "v1:x")
        self.assertEqual(wrapped("x"), "legacy:x")

    def test_short_window_qwen_rate_limit_is_legacy_block_outside_production(self) -> None:
        health.publish_provider_unavailable(
            "groq",
            model=run181.GROQ_VISION_MODEL,
            quota_domain=run181.GROQ_VISION_QUOTA_DOMAIN,
            reason="429 tokens per minute rate limit reached",
            source="scope-test",
        )
        evidence = health.provider_unavailable(
            "groq",
            model=run181.GROQ_VISION_MODEL,
            quota_domain=run181.GROQ_VISION_QUOTA_DOMAIN,
        )
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence.source, "scope-test")

    def test_short_window_qwen_rate_limit_becomes_cooldown_inside_production(self) -> None:
        with mock.patch.object(contract.legacy, "_state", return_value=object()), mock.patch.object(
            v1.time, "monotonic", return_value=100.0
        ), scope.visual_retrieval_runtime_scope():
            health.publish_provider_unavailable(
                "groq",
                model=run181.GROQ_VISION_MODEL,
                quota_domain=run181.GROQ_VISION_QUOTA_DOMAIN,
                reason="429 tokens per minute rate limit reached; try again in 7.5s",
                source="scope-test",
            )
            evidence = health.provider_unavailable(
                "groq",
                model=run181.GROQ_VISION_MODEL,
                quota_domain=run181.GROQ_VISION_QUOTA_DOMAIN,
            )
            state = v1._GROQ_CAPACITY.get()
        self.assertIsNone(evidence)
        self.assertIsNotNone(state)
        self.assertAlmostEqual(state.next_allowed_monotonic, 107.5, places=2)

    def test_daily_qwen_limit_remains_hard_block_inside_production(self) -> None:
        with scope.visual_retrieval_runtime_scope():
            health.publish_provider_unavailable(
                "groq",
                model=run181.GROQ_VISION_MODEL,
                quota_domain=run181.GROQ_VISION_QUOTA_DOMAIN,
                reason="429 tokens per day limit reached",
                source="scope-test",
            )
        evidence = health.provider_unavailable(
            "groq",
            model=run181.GROQ_VISION_MODEL,
            quota_domain=run181.GROQ_VISION_QUOTA_DOMAIN,
        )
        self.assertIsNotNone(evidence)

    def test_contact_sheet_transport_is_scoped_not_global(self) -> None:
        sampler = contract.legacy._sample_preview_frames
        self.assertTrue(getattr(sampler, "_isco_contact_sheet_runtime_scope_v1", False))
        inactive = getattr(sampler, "_isco_visual_scope_inactive_target")
        active = getattr(sampler, "_isco_visual_scope_active_target")
        self.assertIsNot(inactive, active)
        self.assertTrue(getattr(active, "_isco_contact_sheet_v1", False))


if __name__ == "__main__":
    unittest.main()
