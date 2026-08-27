from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from scripts import run120_dossier_repair_hardening as hardening
from scripts import run120_schema_policy_bridge as bridge


def _estimate(total: int) -> dict:
    return {
        "estimated_prompt_tokens": max(1, total - 2650),
        "reserved_completion_tokens": 2400,
        "token_safety_reserve": 250,
        "estimated_request_tokens": total,
        "provider_tpm_limit": 8000,
        "contract": "full_script",
    }


def _section(section_id: str):
    return SimpleNamespace(
        id=section_id,
        narration=(f"نص {section_id} " * 120).strip(),
        key_point=f"فكرة {section_id}",
    )


def _plan(count: int = 2):
    return SimpleNamespace(
        topic="موضوع الاختبار",
        format="film",
        sections=[_section(f"S{i}") for i in range(1, count + 1)],
        identity_opener="",
        identity_closer="",
        narrative_format="direct_cinematic",
        editorial_intent={"editorial_thesis": "thesis"},
    )


class Run122DossierCapacityPreflightTests(unittest.TestCase):
    def test_oversized_pair_is_rejected_before_schema_router(self):
        schema_owner = mock.Mock()
        with mock.patch.object(
            bridge.capacity, "groq_capacity_estimate", return_value=_estimate(8077)
        ), mock.patch.object(
            bridge.staged, "_call_with_schema_repair", schema_owner
        ):
            with self.assertRaises(hardening._DossierTransportPressure) as caught:
                bridge._policy_owned_call("k", "prompt", "m", ["S1", "S2"])

        self.assertIn("DOSSIER_TPM_CAPACITY_PREFLIGHT", str(caught.exception))
        schema_owner.assert_not_called()

    def test_portable_pair_keeps_existing_schema_owner(self):
        expected = {
            "S1": {"narration": "واحد", "key_point": "أ"},
            "S2": {"narration": "اثنان", "key_point": "ب"},
        }
        schema_owner = mock.Mock(return_value=expected)
        with mock.patch.object(
            bridge.capacity, "groq_capacity_estimate", return_value=_estimate(7600)
        ), mock.patch.object(
            bridge.staged, "_call_with_schema_repair", schema_owner
        ):
            result = bridge._policy_owned_call("k", "prompt", "m", ["S1", "S2"])

        self.assertEqual(result, expected)
        schema_owner.assert_called_once()
        self.assertEqual(schema_owner.call_args.kwargs["expected_ids"], ["S1", "S2"])

    def test_oversized_single_fails_closed_before_provider(self):
        schema_owner = mock.Mock()
        with mock.patch.object(
            bridge.capacity, "groq_capacity_estimate", return_value=_estimate(8100)
        ), mock.patch.object(
            bridge.staged, "_call_with_schema_repair", schema_owner
        ):
            with self.assertRaises(hardening._DossierTransportPressure):
                bridge._policy_owned_call("k", "prompt", "m", ["S1"])
        schema_owner.assert_not_called()

    def test_repair_transport_splits_unsafe_pair_before_any_pair_provider_attempt(self):
        plan = _plan(2)
        provider_ids: list[list[str]] = []
        estimates = iter([_estimate(8200), _estimate(6200), _estimate(6300)])

        def fake_schema_owner(_key, _prompt, _model, *, expected_ids):
            ids = list(expected_ids)
            provider_ids.append(ids)
            return {
                section_id: {
                    "narration": (f"مصَحح {section_id} " * 120).strip(),
                    "key_point": f"مصَحح {section_id}",
                }
                for section_id in ids
            }

        engine_stubs = mock.patch.multiple(
            hardening.staged,
            load_editorial_policy=mock.Mock(return_value={"brand_signature": {}}),
            _writer_policy_json=mock.Mock(return_value="{}"),
            _compact_planning_policy_json=mock.Mock(side_effect=lambda value: value),
            _compact_planning_research_json=mock.Mock(side_effect=lambda value: value),
            _strip_host_managed_phrases=mock.Mock(),
            _apply_brand_signature=mock.Mock(),
            _assert_brand_signature_invariant=mock.Mock(),
            _reject_unverified_religious_quotes=mock.Mock(),
            _strip_exact_host_phrase=mock.Mock(side_effect=lambda text, phrase: text),
        )

        original_call = hardening._one_schema_bounded_call
        try:
            hardening._one_schema_bounded_call = bridge._policy_owned_call
            with engine_stubs, mock.patch.object(
                bridge.capacity, "groq_capacity_estimate", side_effect=lambda _prompt: next(estimates)
            ), mock.patch.object(
                bridge.staged, "_call_with_schema_repair", side_effect=fake_schema_owner
            ):
                repaired = hardening._repair_existing_plan(
                    plan,
                    "- [tone] naturalness_flag",
                    api_key="k",
                    topic=plan.topic,
                    requested_format="film",
                    content_model="m",
                    research_context={},
                )
        finally:
            hardening._one_schema_bounded_call = original_call

        # The unsafe pair never reaches the provider/schema owner; only its singles do.
        self.assertEqual(provider_ids, [["S1"], ["S2"]])
        self.assertTrue(repaired.sections[0].narration.startswith("مصَحح S1"))
        self.assertTrue(repaired.sections[1].narration.startswith("مصَحح S2"))


if __name__ == "__main__":
    unittest.main()
