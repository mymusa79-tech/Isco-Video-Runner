from __future__ import annotations

import unittest

from scripts import provider_capacity_hardening as capacity
from scripts import run123_planning_latency_hardening as run123
from scripts import run125_capacity_routing_closure as run125
from scripts import runtime_patch_contracts as contracts


MODEL = "openai/gpt-oss-120b"


class Run127RuntimeCompositionTests(unittest.TestCase):
    def test_full_runtime_source_scan_has_no_legacy_capacity_or_patch_signature_drift(self) -> None:
        result = contracts.repository_runtime_patch_audit()
        self.assertGreater(result["runtime_python_files_scanned"], 50)
        self.assertEqual(result["legacy_capacity_violations"], 0)
        self.assertEqual(result["static_patch_contract_violations"], 0)

    def test_exact_run127_stale_wrapper_shape_is_detected(self) -> None:
        def stale_cache_aware_pacing(request_capacity: dict) -> float:
            del request_capacity
            return 0.0

        with self.assertRaisesRegex(RuntimeError, "RUNTIME_CALL_CONTRACT_MISMATCH"):
            contracts._bind_contract(
                stale_cache_aware_pacing,
                {"estimated_request_tokens": 1},
                model_name=MODEL,
                label="run127_exact_failure_shape",
            )

    def test_run123_replacement_and_run125_wrapper_share_canonical_model_keyword(self) -> None:
        # This is the exact interface seam that Run127 missed in isolated unit tests.
        run125._assert_pacing_contract(
            run123._fast_failover_groq_pacing,
            label="run123_to_run125_pacing_seam",
        )

    def test_canonical_capacity_owner_accepts_same_contract(self) -> None:
        contracts._bind_contract(
            capacity._proactive_groq_pacing,
            {"estimated_request_tokens": 1},
            model_name=MODEL,
            label="canonical_capacity_pacing",
        )


if __name__ == "__main__":
    unittest.main()
