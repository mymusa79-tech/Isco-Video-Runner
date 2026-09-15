from __future__ import annotations

import json
import unittest
from dataclasses import dataclass

import isco_video_agent.resilient_planner as staged

from scripts import structural_editorial_contract as structural
from scripts import word_band_repair_contract as word_band


@dataclass
class Section:
    id: str
    narration: str


def _section(section_id: str, words: int, *, rhetorical: bool = False) -> Section:
    tokens = [f"كلمة{section_id}_{index}" for index in range(words)]
    if rhetorical and tokens:
        tokens[0] = "سؤال؟"
    return Section(section_id, " ".join(tokens))


def _run265_shape(*, rhetorical_questions: int) -> list[Section]:
    # Exact aggregate from real Long Run #265 immediately after Script Doctor.
    counts = [92, 92, 92, 92, 92, 92, 92, 95]
    return [
        _section(
            f"sec_{index}",
            words,
            rhetorical=index <= rhetorical_questions,
        )
        for index, words in enumerate(counts, start=1)
    ]


class RhetoricalRepairClosureTests(unittest.TestCase):
    def test_run265_exact_739_word_six_question_shape_is_detected_pre_append(self) -> None:
        sections = _run265_shape(rhetorical_questions=6)
        self.assertEqual(sum(staged._word_count(s.narration) for s in sections), 739)
        self.assertTrue(word_band._pre_append_rhetorical_residual(sections))

    def test_doctor_guidance_requires_clearing_flag_not_merely_reducing_questions(self) -> None:
        guidance = structural._rhetorical_question_guidance()
        self.assertIn("exact detector flag is clear", guidance)
        self.assertIn("not an acceptable repair", guidance)
        self.assertIn("Do not introduce new ؟", guidance)

    def test_append_rule_is_projected_only_when_doctor_was_clear(self) -> None:
        base = {"tone": "calm", "visuals": {"x": 1}}
        clear_policy = json.loads(
            word_band._project_policy_json(
                json.dumps(base),
                prohibit_new_rhetorical_questions=True,
            )
        )
        residual_policy = json.loads(
            word_band._project_policy_json(
                json.dumps(base),
                prohibit_new_rhetorical_questions=False,
            )
        )
        self.assertIn("append_only_rhetorical_rule", clear_policy)
        self.assertNotIn("append_only_rhetorical_rule", residual_policy)
        self.assertNotIn("visuals", clear_policy)

    def test_append_cannot_reintroduce_question_mark_after_doctor_clears_flag(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "introduced Arabic question-mark text"):
            word_band._validate_append_rhetorical_regression(
                {"practice": "توسيع الفكرة مباشرة ثم ماذا لو عدنا للسؤال؟"},
                doctor_was_clear=True,
            )

    def test_existing_doctor_residual_is_diagnosed_without_inventing_second_repair_owner(self) -> None:
        # Case (A): measurement reports that Doctor still owns the unresolved defect.
        # Append validation must not pretend the append step introduced a pre-existing
        # flag; the unchanged final structural gate remains the fail-closed authority.
        sections = _run265_shape(rhetorical_questions=6)
        self.assertTrue(word_band._pre_append_rhetorical_residual(sections))
        word_band._validate_append_rhetorical_regression(
            {"practice": "إضافة تقريرية فقط"},
            doctor_was_clear=False,
        )

    def test_doctor_clear_shape_stays_clear_before_append(self) -> None:
        sections = _run265_shape(rhetorical_questions=0)
        self.assertEqual(sum(staged._word_count(s.narration) for s in sections), 739)
        self.assertFalse(word_band._pre_append_rhetorical_residual(sections))


if __name__ == "__main__":
    unittest.main()
