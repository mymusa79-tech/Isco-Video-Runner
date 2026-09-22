from __future__ import annotations

import unittest

from clean_v2.pipeline import _factuality_target_section_ids
from clean_v2.text_audit import _text_audit_schema, _validate_factuality_result


class StructuredFactualityLocationTests(unittest.TestCase):
    def test_schema_enums_current_script_section_ids(self) -> None:
        schema = _text_audit_schema(("s1", "s2", "s3"))
        for field in (
            "unsupported_claims",
            "professional_advice_flags",
            "expert_persona_flags",
        ):
            item = schema["properties"][field]["items"]
            self.assertEqual(item["required"], ["section_id", "issue"])
            self.assertEqual(item["properties"]["section_id"]["enum"], ["s1", "s2", "s3"])
            self.assertFalse(item["additionalProperties"])

    def test_all_provider_wordings_use_structured_section_id_without_prose_inference(self) -> None:
        script = {
            "sections": [
                {"id": "s1", "narration": "alpha"},
                {"id": "s2", "narration": "beta"},
                {"id": "s3", "narration": "gamma"},
            ]
        }
        wordings = {
            "gemini": "This claim needs support; no location words here.",
            "groq": "المعلومة تحتاج إلى سند مباشر.",
            "openrouter": "Claim is unsupported in the approved research pack.",
            "mistral": "Unsupported causal assertion.",
        }
        for provider, wording in wordings.items():
            with self.subTest(provider=provider):
                raw = {
                    "status": "block",
                    "unsupported_claims": [{"section_id": "s2", "issue": wording}],
                    "professional_advice_flags": [],
                    "expert_persona_flags": [],
                    "notes": [],
                }
                validated = _validate_factuality_result(raw, ("s1", "s2", "s3"))
                report = {"diagnostics": {"provider": provider, "raw_result": validated}}
                self.assertEqual(_factuality_target_section_ids(report, script), ("s2",))

    def test_invalid_or_missing_section_id_fails_closed(self) -> None:
        base = {
            "status": "block",
            "professional_advice_flags": [],
            "expert_persona_flags": [],
            "notes": [],
        }
        for item in (
            {"issue": "no id"},
            {"section_id": "s9", "issue": "outside current script"},
            "Section s2 has a problem",
        ):
            with self.subTest(item=item):
                raw = dict(base)
                raw["unsupported_claims"] = [item]
                with self.assertRaises(ValueError):
                    _validate_factuality_result(raw, ("s1", "s2", "s3"))


if __name__ == "__main__":
    unittest.main()
