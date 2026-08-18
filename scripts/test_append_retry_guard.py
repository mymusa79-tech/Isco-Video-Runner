from __future__ import annotations

import unittest

from scripts.append_retry_guard import _parse_safe_partial_additions


class AppendRetryGuardTests(unittest.TestCase):
    def test_exact_two_additions_still_pass(self) -> None:
        result = _parse_safe_partial_additions(
            {
                "additions": [
                    {"id": "s2", "append_text": "إضافة أولى"},
                    {"id": "s3", "append_text": "إضافة ثانية"},
                ]
            },
            ["s2", "s3"],
        )
        self.assertEqual(result, {"s2": "إضافة أولى", "s3": "إضافة ثانية"})

    def test_run40_shape_single_valid_addition_is_accepted_without_synthesis(self) -> None:
        result = _parse_safe_partial_additions(
            {"additions": [{"id": "s2", "append_text": " ".join(["إضافة"] * 166)}]},
            ["s2", "s3"],
        )
        self.assertEqual(list(result), ["s2"])
        self.assertEqual(len(result["s2"].split()), 166)
        self.assertNotIn("s3", result)

    def test_single_addition_object_is_normalized_safely(self) -> None:
        result = _parse_safe_partial_additions(
            {"additions": {"id": "s3", "append_text": "إضافة آمنة"}},
            ["s2", "s3"],
        )
        self.assertEqual(result, {"s3": "إضافة آمنة"})

    def test_empty_or_missing_additions_still_fail_closed(self) -> None:
        for payload in ({}, {"additions": []}):
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(RuntimeError, "between 1 and 2"):
                    _parse_safe_partial_additions(payload, ["s2", "s3"])

    def test_unknown_target_is_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "non-target section: s9"):
            _parse_safe_partial_additions(
                {"additions": [{"id": "s9", "append_text": "لا تقبل"}]},
                ["s2", "s3"],
            )

    def test_duplicate_target_is_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "duplicated section id: s2"):
            _parse_safe_partial_additions(
                {
                    "additions": [
                        {"id": "s2", "append_text": "أ"},
                        {"id": "s2", "append_text": "ب"},
                    ]
                },
                ["s2", "s3"],
            )

    def test_reversed_target_order_is_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "preserve target order"):
            _parse_safe_partial_additions(
                {
                    "additions": [
                        {"id": "s3", "append_text": "ثانية"},
                        {"id": "s2", "append_text": "أولى"},
                    ]
                },
                ["s2", "s3"],
            )

    def test_replacement_narration_field_is_ignored(self) -> None:
        result = _parse_safe_partial_additions(
            {
                "additions": [
                    {
                        "id": "s2",
                        "append_text": "الإضافة الوحيدة المقبولة",
                        "narration": "محاولة استبدال يجب تجاهلها",
                    }
                ]
            },
            ["s2", "s3"],
        )
        self.assertEqual(result, {"s2": "الإضافة الوحيدة المقبولة"})
        self.assertNotIn("محاولة استبدال", result["s2"])


if __name__ == "__main__":
    unittest.main()
