from __future__ import annotations

import unittest

from scripts.control_approved_brief import resolve_control_format


class ProfessionalLongFormatMomentGuardTests(unittest.TestCase):
    def test_long_kind_cannot_be_downgraded_to_moment_by_title_signal(self) -> None:
        request = {
            "kind": "long",
            "approved_topic": "شورت عن التسويف",
            "format": "auto",
            "format_policy": {
                "version": "professional_long_format_router_v1",
                "requested": "auto",
                "resolution_stage": "v4_before_approved_brief_binding",
            },
            "research_pack": [{"x": 1}, {"x": 2}],
        }
        with self.assertRaisesRegex(RuntimeError, "non-long format"):
            resolve_control_format(request)


if __name__ == "__main__":
    unittest.main()
