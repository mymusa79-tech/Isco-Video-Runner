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
