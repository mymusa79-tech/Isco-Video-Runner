from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace

from scripts import structural_editorial_contract as contract


class RhetoricalRepairClosureTests(unittest.TestCase):
    @staticmethod
    def _film_sections(*, total_words: int = 739, question_marks: int = 6) -> list[SimpleNamespace]:
        """Build the Run #265 shape: eight Film sections and an exact total word count."""
        if total_words < 8:
            raise ValueError("total_words must allow at least one token per section")
        base, remainder = divmod(total_words, 8)
        sections: list[SimpleNamespace] = []
        for index in range(8):
            count = base + (1 if index < remainder else 0)
            words = [f"كلمة{index}_{word}" for word in range(count)]
            narration = " ".join(words)
            if index < question_marks:
                narration += "؟"
            sections.append(
                SimpleNamespace(
                    id=f"sec_{index + 1}",
                    narration=narration,
                    key_point=f"نقطة {index + 1}",
                )
            )
        return sections

    def test_run265_shape_is_exactly_739_words_and_six_questions(self) -> None:
        sections = self._film_sections(total_words=739, question_marks=6)

        self.assertEqual(
            sum(len(section.narration.split()) for section in sections),
            739,
        )
        self.assertEqual(contract._rhetorical_question_count(sections), 6)
        self.assertIn(
            contract._RHETORICAL_QUESTIONS_FLAG,
            contract._current_flags(sections),
        )

    def test_pre_append_probe_identifies_residual_doctor_failure_without_extra_call(self) -> None:
        sections = self._film_sections(total_words=739, question_marks=6)
        calls = 0

        def existing_append_owner(*args, **kwargs):
            nonlocal calls
            calls += 1
            self.assertIs(kwargs["sections"], sections)
            return {"sec_1": "إضافة مباشرة تحافظ على الفكرة من دون استفهام جديد"}

        wrapped = contract._with_pre_append_probe(existing_append_owner)
        output = io.StringIO()
        with redirect_stdout(output):
            additions = wrapped(
                "api-key",
                topic="موضوع تجريبي",
                model="model",
                sections=sections,
                policy_json="{}",
                research_json="{}",
                narrative_format="direct_cinematic",
                current_words=739,
                minimum=800,
            )

        self.assertEqual(calls, 1)
        self.assertEqual(
            additions,
            {"sec_1": "إضافة مباشرة تحافظ على الفكرة من دون استفهام جديد"},
        )
        log = output.getvalue()
        self.assertIn("boundary=post_script_doctor_pre_append", log)
        self.assertIn("rhetorical_questions=6", log)
        self.assertIn("excessive_rhetorical_questions=true", log)
        self.assertIn("introduced_question_marks=0", log)

    def test_append_only_cannot_reintroduce_question_mark(self) -> None:
        sections = self._film_sections(total_words=739, question_marks=0)
        calls = 0

        def existing_append_owner(*args, **kwargs):
            nonlocal calls
            calls += 1
            return {"sec_1": "تفصيل جديد مفيد، لكن لماذا يحدث هذا؟"}

        wrapped = contract._with_pre_append_probe(existing_append_owner)
        with self.assertRaisesRegex(
            RuntimeError,
            "append-only guard blocked new rhetorical questions",
        ):
            wrapped(
                "api-key",
                topic="موضوع تجريبي",
                model="model",
                sections=sections,
                policy_json="{}",
                research_json="{}",
                narrative_format="direct_cinematic",
                current_words=739,
                minimum=800,
            )

        self.assertEqual(calls, 1)

    def test_doctor_guidance_requires_declarative_rewrite_not_punctuation_deletion(self) -> None:
        guidance = contract._rhetorical_question_guidance()

        self.assertIn("direct declarative sentence", guidance)
        self.assertIn("do not merely remove the punctuation", guidance)
        self.assertIn("at most one genuinely necessary viewer-facing question", guidance)
        self.assertIn("final acceptance requirement", guidance)


if __name__ == "__main__":
    unittest.main()
