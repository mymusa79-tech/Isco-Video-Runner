from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import isco_video_agent.resilient_planner as staged
from scripts import append_retry_guard as append_guard
from scripts.word_band_repair_contract import install_word_band_repair_contract


class WordBandRepairContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self._doctor = staged._script_doctor
        self._retry = staged._script_doctor_underlength_retry
        self._repair = append_guard._repair_all_residual_underlength

    def tearDown(self) -> None:
        staged._script_doctor = self._doctor
        staged._script_doctor_underlength_retry = self._retry
        append_guard._repair_all_residual_underlength = self._repair

    @staticmethod
    def _sections(counts: list[int]) -> list[staged.ScriptSection]:
        return [
            staged.ScriptSection(
                id=f"sec_{index}",
                narration=" ".join([f"كلمة{index}"] * words),
                visual_query="room notebook",
                key_point=f"distinct key point {index}",
            )
            for index, words in enumerate(counts, start=1)
        ]

    @staticmethod
    def _doctor_result(counts: list[int]) -> dict[str, dict]:
        return {
            f"sec_{index}": {
                "narration": " ".join([f"تحرير{index}"] * words),
                "key_point": f"edited key point {index}",
            }
            for index, words in enumerate(counts, start=1)
        }

    def _call_doctor(self, sections):
        return staged._script_doctor(
            "key",
            topic="topic",
            model="model",
            sections=sections,
            policy_json="{}",
            research_json="{}",
            editorial_intent_json="{}",
            narrative_format="problem_reveal_solution",
            issue_notes="- test",
            identity_opener="",
            identity_closer="",
        )

    def test_film_doctor_cannot_create_new_word_band_defect(self) -> None:
        sections = self._sections([120] * 8)
        calls = 0

        def fake_doctor(*args, **kwargs):
            nonlocal calls
            del args, kwargs
            calls += 1
            return self._doctor_result([120, 95, 120, 120, 120, 120, 120, 120])

        staged._script_doctor = fake_doctor
        install_word_band_repair_contract()
        corrected = self._call_doctor(sections)

        self.assertEqual(calls, 1)
        self.assertEqual(staged._word_count(corrected["sec_2"]["narration"]), 120)
        self.assertEqual(corrected["sec_2"]["key_point"], "edited key point 2")
        self.assertEqual(staged._word_count(corrected["sec_1"]["narration"]), 120)

    def test_existing_underfloor_section_may_be_fixed_by_doctor(self) -> None:
        sections = self._sections([120, 100, 120, 120, 120, 120, 120, 120])

        def fake_doctor(*args, **kwargs):
            del args, kwargs
            return self._doctor_result([120, 115, 120, 120, 120, 120, 120, 120])

        staged._script_doctor = fake_doctor
        install_word_band_repair_contract()
        corrected = self._call_doctor(sections)

        self.assertEqual(staged._word_count(corrected["sec_2"]["narration"]), 115)

    def test_run255_successor_797_shape_uses_small_local_context_same_stage_owner(self) -> None:
        counts = [100, 100, 100, 100, 100, 100, 100, 97]
        self.assertEqual(sum(counts), 797)
        sections = self._sections(counts)
        prompts: list[str] = []

        policy = {
            "audience": "Arabic audience",
            "language": {"register": "MSA", "avoid": ["generic filler"]},
            "values": {"respect_islam": True, "rules": ["No fabricated religious quotes"]},
            "visuals": {"rules": ["VISUAL_SENTINEL_" * 1200]},
            "audio": {"rules": ["AUDIO_SENTINEL_" * 1200]},
            "brand_signature": {"opener": "BRAND_SENTINEL_" * 1200},
            "release_gate": {"reject_on_unverified_religious_quote": True},
        }
        research = {
            "approved_research_pack": "RESEARCH_SENTINEL_" * 5000,
            "market_signals": {"grounded_research": "MARKET_SENTINEL_" * 5000},
            "factuality_rule": "Do not introduce claims not already supported.",
            "content_boundaries": ["no medical diagnosis"],
        }

        def fake_json(api_key, prompt, model):
            del api_key, model
            prompts.append(prompt)
            return {
                "additions": [
                    {
                        "id": f"sec_{index}",
                        "append_text": " ".join(["إضافة"] * 15),
                    }
                    for index in range(1, 9)
                ]
            }

        install_word_band_repair_contract()
        with patch.object(staged, "json_text", side_effect=fake_json):
            additions = staged._script_doctor_underlength_retry(
                "key",
                topic="topic",
                model="model",
                sections=sections,
                policy_json=json.dumps(policy, ensure_ascii=False),
                research_json=json.dumps(research, ensure_ascii=False),
                editorial_intent_json=json.dumps(
                    {
                        "viewer_promise": "promise",
                        "editorial_turn": "turn",
                        "evidence_boundaries": ["no new facts"],
                        "earned_payoff": "payoff",
                    },
                    ensure_ascii=False,
                ),
                narrative_format="problem_reveal_solution",
                current_words=797,
                minimum=800,
            )

        self.assertEqual(len(prompts), 1)
        prompt = prompts[0]
        self.assertNotIn("VISUAL_SENTINEL_", prompt)
        self.assertNotIn("AUDIO_SENTINEL_", prompt)
        self.assertNotIn("BRAND_SENTINEL_", prompt)
        self.assertNotIn("RESEARCH_SENTINEL_", prompt)
        self.assertNotIn("MARKET_SENTINEL_", prompt)
        self.assertIn("factuality_rule", prompt)
        self.assertIn("content_boundaries", prompt)
        self.assertIn("Do not introduce any new externally verifiable", prompt)
        self.assertLess(len(prompt.encode("utf-8")), 35_000)
        self.assertEqual(list(additions), [f"sec_{index}" for index in range(1, 9)])

    def test_projection_does_not_create_an_extra_provider_retry(self) -> None:
        sections = self._sections([120, 100, 120, 120, 120, 120, 120, 120])
        calls = 0

        def fake_json(api_key, prompt, model):
            nonlocal calls
            del api_key, prompt, model
            calls += 1
            return {
                "additions": [
                    {"id": "sec_2", "append_text": " ".join(["إضافة"] * 15)}
                ]
            }

        install_word_band_repair_contract()
        with patch.object(staged, "json_text", side_effect=fake_json):
            result = staged._script_doctor_underlength_retry(
                "key",
                topic="topic",
                model="model",
                sections=sections,
                policy_json="{}",
                research_json="{}",
                narrative_format="problem_reveal_solution",
                current_words=sum([120, 100, 120, 120, 120, 120, 120, 120]),
                minimum=800,
            )

        self.assertEqual(calls, 1)
        self.assertEqual(list(result), ["sec_2"])


if __name__ == "__main__":
    unittest.main()
