from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from clean_v2.pipeline import (
    _first_spoken_sentence,
    _run_legacy_tone_naturalness_audit,
    _run_text_audits,
)
from clean_v2.tone_audit import TONE_AUDIT_SCHEMA, _mistral_tone_call


def _tone_result(*, status: str = "pass", validation: str = "valid") -> dict:
    return {
        "status": status,
        "validation": validation,
        "provider": "gemini" if validation == "valid" else None,
        "attempts": [],
        "preachiness_flags": [],
        "cultural_dignity_flags": [],
        "naturalness_flags": [],
        "narrative_format_flags": [],
        "unverified_religious_quote_flags": [],
        "notes": [],
    }


class CleanV2ToneNaturalnessTests(unittest.TestCase):
    def test_strict_schema_matches_legacy_tone_contract(self):
        self.assertFalse(TONE_AUDIT_SCHEMA["additionalProperties"])
        self.assertEqual(
            set(TONE_AUDIT_SCHEMA["required"]),
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
        self.assertEqual(
            TONE_AUDIT_SCHEMA["properties"]["status"]["enum"],
            ["pass", "block"],
        )

    def test_mistral_tone_call_uses_strict_schema(self):
        payload = _tone_result()
        captured = {}

        def fake_executor(prompt, **kwargs):
            captured["prompt"] = prompt
            captured.update(kwargs)
            return payload

        with patch("clean_v2.tone_audit.mistral_executor_json", side_effect=fake_executor), patch(
            "clean_v2.tone_audit._validate_tone_result",
            side_effect=lambda value: value,
        ):
            result = _mistral_tone_call("audit prompt")

        self.assertEqual(result, payload)
        self.assertEqual(captured["task_kind"], "text_audit")
        self.assertEqual(captured["temperature"], 0.1)
        name, schema = captured["response_schema"]
        self.assertEqual(name, "clean_v2_tone_naturalness_audit_v1")
        self.assertIs(schema, TONE_AUDIT_SCHEMA)

    def test_first_spoken_sentence_is_runtime_hook(self):
        script = {
            "sections": [
                {
                    "id": "s1",
                    "narration": "لماذا نخطط كثيرًا ولا نبدأ؟ الجواب ليس نقص الوقت.",
                }
            ]
        }
        self.assertEqual(
            _first_spoken_sentence(script),
            "لماذا نخطط كثيرًا ولا نبدأ؟",
        )

    def test_valid_content_block_is_quality_block_not_infrastructure(self):
        blocked = _tone_result(status="block")
        blocked["naturalness_flags"] = ["generic AI filler"]
        dummy_plan = SimpleNamespace(hook="")

        with tempfile.TemporaryDirectory() as tmp, patch(
            "clean_v2.pipeline._build_production_plan_for_audit",
            return_value=dummy_plan,
        ), patch(
            "clean_v2.tone_audit.audit_tone_and_naturalness_with_mistral",
            return_value=blocked,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "tone/naturalness gate blocked real production",
            ):
                _run_legacy_tone_naturalness_audit(
                    output_dir=Path(tmp),
                    brief={"format": "film"},
                    plan={"sections": []},
                    script={
                        "sections": [
                            {"id": "s1", "narration": "افتتاح واضح. ثم شرح طبيعي."}
                        ]
                    },
                )
            persisted = json.loads(
                (Path(tmp) / "tone-naturalness-audit.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(persisted["status"], "block")
            self.assertEqual(dummy_plan.hook, "افتتاح واضح.")

    def test_provider_exhaustion_stays_infrastructure(self):
        exhausted = _tone_result(status="block", validation="providers_exhausted")
        exhausted["attempts"] = [
            {"provider": "gemini", "outcome": "rate_limited"},
            {"provider": "groq", "outcome": "rate_limited"},
            {"provider": "openrouter", "outcome": "other"},
            {"provider": "mistral", "outcome": "rate_limited"},
        ]

        with tempfile.TemporaryDirectory() as tmp, patch(
            "clean_v2.pipeline._build_production_plan_for_audit",
            return_value=SimpleNamespace(hook=""),
        ), patch(
            "clean_v2.tone_audit.audit_tone_and_naturalness_with_mistral",
            return_value=exhausted,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "text_audit exhausted bounded provider route: tone_naturalness",
            ):
                _run_legacy_tone_naturalness_audit(
                    output_dir=Path(tmp),
                    brief={"format": "film"},
                    plan={"sections": []},
                    script={"sections": [{"id": "s1", "narration": "افتتاح واضح."}]},
                )

    def test_composite_runs_factuality_before_tone(self):
        order = []

        def factuality(**kwargs):
            order.append("factuality")
            return {"status": "pass"}

        def tone(**kwargs):
            order.append("tone")
            return {"status": "pass"}

        with patch(
            "clean_v2.pipeline._run_legacy_factuality_audit",
            side_effect=factuality,
        ), patch(
            "clean_v2.pipeline._run_legacy_tone_naturalness_audit",
            side_effect=tone,
        ):
            report = _run_text_audits(
                output_dir=Path("."),
                brief={},
                plan={},
                script={},
            )

        self.assertEqual(order, ["factuality", "tone"])
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["factuality_status"], "pass")
        self.assertEqual(report["tone_naturalness_status"], "pass")


if __name__ == "__main__":
    unittest.main()
