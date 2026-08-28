from __future__ import annotations

import unittest

from scripts import run125_cache_prefix_contract as prefix
from scripts import run125_capacity_routing_closure as closure


class Run125QuotaClassifierTests(unittest.TestCase):
    def test_tpd_exhaustion_is_nonretryable_run_circuit(self) -> None:
        prefix.install_run125_cache_prefix_contract()
        failure = closure.router.classify_provider_failure(
            "groq",
            "GROQ_HTTP_429 status=429 code=rate_limit_exceeded on tokens per day (TPD): Limit 200000",
        )
        self.assertEqual(failure.telemetry_result, "quota_exhausted")
        self.assertTrue(failure.open_circuit)
        self.assertNotIn(failure.telemetry_result, closure.router._TRANSIENT_RESULTS)


if __name__ == "__main__":
    unittest.main()
