from __future__ import annotations

import unittest

from scripts.control_approved_brief import resolve_control_format


class ProfessionalLongFormatPolicyFailClosedTests(unittest.TestCase):
    def test_auto_requires_expected_policy_version_and_stage(self) -> None:
        request = {
            "kind": "long",
            "approved_topic": "قصة رجل بدأ من جديد",
            "format": "auto",
            "research_pack": [{}, {}],
        }
        with self.assertRaises(RuntimeError):
            resolve_control_format(request)
        request["format_policy"] = {
            "version": "wrong",
            "requested": "auto",
            "resolution_stage": "v4_before_approved_brief_binding",
        }
        with self.assertRaises(RuntimeError):
            resolve_control_format(request)


if __name__ == "__main__":
    unittest.main()
