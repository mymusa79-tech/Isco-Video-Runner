from __future__ import annotations

import inspect
import unittest

from scripts import gold_text_qc_pending_v1 as pending


class GoldTextNoQualityWeakeningTests(unittest.TestCase):
    def test_classifier_has_no_threshold_or_score_override(self) -> None:
        source = inspect.getsource(pending)
        for forbidden in (
            "_SCORE_MINIMUMS",
            "minimum_score",
            "threshold =",
            "status\"] = \"pass\"",
            "release_allowed = True",
        ):
            self.assertNotIn(forbidden, source)

    def test_classifier_only_promotes_explicit_last_provider_technical_error(self) -> None:
        source = inspect.getsource(pending._provider_review_with_qc_pending)
        self.assertIn('provider == "openrouter"', source)
        self.assertIn("provider_error is not None", source)
        self.assertIn("_ACTIVE_RELEASE_REVIEW.get()", source)


if __name__ == "__main__":
    unittest.main()
