from __future__ import annotations

import inspect
import unittest
from dataclasses import dataclass

from scripts import structural_editorial_contract as contract


@dataclass
class Section:
    id: str
    narration: str


@dataclass
class Plan:
    sections: list[Section]


def _pad(text: str, words: int = 120) -> str:
    current = text.split()
    filler = [f"تفصيل{i}" for i in range(max(0, words - len(current)))]
    return " ".join(current + filler)


class StructuralEditorialContractTests(unittest.TestCase):
    def test_clean_sections_do_not_create_a_doctor_issue(self) -> None:
        sections = [
            Section("s1", _pad("مقدمة طبيعية واضحة.")),
            Section("s2", _pad("شرح عملي مختلف.")),
        ]

        def base(items, *, minimum, maximum, target):
            self.assertIs(items, sections)
            return []

        wrapped = contract._with_structural_issue_notes(base)
        self.assertEqual(wrapped(sections, minimum=110, maximum=170, target=120), [])

    def test_run109_repeated_not_x_but_y_promotes_existing_script_doctor(self) -> None:
        sections = [
            Section("s1", _pad("ليس الخلل في الوقت بل في طريقة توزيعه.")),
            Section("s2", _pad("ليست المشكلة في الإرادة بل في الاحتكاك اليومي.")),
            Section("s3", _pad("ليس الحل جدولًا أقسى بل قرارًا أبسط.")),
        ]

        def base(items, *, minimum, maximum, target):
            return ["existing deterministic issue"]

        wrapped = contract._with_structural_issue_notes(base)
        notes = wrapped(sections, minimum=110, maximum=170, target=120)
        self.assertEqual(notes[0], "existing deterministic issue")
        self.assertEqual(len(notes), 2)
        self.assertIn("repeated_not_x_but_y", notes[1])
        self.assertIn("existing Script Doctor", inspect.getdoc(contract) or "")

    def test_final_gate_accepts_plan_after_same_detector_is_clear(self) -> None:
        clean = Plan([
            Section("s1", _pad("مقدمة واضحة ذات معنى محدد.")),
            Section("s2", _pad("تفصيل ثان يطور الفكرة من زاوية أخرى.")),
        ])
        wrapped = contract._with_structural_final_gate(lambda: clean)
        self.assertIs(wrapped(), clean)

    def test_final_gate_fails_closed_if_bounded_doctor_did_not_clear_flags(self) -> None:
        bad = Plan([
            Section("s1", _pad("ليس أ بل ب.")),
            Section("s2", _pad("ليس ج بل د.")),
            Section("s3", _pad("ليس هـ بل و.")),
        ])
        wrapped = contract._with_structural_final_gate(lambda: bad)
        with self.assertRaisesRegex(RuntimeError, "repeated_not_x_but_y"):
            wrapped()

    def test_run249_excessive_rhetorical_questions_gets_a_plain_language_explanation(self) -> None:
        # Run #249 (real production log, Long/film): Doctor got only the bare machine
        # label "excessive_rhetorical_questions" and still failed to clear it in its
        # one bounded pass. This proves the explanatory note is now added alongside
        # the existing generic note, without replacing it.
        sections = [
            Section("s1", _pad("هل تعلم أن الوقت يمر؟ هل فكرت يومًا في هذا؟")),
            Section("s2", _pad("ألا تشعر أحيانًا بالتعب؟ ألا يكفي هذا سببًا؟")),
            Section("s3", _pad("أليس من الأفضل أن نبدأ؟ ماذا لو حاولنا الآن؟")),
        ]

        def base(items, *, minimum, maximum, target):
            return []

        wrapped = contract._with_structural_issue_notes(base)
        notes = wrapped(sections, minimum=110, maximum=170, target=120)
        self.assertEqual(len(notes), 2)
        self.assertIn("excessive_rhetorical_questions", notes[0])
        self.assertIn("excessive_rhetorical_questions", notes[1])
        self.assertIn("؟", notes[1])
        self.assertIn("direct statement", notes[1])

    def test_rhetorical_question_guidance_never_states_a_numeric_threshold_or_count(self) -> None:
        # The detector's threshold and counting method belong to Engine only. This
        # guidance must never duplicate either, or it risks silently drifting from the
        # real detector (guarded structurally by test_structural_editorial_contract_
        # no_threshold_drift.py's forbidden-token scan on the whole module source).
        text = contract._rhetorical_question_guidance()
        for token in ("6", "5", "threshold", "ceiling", "count is"):
            self.assertNotIn(token, text)

    def test_other_flags_are_unaffected_by_the_rhetorical_question_addition(self) -> None:
        sections = [
            Section("s1", _pad("ليس الخلل في الوقت بل في طريقة توزيعه.")),
            Section("s2", _pad("ليست المشكلة في الإرادة بل في الاحتكاك اليومي.")),
            Section("s3", _pad("ليس الحل جدولًا أقسى بل قرارًا أبسط.")),
        ]

        def base(items, *, minimum, maximum, target):
            return []

        wrapped = contract._with_structural_issue_notes(base)
        notes = wrapped(sections, minimum=110, maximum=170, target=120)
        self.assertEqual(len(notes), 1)
        self.assertNotIn("excessive_rhetorical_questions", notes[0])

    def test_contract_adds_no_provider_or_retry_owner(self) -> None:
        source = inspect.getsource(contract)
        forbidden = (
            "json_text(",
            "task_router(",
            "openrouter",
            "groq",
            "gemini",
            "sleep(",
            "max_attempts",
        )
        for token in forbidden:
            self.assertNotIn(token, source)


if __name__ == "__main__":
    unittest.main()
