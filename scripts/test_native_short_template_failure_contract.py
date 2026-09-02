from __future__ import annotations

import unittest
from unittest import mock

from scripts import native_short_planner_router as router


class NativeShortTemplateFailureContractTests(unittest.TestCase):
    def test_internal_pillar_failure_never_becomes_plausible_why_reframe_choice(self):
        # Deliberately use a topic with none of the weighted template signals so the
        # deterministic pillar fallback is the only legitimate next step.
        topic = "موضوع محايد بلا إشارات مصنفة"
        with mock.patch.object(router.native_short, "choose_pillar", side_effect=KeyError("broken routing")):
            with self.assertRaisesRegex(
                router.NativeShortPlannerError,
                "native_short_template_fallback_failed",
            ):
                router.select_native_short_template(topic)


if __name__ == "__main__":
    unittest.main()
