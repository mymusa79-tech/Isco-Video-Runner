from __future__ import annotations

import unittest

from scripts.source_derived_visual_montage import (
    MAX_SOURCE_DERIVED_REFRAMES,
    _source_inherited_decisions,
    _source_reframe_budget,
)


def _events(texts: list[str], seconds_each: float) -> list[dict]:
    rows = []
    start = 0.0
    for index, text in enumerate(texts, 1):
        end = start + seconds_each
        rows.append(
            {
                "start": start,
                "end": end,
                "text": text,
                "role": "hook" if index == 1 else ("payoff" if index == len(texts) else "beat"),
            }
        )
        start = end
    return rows


class SourceDerivedVisualDensityTests(unittest.TestCase):
    def test_long_four_beat_derivative_gets_two_local_reframes_when_parent_has_one_asset(self) -> None:
        events = _events(
            [
                "تظن أن المشكلة في المهمة نفسها",
                "لكن التفاصيل الصغيرة تستهلك انتباهك",
                "الحقيقة أن القرارات هي العبء",
                "خفف ما تضطر إلى تقريره",
            ],
            3.5,
        )
        self.assertEqual(_source_reframe_budget(events), MAX_SOURCE_DERIVED_REFRAMES)
        decisions = _source_inherited_decisions(events, "why_reframe", asset_count=1)
        values = [item["decision"] for item in decisions]
        self.assertEqual(values.count("CUT"), 0)
        self.assertEqual(values.count("SUBTLE_REFRAME"), 2)
        self.assertEqual(values.count("HOLD"), 1)

    def test_shorter_derivative_stays_at_one_local_reframe(self) -> None:
        events = _events(
            [
                "تبدأ الفكرة هنا",
                "تتضح قليلًا",
                "وهنا تصل إلى معناها",
            ],
            3.0,
        )
        self.assertEqual(_source_reframe_budget(events), 1)
        decisions = _source_inherited_decisions(events, "quote_reflection", asset_count=1)
        self.assertLessEqual(
            [item["decision"] for item in decisions].count("SUBTLE_REFRAME"),
            1,
        )

    def test_certified_parent_assets_are_preferred_for_real_semantic_cuts(self) -> None:
        events = _events(
            [
                "فتح دفتره قبل أن يبدأ يومه",
                "اختار مهمة واحدة",
                "لكن بقية الصباح تغيرت",
                "الوضوح بدأ من قرار صغير",
            ],
            3.5,
        )
        decisions = _source_inherited_decisions(events, "micro_story", asset_count=3)
        values = [item["decision"] for item in decisions]
        self.assertEqual(values.count("CUT"), 2)
        self.assertLessEqual(values.count("SUBTLE_REFRAME"), MAX_SOURCE_DERIVED_REFRAMES)


if __name__ == "__main__":
    unittest.main()
