from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from clean_v2 import text_audit
from clean_v2.pipeline import (
    _run_legacy_text_audits,
    _run_legacy_tone_naturalness_audit,
)


VALID_TONE = {
    "status": "pass",
    "preachiness_flags": [],
    "cultural_dignity_flags": [],
    "naturalness_flags": [],
    "narrative_format_flags": [],
    "unverified_religious_quote_flags": [],
    "notes": [],
}


class CleanV2ToneNaturalnessTests(unittest.TestCase):
    def test_tone_schema_matches_legacy_return_contract_and_is_strict(self):
        schema = text_audit.TONE_AUDIT_SCHEMA
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(
            set(schema["required"]),
            {
                "status",
                "preachiness_flags",
                "cultural_dignity_flags",
                "naturalness_flags",
                "narrative_format_flags",
                "unverified_religious_quote_flags",
                "notes",
            },
        )
        self.assertEqual(schema["properties"]["status"]["enum"], ["pass", "block"])

    def test_mistral_tone_call_uses_strict_schema(self):
        captured = {}

        def fake_executor(prompt, **kwargs):
            captured["prompt"] = prompt
            captured.update(kwargs)
            return dict(VALID_TONE)

        with patch.object(text_audit, "mistral_executor_json", side_effect=fake_executor):
            result = text_audit._mistral_tone_call("audit this")
        self.assertEqual(result, VALID_TONE)
        self.assertEqual(captured["task_kind"], "text_audit")
        name, schema = captured["response_schema"]
        self.assertEqual(name, "clean_v2_tone_naturalness_audit_v1")
        self.assertIs(schema, text_audit.TONE_AUDIT_SCHEMA)

    def test_tone_validator_rejects_missing_required_array(self):
        import sys
        import types

        router = types.ModuleType("isco_video_agent.text_audit_router")

        def validate_audit_payload(raw, *, required_arrays):
            if raw.get("status") not in {"pass", "block"}:
                raise ValueError("bad status")
            for field in required_arrays:
                if field not in raw or not isinstance(raw[field], list):
                    raise ValueError("missing array")
            return raw, raw["status"]

        router.validate_audit_payload = validate_audit_payload
        parent = types.ModuleType("isco_video_agent")
        parent.text_audit_router = router
        with patch.dict(
            sys.modules,
            {
                "isco_video_agent": parent,
                "isco_video_agent.text_audit_router": router,
            },
        ):
            broken = dict(VALID_TONE)
            broken.pop("naturalness_flags")
            with self.assertRaises(text_audit.MistralExecutorWireFailure):
                text_audit._validate_tone_result(broken)

    def test_tone_content_block_and_provider_exhaustion_keep_distinct_classification(self):
        brief = {"format": "film"}
        plan = {"sections": []}
        script = {"sections": []}

        with tempfile.TemporaryDirectory() as tmp, patch(
            "clean_v2.pipeline._build_production_plan_for_audit",
            return_value=object(),
        ), patch(
            "clean_v2.text_audit.audit_tone_with_mistral",
            return_value={
                **VALID_TONE,
                "status": "block",
                "naturalness_flags": ["generic AI filler"],
                "validation": "valid",
                "attempts": [],
            },
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "tone/naturalness gate blocked",
            ):
                _run_legacy_tone_naturalness_audit(
                    output_dir=Path(tmp),
                    brief=brief,
                    plan=plan,
                    script=script,
                )

        with tempfile.TemporaryDirectory() as tmp, patch(
            "clean_v2.pipeline._build_production_plan_for_audit",
            return_value=object(),
        ), patch(
            "clean_v2.text_audit.audit_tone_with_mistral",
            return_value={
                **VALID_TONE,
                "status": "block",
                "validation": "providers_exhausted",
                "attempts": [{"provider": "mistral", "outcome": "rate_limited"}],
            },
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "exhausted bounded provider route: tone_naturalness",
            ):
                _run_legacy_tone_naturalness_audit(
                    output_dir=Path(tmp),
                    brief=brief,
                    plan=plan,
                    script=script,
                )

    def test_composite_text_audit_runs_factuality_then_tone(self):
        calls = []

        def factuality(**kwargs):
            calls.append("factuality")
            return {"status": "pass"}

        def tone(**kwargs):
            calls.append("tone")
            return {"status": "pass"}

        with tempfile.TemporaryDirectory() as tmp, patch(
            "clean_v2.pipeline._run_legacy_factuality_audit",
            side_effect=factuality,
        ), patch(
            "clean_v2.pipeline._run_legacy_tone_naturalness_audit",
            side_effect=tone,
        ):
            result = _run_legacy_text_audits(
                output_dir=Path(tmp),
                brief={"format": "film"},
                plan={},
                script={},
            )
            self.assertEqual(calls, ["factuality", "tone"])
            self.assertEqual(result["status"], "pass")
            self.assertEqual(result["factuality_status"], "pass")
            self.assertEqual(result["tone_naturalness_status"], "pass")


if __name__ == "__main__":
    unittest.main()
