from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from scripts import planning_envelope_preflight as preflight


class Run227ShortStageProviderParityTests(unittest.TestCase):
    def test_actual_short_stage_contract_keeps_preflight_families_routable(self) -> None:
        effective = preflight._short_stage_provider_parity(
            "كيف تنهض عندما تفقد الدافع تمامًا؟",
            ("gemini", "groq"),
        )
        self.assertEqual(effective, ("gemini", "groq"))

    def test_preflight_fails_if_live_short_stages_cannot_route_two_families(self) -> None:
        narrowed = SimpleNamespace(
            provider_policy=SimpleNamespace(providers=("groq", "openrouter"))
        )
        with patch.object(preflight, "moment_stage_spec", return_value=narrowed):
            with self.assertRaisesRegex(
                RuntimeError,
                "SHORT_STAGE_PROVIDER_REDUNDANCY_REQUIRED",
            ):
                preflight._short_stage_provider_parity(
                    "موضوع قصير",
                    ("gemini", "groq"),
                )

    def test_preflight_reports_only_common_stage_families(self) -> None:
        draft = SimpleNamespace(
            provider_policy=SimpleNamespace(providers=("gemini", "groq", "openrouter"))
        )
        review = SimpleNamespace(
            provider_policy=SimpleNamespace(providers=("gemini", "groq"))
        )
        repair = SimpleNamespace(
            provider_policy=SimpleNamespace(providers=("gemini", "groq", "openrouter"))
        )
        with patch.object(
            preflight,
            "moment_stage_spec",
            side_effect=[draft, review, repair],
        ):
            effective = preflight._short_stage_provider_parity(
                "موضوع قصير",
                ("gemini", "groq", "openrouter"),
            )
        self.assertEqual(effective, ("gemini", "groq"))


if __name__ == "__main__":
    unittest.main()
