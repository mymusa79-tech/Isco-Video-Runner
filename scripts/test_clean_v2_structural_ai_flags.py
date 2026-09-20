from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from clean_v2.pipeline import (
    STAGES,
    STRUCTURAL_AI_STAGE,
    TEXT_AUDIT_STAGE,
    _run_structural_ai_flags,
)
from clean_v2.structural_ai import structural_ai_flags


class CleanV2StructuralAIFlagsTests(unittest.TestCase):
    def test_stage_is_after_script_and_before_text_audit(self):
        self.assertLess(STAGES.index("script"), STAGES.index(STRUCTURAL_AI_STAGE))
        self.assertLess(STAGES.index(STRUCTURAL_AI_STAGE), STAGES.index(TEXT_AUDIT_STAGE))

    def test_legacy_thresholds_are_preserved(self):
        long_questions = " ".join(f"هل هذا سؤال {i}؟" for i in range(6))
        short_questions = " ".join(f"هل هذا سؤال {i}؟" for i in range(3))
        self.assertIn(
            "excessive_rhetorical_questions",
            structural_ai_flags(long_questions, short_form=False),
        )
        self.assertNotIn(
            "excessive_rhetorical_questions",
            structural_ai_flags(short_questions, short_form=False),
        )
        self.assertIn(
            "excessive_rhetorical_questions",
            structural_ai_flags(short_questions, short_form=True),
        )

    def test_legacy_duplicate_and_generic_closer_flags_are_preserved(self):
        text = (
            "هذه جملة واضحة فيها أربع كلمات على الأقل. "
            "هذه جملة واضحة فيها أربع كلمات على الأقل. "
            "وفي النهاية كل ما عليك أن تبدأ."
        )
        flags = structural_ai_flags(text, short_form=False)
        self.assertIn("duplicate_sentence", flags)
        self.assertIn("generic_motivational_closer", flags)

    def test_report_is_advisory_and_format_aware(self):
        script = {
            "sections": [
                {"id": "s1", "narration": "هل أبدأ؟ هل أتوقف؟ هل أغيّر؟"}
            ]
        }
        with tempfile.TemporaryDirectory() as tmp:
            report = _run_structural_ai_flags(
                output_dir=Path(tmp),
                brief={"format": "moment"},
                script=script,
            )
            self.assertEqual(report["mode"], "advisory")
            self.assertTrue(report["short_form"])
            self.assertIn("excessive_rhetorical_questions", report["flags"])
            persisted = json.loads(
                (Path(tmp) / "structural-ai-flags.json").read_text(encoding="utf-8")
            )
            self.assertEqual(persisted, report)


if __name__ == "__main__":
    unittest.main()
