from __future__ import annotations

import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from scripts import opening_feasibility_guard as opening_guard
from scripts import provider_health_registry as health
from scripts import run181_vision_mesh_closure as run181
from scripts import run200_short_vision_recovery_closure as closure
from scripts import short_cinematic_director as short_director


TECHNICAL = {
    "status": "block",
    "reason": "Vision provider call failed technically: 429",
    "review_origin": "runner_vision_provider_call_failure",
    "vision_review_performed": False,
    "semantic_verdict": False,
    "verdict_authority": "technical_unavailable",
}
PASS = {
    "status": "pass",
    "relevance": 0.95,
    "visual_quality": 0.95,
    "vision_review_performed": True,
    "semantic_verdict": True,
    "verdict_authority": "vision",
}


class Run200TechnicalRetryTests(unittest.TestCase):
    def test_technical_unavailable_is_not_a_semantic_verdict(self) -> None:
        self.assertTrue(closure._is_technical_unavailable(dict(TECHNICAL)))
        self.assertFalse(closure._is_technical_unavailable(dict(PASS)))
        semantic_block = dict(TECHNICAL)
        semantic_block.update(
            vision_review_performed=True,
            semantic_verdict=True,
            verdict_authority="vision",
        )
        self.assertFalse(closure._is_technical_unavailable(semantic_block))

    def test_same_candidate_half_open_retries_once_only_when_live_cooldown_exists(self) -> None:
        original_factory = short_director._stable_intent_audit
        calls = {"n": 0}

        def base_factory(audit_fn, intended_visual):
            del intended_visual

            def base(*args, **kwargs):
                del args, kwargs
                calls["n"] += 1
                return dict(TECHNICAL) if calls["n"] == 1 else dict(PASS)

            return base

        short_director._stable_intent_audit = base_factory
        try:
            with mock.patch.object(closure, "_active_groq_cooldown_seconds", return_value=2.0):
                closure._install_short_same_candidate_half_open()
                wrapped = short_director._stable_intent_audit(lambda **_: {}, "intent")
                result = wrapped()
            self.assertEqual(result["status"], "pass")
            self.assertEqual(calls["n"], 2)
        finally:
            short_director._stable_intent_audit = original_factory

    def test_no_cooldown_means_no_hidden_retry(self) -> None:
        original_factory = short_director._stable_intent_audit
        calls = {"n": 0}

        def base_factory(audit_fn, intended_visual):
            del audit_fn, intended_visual

            def base(*args, **kwargs):
                del args, kwargs
                calls["n"] += 1
                return dict(TECHNICAL)

            return base

        short_director._stable_intent_audit = base_factory
        try:
            with mock.patch.object(closure, "_active_groq_cooldown_seconds", return_value=None):
                closure._install_short_same_candidate_half_open()
                wrapped = short_director._stable_intent_audit(lambda **_: {}, "intent")
                result = wrapped()
            self.assertTrue(closure._is_technical_unavailable(result))
            self.assertEqual(calls["n"], 1)
        finally:
            short_director._stable_intent_audit = original_factory

    def test_active_cooldown_requires_real_429_and_bounded_future_deadline(self) -> None:
        state = types.SimpleNamespace(last_status=429, next_allowed_monotonic=110.0)
        with mock.patch.object(closure.visual_v1, "_capacity_state", return_value=state), mock.patch.object(
            closure.time, "monotonic", return_value=100.0
        ):
            self.assertEqual(closure._active_groq_cooldown_seconds(), 10.0)

        state.last_status = 503
        with mock.patch.object(closure.visual_v1, "_capacity_state", return_value=state), mock.patch.object(
            closure.time, "monotonic", return_value=100.0
        ):
            self.assertIsNone(closure._active_groq_cooldown_seconds())

        state.last_status = 429
        state.next_allowed_monotonic = 100.0 + closure.MAX_HALF_OPEN_WAIT_SECONDS + 1.0
        with mock.patch.object(closure.visual_v1, "_capacity_state", return_value=state), mock.patch.object(
            closure.time, "monotonic", return_value=100.0
        ):
            self.assertIsNone(closure._active_groq_cooldown_seconds())


class Run200TruthAndForensicsTests(unittest.TestCase):
    def _failed_result(self):
        review = types.SimpleNamespace(
            provider="groq",
            candidate={"id": 123},
            from_cache=False,
            audit=dict(TECHNICAL),
        )
        return types.SimpleNamespace(
            status="failed",
            chosen=None,
            reviewed=[review],
            used_alternate_query=False,
        )

    def test_failed_unmade_verdict_is_truthfully_vision_unavailable_and_persisted(self) -> None:
        original_selector = short_director.select_with_recovery
        short_director.select_with_recovery = lambda *args, **kwargs: self._failed_result()
        try:
            closure._install_truthful_short_selector_outcome()
            with tempfile.TemporaryDirectory() as root:
                token_root = closure._ACTIVE_ROOT.set(Path(root))
                token_index = closure._SELECTOR_CALL_INDEX.set(0)
                try:
                    with self.assertRaises(opening_guard.VisionVerdictUnavailableError) as raised:
                        short_director.select_with_recovery(intended_visual="same editorial intent")
                finally:
                    closure._SELECTOR_CALL_INDEX.reset(token_index)
                    closure._ACTIVE_ROOT.reset(token_root)
                self.assertIn("VISION_UNAVAILABLE", str(raised.exception))
                partial = Path(root) / closure.PARTIAL_AUDIT_FILENAME
                self.assertTrue(partial.is_file())
                rows = json.loads(partial.read_text(encoding="utf-8"))
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["candidate_id"], 123)
                self.assertFalse(rows[0]["vision_review_performed"])
                self.assertEqual(rows[0]["verdict_authority"], "technical_unavailable")
        finally:
            short_director.select_with_recovery = original_selector

    def test_real_semantic_failure_is_not_reclassified_as_provider_outage(self) -> None:
        semantic = dict(TECHNICAL)
        semantic.update(
            status="block",
            reason="safe model judged the clip irrelevant",
            review_origin="short_cinematic_cloud_visual_qa",
            vision_review_performed=True,
            semantic_verdict=True,
            verdict_authority="vision",
        )
        review = types.SimpleNamespace(
            provider="groq",
            candidate={"id": 456},
            from_cache=False,
            audit=semantic,
        )
        result = types.SimpleNamespace(
            status="failed",
            chosen=None,
            reviewed=[review],
            used_alternate_query=True,
        )
        self.assertIs(
            opening_guard._enforce_truthful_visual_outcome(result, scope="short_cinematic"),
            result,
        )


class Run200CooldownOwnershipTests(unittest.TestCase):
    def test_observed_header_cooldown_is_not_extended_by_string_fallback(self) -> None:
        original = health.publish_provider_unavailable
        downstream = mock.Mock(return_value=None)
        health.publish_provider_unavailable = downstream
        try:
            with mock.patch.object(closure, "_active_groq_cooldown_seconds", return_value=7.5):
                closure._install_exact_groq_cooldown_preservation()
                health.publish_provider_unavailable(
                    "groq",
                    model=run181.GROQ_VISION_MODEL,
                    quota_domain=run181.GROQ_VISION_QUOTA_DOMAIN,
                    reason="HTTP_429 rate limit",
                    source="vision_stage",
                )
            downstream.assert_not_called()
        finally:
            health.publish_provider_unavailable = original

    def test_daily_limit_still_flows_to_hard_unavailable_owner(self) -> None:
        original = health.publish_provider_unavailable
        downstream = mock.Mock(return_value=None)
        health.publish_provider_unavailable = downstream
        try:
            with mock.patch.object(closure, "_active_groq_cooldown_seconds", return_value=7.5):
                closure._install_exact_groq_cooldown_preservation()
                health.publish_provider_unavailable(
                    "groq",
                    model=run181.GROQ_VISION_MODEL,
                    quota_domain=run181.GROQ_VISION_QUOTA_DOMAIN,
                    reason="429 tokens per day daily limit",
                    source="vision_stage",
                )
            downstream.assert_called_once()
        finally:
            health.publish_provider_unavailable = original


if __name__ == "__main__":
    unittest.main()
