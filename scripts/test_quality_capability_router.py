from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts import quality_capability_router as router


class RegistryTests(unittest.TestCase):
    def test_quality_registry_has_no_dynamic_aliases(self) -> None:
        for policy in router.capability_registry().values():
            for candidate in policy.candidates:
                self.assertNotEqual(candidate.model, "openrouter/free")
                self.assertNotEqual(candidate.model, "openrouter/auto")
                self.assertFalse(candidate.model.endswith("-latest"))
                self.assertNotEqual(candidate.model, "latest")

    def test_production_and_emergency_tiers_are_explicit(self) -> None:
        gold = router.policy_for(router.CAP_GOLD_VISION)
        tiers = {candidate.tier for candidate in gold.candidates}
        self.assertIn(router.TIER_PRODUCTION_FREE, tiers)
        self.assertIn(router.TIER_EMERGENCY, tiers)
        self.assertTrue(
            all(
                candidate.provider == "openrouter"
                for candidate in gold.candidates
                if candidate.tier == router.TIER_EMERGENCY
            )
        )


class RouteDecisionTests(unittest.TestCase):
    @staticmethod
    def _all_credentials(_candidate) -> bool:
        return True

    @staticmethod
    def _healthy(_candidate):
        return None

    def test_sequence_filters_credential_then_health_then_ranks(self) -> None:
        def credential(candidate) -> bool:
            return candidate.provider != "gemini"

        class Evidence:
            reason = "quota exhausted"

        def health(candidate):
            if candidate.provider == "openrouter" and candidate.model.startswith("google/"):
                return Evidence()
            return None

        decision = router.route_candidates(
            router.CAP_GOLD_VISION,
            credential_probe=credential,
            health_probe=health,
        )
        self.assertEqual(decision.candidates[0].provider, "groq")
        rejected = {item.identity: item.reason for item in decision.rejected}
        self.assertTrue(any(reason == "credential_unavailable" for reason in rejected.values()))
        self.assertTrue(any(reason.startswith("health_block:") for reason in rejected.values()))

    def test_independent_auditor_excludes_source_provider(self) -> None:
        source = router.ArtifactProvenance(
            artifact="primary.json",
            capability=router.CAP_AUDIO_SEMANTIC_AUDIT,
            provider="groq",
            model="whisper-large-v3-turbo",
        )
        decision = router.route_candidates(
            router.CAP_INDEPENDENT_AUDIO_AUDIT,
            credential_probe=self._all_credentials,
            health_probe=self._healthy,
            exclude_provenance=(source,),
        )
        self.assertEqual([item.provider for item in decision.candidates], ["gemini"])
        self.assertTrue(
            any(item.reason == "independence_provider_conflict" for item in decision.rejected)
        )

    def test_minimum_two_audio_providers_fails_preflight_when_one_missing(self) -> None:
        with self.assertRaisesRegex(router.CapabilityRouteError, "ready_providers=1/2"):
            router.require_route(
                router.CAP_FINAL_QC,
                credential_probe=lambda item: item.provider == "groq",
                health_probe=self._healthy,
            )


class RetryTaxonomyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.candidate = router.policy_for(router.CAP_GOLD_VISION).candidates[0]

    def test_429_falls_back_immediately(self) -> None:
        result = router.classify_retry(candidate=self.candidate, http_status=429, error="quota")
        self.assertEqual(result.action, router.RetryAction.FALLBACK)

    def test_auth_isolates_and_falls_back(self) -> None:
        result = router.classify_retry(candidate=self.candidate, http_status=401, error="unauthorized")
        self.assertEqual(result.action, router.RetryAction.ISOLATE_AND_FALLBACK)

    def test_timeout_gets_one_bounded_retry_then_fallback(self) -> None:
        first = router.classify_retry(candidate=self.candidate, error="network timeout", transient_retry_count=0)
        second = router.classify_retry(candidate=self.candidate, error="network timeout", transient_retry_count=1)
        self.assertEqual(first.action, router.RetryAction.RETRY_SAME)
        self.assertEqual(second.action, router.RetryAction.FALLBACK)

    def test_valid_semantic_fail_is_terminal_not_provider_failure(self) -> None:
        result = router.classify_retry(
            candidate=self.candidate,
            valid_result=True,
            semantic_pass=False,
        )
        self.assertEqual(result.action, router.RetryAction.TERMINAL_QUALITY_FAIL)

    def test_schema_repair_is_bounded(self) -> None:
        first = router.classify_retry(candidate=self.candidate, error="invalid JSON schema", schema_repair_count=0)
        second = router.classify_retry(candidate=self.candidate, error="invalid JSON schema", schema_repair_count=1)
        self.assertEqual(first.action, router.RetryAction.REPAIR_SAME)
        self.assertEqual(second.action, router.RetryAction.FALLBACK)


class ReservationAndProvenanceTests(unittest.TestCase):
    @staticmethod
    def _all_credentials(_candidate) -> bool:
        return True

    @staticmethod
    def _healthy(_candidate):
        return None

    def test_critical_preflight_reserves_final_gold_and_audit(self) -> None:
        result = router.critical_capacity_preflight(
            credential_probe=self._all_credentials,
            health_probe=self._healthy,
        )
        self.assertEqual(result["decision"], "pass")
        capabilities = {item["capability"] for item in result["reservations"]}
        self.assertEqual(
            capabilities,
            {
                router.CAP_FINAL_QC,
                router.CAP_INDEPENDENT_AUDIO_AUDIT,
                router.CAP_GOLD_VISION,
                router.CAP_GOLD_TEXT,
            },
        )

    def test_artifact_provenance_round_trip_binds_sha(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            artifact = Path(temp) / "audit.json"
            subject = Path(temp) / "final.mp4"
            artifact.write_text('{"decision":"pass"}', encoding="utf-8")
            subject.write_bytes(b"final-bytes")
            written = router.record_artifact_provenance(
                artifact,
                capability=router.CAP_AUDIO_SEMANTIC_AUDIT,
                provider="groq",
                model="whisper-large-v3-turbo",
                subject=subject,
            )
            loaded = router.read_artifact_provenance(artifact)
            self.assertEqual(loaded, written)
            self.assertTrue(router.provenance_sidecar(artifact).is_file())


if __name__ == "__main__":
    unittest.main()
