from __future__ import annotations

import unittest
from unittest import mock

import isco_video_agent.resilient_planner as staged

from scripts import planning_envelope_preflight as preflight
from scripts import planning_outline_split_contract as split
from scripts import planning_stage_contract as stage_contract


def _core_payload(text: str) -> dict:
    narrative = next(iter(staged._NARRATIVE_FORMATS))
    return {
        "pillar": "understand",
        "hook": text,
        "title_options": ["a", "b", "c"],
        "thumbnail_concepts": ["a", "b", "c"],
        "cta": "cta",
        "closing_payoff": text,
        "narrative_format": narrative,
        "opener_variant": "fresh opener",
        "closer_variant": "fresh closer",
        "transition_variants": ["t1", "t2", "t3"],
        "editorial_intent": {
            "editorial_thesis": text,
            "viewer_starting_belief": text,
            "hidden_assumption": text,
            "editorial_turn": text,
            "stakes": text,
            "viewer_promise": text,
            "evidence_boundaries": [text],
            "earned_payoff": text,
        },
    }


class PlanningOutlinePortabilityBoundTests(unittest.TestCase):
    def test_preflight_fixture_tracks_same_hard_runtime_bound(self) -> None:
        premise = preflight._bounded_preflight_locked_premise()
        measured = split.locked_premise_utf8_bytes(premise)
        self.assertLessEqual(measured, split.LOCKED_PREMISE_MAX_UTF8_BYTES)
        self.assertGreaterEqual(
            measured,
            int(split.LOCKED_PREMISE_MAX_UTF8_BYTES * 0.90),
        )

    def test_oversized_core_is_rejected_not_truncated(self) -> None:
        spec = split.outline_core_stage_spec(6)
        contract = stage_contract.bind_request_contract(spec, "oversized-core")
        payload = _core_payload("م" * 700)
        self.assertGreater(
            split.locked_premise_utf8_bytes(payload),
            split.LOCKED_PREMISE_MAX_UTF8_BYTES,
        )
        with (
            mock.patch.object(staged, "validate_narrative_format", return_value=[]),
            mock.patch.object(staged, "validate_identity_phrases", return_value=[]),
            mock.patch.object(staged, "intent_from_dict", return_value=object()),
        ):
            with self.assertRaises(stage_contract.PlanningStageError) as caught:
                split._validate_core(payload, contract)
        self.assertEqual(caught.exception.code, stage_contract.PlanningErrorCode.SEMANTIC_INVALID)
        self.assertIn("locked_premise_portability_budget_exceeded", str(caught.exception))

    def test_normal_core_is_preserved_byte_for_byte(self) -> None:
        spec = split.outline_core_stage_spec(6)
        contract = stage_contract.bind_request_contract(spec, "normal-core")
        payload = _core_payload("مختصر")
        before = repr(payload)
        with (
            mock.patch.object(staged, "validate_narrative_format", return_value=[]),
            mock.patch.object(staged, "validate_identity_phrases", return_value=[]),
            mock.patch.object(staged, "intent_from_dict", return_value=object()),
        ):
            returned = split._validate_core(payload, contract)
        self.assertIs(returned, payload)
        self.assertEqual(repr(payload), before)


if __name__ == "__main__":
    unittest.main()
