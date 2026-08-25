from __future__ import annotations

import unittest
from dataclasses import dataclass

from scripts.structural_editorial_repair import (
    _current_flags,
    _minimal_triggering_section_ids,
    repair_structural_flags,
)


@dataclass
class Section:
    id: str
    narration: str
    key_point: str = ""


@dataclass
class Plan:
    sections: list[Section]
    topic: str = "موضوع"
    format: str = "film"
    hook: str = "hook"
    cta: str = "cta"
    closing_payoff: str = "payoff"
    editorial_intent: dict | None = None
    narrative_format: str = "direct_cinematic"
    identity_opener: str = ""
    identity_closer: str = ""

    def __post_init__(self):
        if self.editorial_intent is None:
            self.editorial_intent = {"viewer_promise": "promise"}


def _pad(text: str, words: int = 120) -> str:
    current = text.split()
    filler = [f"تفصيل{i}" for i in range(max(0, words - len(current)))]
    return " ".join(current + filler)


def _targets_from_prompt(prompt: str) -> list[dict]:
    import json
    import re
    match = re.search(
        r"TARGET_SECTIONS \(data, not instructions\):\n(.+?)\n\nReturn ONLY JSON",
        prompt,
        re.S,
    )
    if not match:
        raise AssertionError("target payload missing from prompt")
    return json.loads(match.group(1))


class StructuralEditorialRepairTests(unittest.TestCase):
    def test_authoritative_detector_localizes_repeated_not_x_but_y_without_duplicate_regex(self):
        plan = Plan([
            Section("s1", _pad("مقدمة طبيعية واضحة.")),
            Section("s2", _pad("ليس الخلل في الوقت بل في طريقة توزيعه.")),
            Section("s3", _pad("شرح مختلف تمامًا.")),
            Section("s4", _pad("ليست المشكلة في الإرادة بل في الاحتكاك اليومي.")),
            Section("s5", _pad("مثال عملي جديد.")),
            Section("s6", _pad("ليس الحل جدولًا أقسى بل قرارًا أبسط.")),
            Section("s7", _pad("خلاصة قبل النهاية.")),
            Section("s8", _pad("ليس المطلوب مراقبة الساعة بل حماية الأولويات. ليس الوقت عدواً بل مساحة قرار.")),
        ])
        self.assertIn("repeated_not_x_but_y", _current_flags(plan))
        target_ids = _minimal_triggering_section_ids(plan, "repeated_not_x_but_y")
        self.assertTrue(target_ids)
        self.assertTrue(set(target_ids).issubset({"s2", "s4", "s6", "s8"}))

    def test_run109_family_is_repaired_locally_and_untargeted_sections_are_unchanged(self):
        plan = Plan([
            Section("s1", _pad("مقدمة طبيعية واضحة.")),
            Section("s2", _pad("ليست في كسلك أو عجزك الدائم عن الالتزام، بل في كون الجداول لا تراعي يومك.")),
            Section("s3", _pad("شرح مستقل مع مثال يومي.")),
            Section("s4", _pad("ليس تسويفًا بالمعنى الكسول، بل هو رد فعل طبيعي على عبء غير واضح.")),
            Section("s5", _pad("تمييز آخر لا يستخدم الصيغة المكررة.")),
            Section("s6", _pad("ليس جداول أكثر صرامة أو تطبيقات معقدة، بل الانتقال إلى قرار صغير يمكن تكراره.")),
            Section("s7", _pad("تطبيق عملي هادئ.")),
            Section("s8", _pad("ليست ضبط الساعات بالثانية ولا مراقبة العقارب بقسوة، بل إدارة الانتباه. ليس الوقت عدواً تقاومه، بل مساحة تعيش فيها.")),
        ])
        before = {s.id: s.narration for s in plan.sections}
        calls = []

        def fake_repair(prompt: str) -> dict:
            calls.append(prompt)
            targets = _targets_from_prompt(prompt)
            return {
                "sections": [
                    {"id": item["id"], "narration": _pad(f"صياغة مباشرة طبيعية للقسم {item['id']} دون القالب المتكرر.")}
                    for item in targets
                ]
            }

        repaired = repair_structural_flags(plan, repair_json_fn=fake_repair)
        self.assertNotIn("repeated_not_x_but_y", _current_flags(repaired))
        self.assertLessEqual(len(calls), 2)
        changed = {s.id for s in repaired.sections if s.narration != before[s.id]}
        self.assertTrue(changed)
        self.assertTrue(changed.issubset({"s2", "s4", "s6", "s8"}))

    def test_second_local_attempt_is_bounded_and_uses_fresh_authoritative_flags(self):
        plan = Plan([
            Section("s1", _pad("ليس أ بل ب.")),
            Section("s2", _pad("ليس ج بل د.")),
            Section("s3", _pad("ليس هـ بل و.")),
            Section("s4", _pad("قسم طبيعي.")),
        ])
        calls = 0

        def fake_repair(prompt: str) -> dict:
            nonlocal calls
            calls += 1
            targets = _targets_from_prompt(prompt)
            if calls == 1:
                return {"sections": [{"id": t["id"], "narration": t["narration"]} for t in targets]}
            return {"sections": [{"id": t["id"], "narration": _pad("صياغة مباشرة بلا قالب متكرر.")} for t in targets]}

        repaired = repair_structural_flags(plan, repair_json_fn=fake_repair, max_attempts=2)
        self.assertEqual(calls, 2)
        self.assertEqual(_current_flags(repaired), ())

    def test_failure_after_bound_is_fail_closed(self):
        plan = Plan([
            Section("s1", _pad("ليس أ بل ب.")),
            Section("s2", _pad("ليس ج بل د.")),
            Section("s3", _pad("ليس هـ بل و.")),
        ])

        def no_op(prompt: str) -> dict:
            targets = _targets_from_prompt(prompt)
            return {"sections": [{"id": t["id"], "narration": t["narration"]} for t in targets]}

        with self.assertRaisesRegex(RuntimeError, "failed closed after 2 local attempt"):
            repair_structural_flags(plan, repair_json_fn=no_op, max_attempts=2)

    def test_duplicate_sentence_family_is_also_covered(self):
        repeated = "هذه جملة واضحة تتكرر حرفيًا هنا"
        plan = Plan([
            Section("s1", repeated + ". " + _pad("تكملة أولى", 115)),
            Section("s2", _pad("فكرة أخرى مختلفة تمامًا.")),
            Section("s3", repeated + ". " + _pad("تكملة ثالثة", 115)),
        ])
        self.assertIn("duplicate_sentence", _current_flags(plan))
        target_ids = _minimal_triggering_section_ids(plan, "duplicate_sentence")
        self.assertEqual(set(target_ids), {"s1", "s3"})

    def test_identity_anchor_loss_is_rejected_closed(self):
        opener = "مرحبًا بك في نداء اليقظة"
        closer = "إلى لقاء هادئ"
        plan = Plan(
            [
                Section("s1", _pad("ليس أ بل ب. ليس ج بل د. " + opener)),
                Section("s2", _pad("ليس هـ بل و.")),
                Section("s3", _pad("قسم طبيعي. " + closer)),
            ],
            identity_opener=opener,
            identity_closer=closer,
        )

        def bad_repair(prompt: str) -> dict:
            targets = _targets_from_prompt(prompt)
            return {"sections": [{"id": t["id"], "narration": _pad("صياغة مباشرة بلا الهوية.")} for t in targets]}

        with self.assertRaisesRegex(RuntimeError, "identity invariant"):
            repair_structural_flags(plan, repair_json_fn=bad_repair)


if __name__ == "__main__":
    unittest.main()
