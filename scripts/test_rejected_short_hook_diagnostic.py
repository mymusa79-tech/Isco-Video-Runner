import json
import unittest

from clean_v2.providers import _safe_mistral_script_raw_diagnostic as diag


class RejectedShortHookDiagnosticTests(unittest.TestCase):
    RAW = json.dumps({"sections": [{"id": "s1", "narration": "جملة افتتاح أولى. جملة ثانية."}]}, ensure_ascii=False)

    def test_hook_validator_failure_exposes_only_opening_sentence(self):
        out = diag(self.RAW, Exception("short_hook_requires_immediate_concrete_tension"))
        self.assertEqual(out["rejected_hook"], "جملة افتتاح أولى.")

    def test_other_failures_stay_shape_only(self):
        self.assertNotIn("rejected_hook", diag(self.RAW, Exception("podcast_estimated_duration_too_short")))


if __name__ == "__main__":
    unittest.main()
