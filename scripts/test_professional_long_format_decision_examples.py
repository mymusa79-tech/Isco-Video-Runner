from __future__ import annotations

import unittest

from scripts.control_approved_brief import resolve_control_format


POLICY = {
    "version": "professional_long_format_router_v1",
    "requested": "auto",
    "resolution_stage": "v4_before_approved_brief_binding",
}


class ProfessionalLongFormatDecisionExampleTests(unittest.TestCase):
    def _request(self, topic: str) -> dict:
        return {
            "kind": "long",
            "approved_topic": topic,
            "format": "auto",
            "format_policy": dict(POLICY),
            "research_pack": [{}, {}, {}],
        }

    def test_analysis_example_is_film(self) -> None:
        self.assertEqual(resolve_control_format(self._request("لماذا نفقد الدافع بعد بداية قوية؟")), "film")

    def test_story_example_is_story(self) -> None:
        self.assertEqual(resolve_control_format(self._request("قصة الرجل الذي انتظر سنوات ثم بدأ من جديد")), "story")


if __name__ == "__main__":
    unittest.main()
