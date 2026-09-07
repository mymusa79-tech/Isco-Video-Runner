from __future__ import annotations

import unittest

from scripts.short_human_editorial_montage import plan_editorial_boundaries


def _events(texts: list[str]) -> list[dict]:
    rows = []
    start = 0.0
    for index, text in enumerate(texts, 1):
        end = start + 3.0
        rows.append(
            {
                "start": start,
                "end": end,
                "text": text,
                "role": "hook" if index == 1 else "beat",
            }
        )
        start = end
    return rows


class ShortHumanEditorialMontageTests(unittest.TestCase):
    def test_why_reframe_cuts_on_meaningful_turn_not_every_beat(self) -> None:
        segments, decisions = plan_editorial_boundaries(
            _events([
                "تظن أن المشكلة في المهمة نفسها",
                "تفاصيل صغيرة تستهلك انتباهك",
                "لكن المشكلة في عدد القرارات",
                "خفف ما تضطر إلى تقريره",
            ]),
            "why_reframe",
        )
        self.assertEqual([item["decision"] for item in decisions], ["HOLD", "CUT", "CUT"])
        self.assertEqual(len(segments), 3)
        self.assertEqual(segments[0]["_covered_beat_ids"], ["b01", "b02"])

    def test_inner_dialogue_uses_local_reframe_without_new_asset_boundary(self) -> None:
        segments, decisions = plan_editorial_boundaries(
            _events([
                "لماذا أشعر أنني متأخر؟",
                "أراجع كل خطوة وكأنها امتحان",
                "ربما أحتاج أن أرى التقدم بهدوء",
                "يكفي أن أعرف خطوتي التالية",
            ]),
            "inner_dialogue",
        )
        self.assertEqual(
            [item["decision"] for item in decisions],
            ["HOLD", "SUBTLE_REFRAME", "CUT"],
        )
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0]["_covered_beat_ids"], ["b01", "b02", "b03"])
        self.assertEqual(segments[0]["_reframe_beat_ids"], ["b03"])

    def test_micro_story_keeps_scene_progression_but_restrains_middle_boundary(self) -> None:
        segments, decisions = plan_editorial_boundaries(
            _events([
                "فتح دفتره قبل أن يبدأ يومه",
                "اختار مهمة واحدة",
                "صار بقية الصباح أوضح",
                "الوضوح بدأ من قرار صغير",
            ]),
            "micro_story",
        )
        self.assertEqual(
            [item["decision"] for item in decisions],
            ["CUT", "SUBTLE_REFRAME", "CUT"],
        )
        self.assertEqual(len(segments), 3)

    def test_two_beat_short_preserves_independent_payoff_asset(self) -> None:
        segments, decisions = plan_editorial_boundaries(
            _events(["تبدأ الفكرة هنا", "وهنا تصل إلى معناها"]),
            "quote_reflection",
        )
        self.assertEqual([item["decision"] for item in decisions], ["CUT"])
        self.assertEqual(len(segments), 2)


if __name__ == "__main__":
    unittest.main()
