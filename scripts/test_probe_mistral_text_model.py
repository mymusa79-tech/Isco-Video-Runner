from __future__ import annotations

import unittest

from scripts import probe_mistral_text_model as probe


def _result(status: int, **headers: str):
    return {
        "http_status": status,
        "rate_limit_headers": headers,
    }


class ProbeClassificationTests(unittest.TestCase):
    def test_zero_rpm_target_with_working_control_is_model_entitlement(self) -> None:
        result = probe.classify(
            _result(429, **{"x-ratelimit-limit-req-minute": "0"}),
            _result(200, **{"x-ratelimit-limit-req-minute": "30"}),
        )

        self.assertEqual(result["outcome"], "target_zero_rpm_for_account")
        self.assertTrue(result["decisive"])

    def test_positive_rpm_limit_is_temporary_exhaustion(self) -> None:
        result = probe.classify(
            _result(
                429,
                **{
                    "x-ratelimit-limit-req-minute": "2",
                    "x-ratelimit-remaining-req-minute": "0",
                    "retry-after": "31",
                },
            ),
            _result(200),
        )

        self.assertEqual(result["outcome"], "target_temporarily_rate_limited")
        self.assertTrue(result["retry_after_present"])

    def test_both_rate_limited_is_ambiguous(self) -> None:
        result = probe.classify(_result(429), _result(429))

        self.assertEqual(result["outcome"], "account_or_temporal_capacity_exhausted")
        self.assertFalse(result["decisive"])

    def test_success_proves_target_available_now(self) -> None:
        result = probe.classify(_result(200), _result(200))

        self.assertEqual(result["outcome"], "target_available_now")
        self.assertTrue(result["decisive"])


if __name__ == "__main__":
    unittest.main()
