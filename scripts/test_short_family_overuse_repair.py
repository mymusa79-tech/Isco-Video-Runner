import copy
import unittest

from clean_v2.visual_story import (
    fallback_visual_story,
    repair_short_family_overuse,
    validate_visual_story,
)


def _plan():
    return {
        "title": "t",
        "promise": "p",
        "_short_visual_diversity_contract": "v1_max2",
        "sections": [
            {"id": "s1", "heading": "a", "purpose": "اسم العائق بوضوح", "visual_query_en": "hand writing in notebook on desk", "visual_query_alt_en": "closed door at end of quiet hallway"},
            {"id": "s2", "heading": "b", "purpose": "شرح الخطوة الصغيرة", "visual_query_en": "sticky notes on notebook page", "visual_query_alt_en": "kettle steaming on kitchen counter morning"},
            {"id": "s3", "heading": "c", "purpose": "النتيجة المرئية", "visual_query_en": "planner checklist with pen", "visual_query_alt_en": "sunlight across tidy empty desk"},
        ],
    }


class FamilyOveruseRepairTests(unittest.TestCase):
    def test_third_same_family_beat_is_switched_to_section_alternate(self):
        plan = _plan()
        story = fallback_visual_story(plan)
        with self.assertRaises(ValueError) as ctx:
            validate_visual_story(story, plan)
        self.assertIn("exceeds two beats", str(ctx.exception))
        repaired = repair_short_family_overuse(copy.deepcopy(story), plan)
        out = validate_visual_story(repaired, plan)
        queries = [b["stock_query_en"] for b in out["beats"]]
        self.assertEqual(len(set(queries)), 3)
        self.assertIn("sunlight across tidy empty desk", queries)

    def test_untouched_when_no_overuse(self):
        plan = _plan()
        plan["sections"][2]["visual_query_en"] = "sunlight across tidy empty desk"
        story = fallback_visual_story(plan)
        self.assertEqual(repair_short_family_overuse(story, plan), story)

    def test_no_qualifying_alternate_leaves_story_for_validator(self):
        plan = _plan()
        for sec in plan["sections"]:
            sec["visual_query_alt_en"] = "pen on notebook page"
        story = fallback_visual_story(plan)
        repaired = repair_short_family_overuse(story, plan)
        with self.assertRaises(ValueError):
            validate_visual_story(repaired, plan)


if __name__ == "__main__":
    unittest.main()
