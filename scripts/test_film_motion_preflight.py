from __future__ import annotations

import unittest

from clean_v2.simple_mode import film_motion_preflight


class FilmMotionPreflightTests(unittest.TestCase):
    def test_film_with_zero_stock_after_eighteen_requests_must_not_render(self) -> None:
        rights = [
            {"beat_id": f"scene{i:02d}", "stock_requested": True,
             "source_preference": "stock_motion", "source_actual": "owner_art",
             "replacement_reason": "visual_review_unavailable"}
            for i in range(1, 19)
        ]
        verdict = film_motion_preflight(rights)
        self.assertEqual(verdict["status"], "needs_verified_motion")
        self.assertEqual(verdict["requested_motion_scenes"], 18)
        self.assertEqual(verdict["verified_or_owner_selected_motion_scenes"], 0)
        self.assertEqual(verdict["minimum_motion_scenes"], 5)

    def test_accepted_real_motion_allows_render(self) -> None:
        rights = [
            {"stock_requested": True, "source_preference": "stock_motion",
             "source_actual": "stock_motion" if i < 5 else "owner_art"}
            for i in range(18)
        ]
        verdict = film_motion_preflight(rights)
        self.assertEqual(verdict["status"], "pass")

    def test_non_stock_film_and_still_only_scenes_do_not_force_motion(self) -> None:
        rights = [{"source_actual": "owner_art"} for _ in range(12)]
        self.assertEqual(film_motion_preflight(rights)["status"], "pass")
        stills = [{"stock_requested": True, "source_preference": "stock_still",
                   "source_actual": "stock_still"} for _ in range(10)]
        self.assertEqual(film_motion_preflight(stills)["status"], "pass")


if __name__ == "__main__":
    unittest.main()
