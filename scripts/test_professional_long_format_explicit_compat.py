from __future__ import annotations

import unittest

from scripts.control_approved_brief import resolve_control_format


class ProfessionalLongFormatExplicitCompatibilityTests(unittest.TestCase):
    def test_existing_explicit_film_story_requests_remain_authoritative(self) -> None:
        base = {"kind": "long", "approved_topic": "أي موضوع"}
        self.assertEqual(resolve_control_format({**base, "format": "film"}), "film")
        self.assertEqual(resolve_control_format({**base, "format": "story"}), "story")


if __name__ == "__main__":
    unittest.main()
