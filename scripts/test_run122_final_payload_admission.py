from __future__ import annotations

import inspect
import unittest
from unittest import mock

from scripts import planning_batch_hardening as batching
from scripts import run120_dossier_repair_hardening as dossier
from scripts import run120_schema_policy_bridge as bridge
from scripts import run122_final_payload_admission as admission


class Run122FinalPayloadAdmissionTests(unittest.TestCase):
    def test_capacity_estimate_uses_dialogue_then_persona_final_payload(self) -> None:
        captured = {}

        def fake_estimate(prompt: str) -> dict:
            captured["prompt"] = prompt
            return {"estimated_request_tokens": 7999}

        with mock.patch.object(
            admission.router, "_enrich_dialogue_prompt", side_effect=lambda value: value + "|DIALOGUE"
        ) as dialogue, mock.patch.object(
            admission.router, "with_channel_persona", side_effect=lambda value: value + "|PERSONA"
        ) as persona, mock.patch.object(
            admission.capacity, "groq_capacity_estimate", side_effect=fake_estimate
        ):
            result = admission.routed_groq_capacity_estimate("RAW")

        self.assertEqual(result["estimated_request_tokens"], 7999)
        self.assertEqual(captured["prompt"], "RAW|DIALOGUE|PERSONA")
        dialogue.assert_called_once_with("RAW")
        persona.assert_called_once_with("RAW|DIALOGUE")

    def test_writer_doctor_split_before_provider_when_only_final_payload_exceeds_limit(self) -> None:
        provider_calls: list[tuple[str, ...]] = []

        def prompt_builder(ids: list[str]) -> str:
            return "RAW:" + "|".join(ids)

        def fake_final_estimate(prompt: str) -> dict:
            # Simulate Run #122: the raw semantic prompt looked portable, but the exact
            # post-middleware payload is over 8K for the parent shard. Child shards fit.
            section_count = prompt.count("|") + 1 if "RAW:" in prompt else 1
            total = 8034 if section_count >= 2 else 7900
            return {"estimated_request_tokens": total}

        def fake_provider(_key, _prompt, _model, *, expected_ids):
            provider_calls.append(tuple(expected_ids))
            return {
                section_id: {"narration": section_id, "key_point": section_id}
                for section_id in expected_ids
            }

        with mock.patch.object(
            admission, "routed_groq_capacity_estimate", side_effect=fake_final_estimate
        ), mock.patch.object(
            batching, "_capacity_admitted", side_effect=admission._routed_capacity_admitted
        ), mock.patch.object(
            batching.staged, "_call_with_schema_repair", side_effect=fake_provider
        ):
            result = batching._call_capacity_aware_shard(
                "key", "model", ["s1", "s2"], prompt_builder=prompt_builder, label="doctor"
            )

        self.assertEqual(provider_calls, [("s1",), ("s2",)])
        self.assertEqual(list(result), ["s1", "s2"])

    def test_dossier_known_oversize_is_split_signal_before_delegate(self) -> None:
        delegate = mock.Mock()
        guarded = admission._dossier_capacity_guard(delegate)
        with mock.patch.object(
            admission,
            "_routed_capacity_admitted",
            return_value=(False, {"estimated_request_tokens": 8166}),
        ):
            with self.assertRaisesRegex(
                dossier._DossierTransportPressure, "RUN122_FINAL_PAYLOAD_CAPACITY_PREFLIGHT"
            ):
                guarded("key", "prompt", "model", ["S1", "S2"])
        delegate.assert_not_called()

    def test_dossier_portable_shard_delegates_exactly_once(self) -> None:
        expected = {"S1": {"narration": "نص", "key_point": "فكرة"}}
        delegate = mock.Mock(return_value=expected)
        guarded = admission._dossier_capacity_guard(delegate)
        with mock.patch.object(
            admission,
            "_routed_capacity_admitted",
            return_value=(True, {"estimated_request_tokens": 7743}),
        ):
            result = guarded("key", "prompt", "model", ["S1"])
        self.assertEqual(result, expected)
        delegate.assert_called_once_with("key", "prompt", "model", ["S1"])

    def test_installer_changes_admission_only_and_wraps_current_schema_owner(self) -> None:
        old_installed = admission._INSTALLED
        old_capacity = batching._capacity_admitted
        old_dossier_call = dossier._one_schema_bounded_call
        current_schema_owner = mock.Mock()
        try:
            admission._INSTALLED = False
            dossier._one_schema_bounded_call = current_schema_owner
            admission.install_run122_final_payload_admission()
            self.assertIs(batching._capacity_admitted, admission._routed_capacity_admitted)
            self.assertTrue(
                getattr(dossier._one_schema_bounded_call, "_isco_run122_final_payload_admission", False)
            )
            self.assertIs(
                getattr(dossier._one_schema_bounded_call, "_isco_run122_delegate", None),
                current_schema_owner,
            )
        finally:
            batching._capacity_admitted = old_capacity
            dossier._one_schema_bounded_call = old_dossier_call
            admission._INSTALLED = old_installed

    def test_schema_bridge_installs_final_payload_guard_after_live_schema_owner(self) -> None:
        source = inspect.getsource(bridge.install_run120_schema_policy_bridge)
        bridge_assign = source.index("hardening._one_schema_bounded_call = _policy_owned_call")
        admission_call = source.index("_install_run122_final_payload_admission()")
        self.assertLess(bridge_assign, admission_call)

        helper_source = inspect.getsource(bridge._install_run122_final_payload_admission)
        self.assertIn("install_run122_final_payload_admission()", helper_source)


if __name__ == "__main__":
    unittest.main()
