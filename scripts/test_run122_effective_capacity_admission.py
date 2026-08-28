from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import planning_batch_hardening as planning
from scripts import run120_dossier_repair_hardening as dossier
from scripts import run122_effective_capacity_admission as hardening


class Run122EffectiveCapacityAdmissionTests(unittest.TestCase):
    def test_capacity_estimate_uses_post_enrichment_prompt(self) -> None:
        seen: list[str] = []

        def fake_estimate(prompt: str) -> dict:
            seen.append(prompt)
            return {"estimated_request_tokens": len(prompt)}

        with patch.object(
            hardening.router,
            "_enrich_dialogue_prompt",
            side_effect=lambda prompt: prompt + "|dialogue",
        ), patch.object(
            hardening.router,
            "with_channel_persona",
            side_effect=lambda prompt: "persona|" + prompt,
        ), patch.object(
            hardening.capacity,
            "groq_capacity_estimate",
            side_effect=fake_estimate,
        ):
            result = hardening._effective_capacity_estimate("raw")

        self.assertEqual(seen, ["persona|raw|dialogue"])
        self.assertEqual(result["estimated_request_tokens"], len("persona|raw|dialogue"))

    def test_writer_doctor_rejects_before_provider_when_no_provider_can_carry_effective_prompt(self) -> None:
        original = planning._capacity_admitted
        had_flag = hasattr(planning, "_ISCO_RUN122_EFFECTIVE_CAPACITY_ADMISSION")
        old_flag = getattr(planning, "_ISCO_RUN122_EFFECTIVE_CAPACITY_ADMISSION", None)
        original_dossier = dossier._one_schema_bounded_call
        try:
            if had_flag:
                delattr(planning, "_ISCO_RUN122_EFFECTIVE_CAPACITY_ADMISSION")
            hardening.install_run122_effective_capacity_admission()
            estimate = {
                "estimated_request_tokens": 8077,
                "contract": "full_script",
                "estimated_prompt_tokens": 5427,
                "reserved_completion_tokens": 2400,
                "token_safety_reserve": 250,
            }
            with patch.object(
                hardening, "_effective_capacity_estimate", return_value=estimate
            ), patch.object(hardening, "_provider_set_viable", return_value=[]):
                admitted, returned = planning._capacity_admitted("raw")
            self.assertFalse(admitted)
            self.assertIs(returned, estimate)
            self.assertEqual(returned["viable_providers"], [])
        finally:
            planning._capacity_admitted = original
            dossier._one_schema_bounded_call = original_dossier
            if had_flag:
                planning._ISCO_RUN122_EFFECTIVE_CAPACITY_ADMISSION = old_flag
            elif hasattr(planning, "_ISCO_RUN122_EFFECTIVE_CAPACITY_ADMISSION"):
                delattr(planning, "_ISCO_RUN122_EFFECTIVE_CAPACITY_ADMISSION")

    def test_writer_doctor_does_not_split_only_because_groq_bootstrap_8k_is_exceeded(self) -> None:
        original = planning._capacity_admitted
        had_flag = hasattr(planning, "_ISCO_RUN122_EFFECTIVE_CAPACITY_ADMISSION")
        old_flag = getattr(planning, "_ISCO_RUN122_EFFECTIVE_CAPACITY_ADMISSION", None)
        original_dossier = dossier._one_schema_bounded_call
        try:
            if had_flag:
                delattr(planning, "_ISCO_RUN122_EFFECTIVE_CAPACITY_ADMISSION")
            hardening.install_run122_effective_capacity_admission()
            estimate = {
                "estimated_request_tokens": 8077,
                "contract": "full_script",
                "estimated_prompt_tokens": 5427,
                "reserved_completion_tokens": 2400,
                "token_safety_reserve": 250,
            }
            with patch.object(
                hardening, "_effective_capacity_estimate", return_value=estimate
            ), patch.object(hardening, "_provider_set_viable", return_value=["gemini"]):
                admitted, returned = planning._capacity_admitted("raw")
            self.assertTrue(admitted)
            self.assertIs(returned, estimate)
            self.assertEqual(returned["viable_providers"], ["gemini"])
        finally:
            planning._capacity_admitted = original
            dossier._one_schema_bounded_call = original_dossier
            if had_flag:
                planning._ISCO_RUN122_EFFECTIVE_CAPACITY_ADMISSION = old_flag
            elif hasattr(planning, "_ISCO_RUN122_EFFECTIVE_CAPACITY_ADMISSION"):
                delattr(planning, "_ISCO_RUN122_EFFECTIVE_CAPACITY_ADMISSION")

    def test_dossier_no_viable_provider_becomes_existing_transport_pressure_without_call(self) -> None:
        original_admission = planning._capacity_admitted
        original_dossier = dossier._one_schema_bounded_call
        had_flag = hasattr(planning, "_ISCO_RUN122_EFFECTIVE_CAPACITY_ADMISSION")
        old_flag = getattr(planning, "_ISCO_RUN122_EFFECTIVE_CAPACITY_ADMISSION", None)
        owner = Mock(return_value={"s1": {"narration": "ok", "key_point": "k"}})
        try:
            dossier._one_schema_bounded_call = owner
            if had_flag:
                delattr(planning, "_ISCO_RUN122_EFFECTIVE_CAPACITY_ADMISSION")
            hardening.install_run122_effective_capacity_admission()
            estimate = {
                "estimated_request_tokens": 8166,
                "contract": "full_script",
                "estimated_prompt_tokens": 5516,
                "reserved_completion_tokens": 2400,
                "token_safety_reserve": 250,
            }
            with patch.object(
                hardening, "_effective_capacity_estimate", return_value=estimate
            ), patch.object(hardening, "_provider_set_viable", return_value=[]):
                with self.assertRaisesRegex(
                    dossier._DossierTransportPressure, "NO_VIABLE_PLANNING_CAPACITY"
                ):
                    dossier._one_schema_bounded_call("key", "prompt", "model", ["s1", "s2"])
            owner.assert_not_called()
        finally:
            planning._capacity_admitted = original_admission
            dossier._one_schema_bounded_call = original_dossier
            if had_flag:
                planning._ISCO_RUN122_EFFECTIVE_CAPACITY_ADMISSION = old_flag
            elif hasattr(planning, "_ISCO_RUN122_EFFECTIVE_CAPACITY_ADMISSION"):
                delattr(planning, "_ISCO_RUN122_EFFECTIVE_CAPACITY_ADMISSION")

    def test_dossier_viable_prompt_delegates_to_existing_schema_owner(self) -> None:
        original_admission = planning._capacity_admitted
        original_dossier = dossier._one_schema_bounded_call
        had_flag = hasattr(planning, "_ISCO_RUN122_EFFECTIVE_CAPACITY_ADMISSION")
        old_flag = getattr(planning, "_ISCO_RUN122_EFFECTIVE_CAPACITY_ADMISSION", None)
        expected = {"s1": {"narration": "ok", "key_point": "k"}}
        owner = Mock(return_value=expected)
        try:
            dossier._one_schema_bounded_call = owner
            if had_flag:
                delattr(planning, "_ISCO_RUN122_EFFECTIVE_CAPACITY_ADMISSION")
            hardening.install_run122_effective_capacity_admission()
            estimate = {
                "estimated_request_tokens": 8166,
                "contract": "full_script",
                "estimated_prompt_tokens": 5516,
                "reserved_completion_tokens": 2400,
                "token_safety_reserve": 250,
            }
            with patch.object(
                hardening, "_effective_capacity_estimate", return_value=estimate
            ), patch.object(hardening, "_provider_set_viable", return_value=["gemini"]):
                actual = dossier._one_schema_bounded_call("key", "prompt", "model", ["s1"])
            self.assertEqual(actual, expected)
            owner.assert_called_once_with("key", "prompt", "model", ["s1"])
        finally:
            planning._capacity_admitted = original_admission
            dossier._one_schema_bounded_call = original_dossier
            if had_flag:
                planning._ISCO_RUN122_EFFECTIVE_CAPACITY_ADMISSION = old_flag
            elif hasattr(planning, "_ISCO_RUN122_EFFECTIVE_CAPACITY_ADMISSION"):
                delattr(planning, "_ISCO_RUN122_EFFECTIVE_CAPACITY_ADMISSION")

    def test_production_wiring_wraps_final_schema_owner_without_package_side_effect(self) -> None:
        bridge_source = Path("scripts/run120_schema_policy_bridge.py").read_text(encoding="utf-8")
        init_source = Path("scripts/__init__.py").read_text(encoding="utf-8")
        owner = bridge_source.index("    hardening._one_schema_bounded_call = _policy_owned_call")
        run122 = bridge_source.index("    install_run122_effective_capacity_admission()")
        self.assertLess(owner, run122)
        self.assertIn(
            "from .run122_effective_capacity_admission import install_run122_effective_capacity_admission",
            bridge_source,
        )
        self.assertNotIn("run122_effective_capacity_admission", init_source)


if __name__ == "__main__":
    unittest.main()
