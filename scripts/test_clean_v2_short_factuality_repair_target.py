from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from clean_v2.pipeline import _run_one_bounded_factuality_repair


class _Router:
    def __init__(self) -> None:
        self.prompt = ""
        self.stage = ""

    def route(self, *, stage, prompt, max_tokens, validator):
        self.stage = stage
        self.prompt = prompt
        return validator(
            {
                "patches": [
                    {
                        "section_id": "s2",
                        "find": "لكن الحقيقة أن توقعاتنا للنتيجة تُشغِّل مشاعرنا أكثر من الطاقة نفسها.",
                        "replace": "لكن قد تتغير مشاعرنا مع توقعاتنا للنتيجة، وهذا يختلف من شخص لآخر.",
                    }
                ]
            }
        )


class ShortFactualityRepairTargetTests(unittest.TestCase):
    def test_cohort_attempt_1_section_note_targets_s2_deterministically(self) -> None:
        script = {
            "schema_version": 1,
            "title": "كيف تنهض عندما تفقد الدافع",
            "sections": [
                {
                    "id": "s1",
                    "narration": "أحياناً يختفي الدافع كأنك في منتصف نهارٍ صامت.",
                },
                {
                    "id": "s2",
                    "narration": "لكن الحقيقة أن توقعاتنا للنتيجة تُشغِّل مشاعرنا أكثر من الطاقة نفسها.",
                },
                {
                    "id": "s3",
                    "narration": "ابدأ بتدوين هدف صغير اليوم، ثم اكتب خطوة واحدة لتحقيقه.",
                },
            ],
        }
        blocked_report = {
            "status": "block",
            "unsupported_claims": [
                "claim that expectations influence emotions more than energy"
            ],
            "professional_advice_flags": [],
            "expert_persona_flags": [],
            "notes": ["Section s2 contains an unsupported psychological claim."],
        }
        plan = {
            "title": "كيف تنهض عندما تفقد الدافع",
            "sections": [
                {"id": "s1"},
                {"id": "s2"},
                {"id": "s3"},
            ],
        }
        router = _Router()

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "narrative-identity.json").write_text(
                json.dumps(
                    {
                        "opener": "",
                        "closer": "",
                        "transitions": [],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (root / "cta-plan.json").write_text(
                json.dumps(
                    {
                        "mode": "none",
                        "spoken_text": "",
                        "anchor_section_id": "",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            report = _run_one_bounded_factuality_repair(
                output_dir=root,
                brief={
                    "format": "short",
                    "approved_topic": "كيف تنهض عندما تفقد الدافع تمامًا؟",
                    "research_pack": [],
                },
                plan=plan,
                script=script,
                router=router,
                blocked_report=blocked_report,
            )

            persisted = json.loads(
                (root / "script-post-factuality-repair.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertEqual(router.stage, "script_patch")
        self.assertIn("- [factuality-location] s2", router.prompt)
        self.assertEqual(report["attempts"], 1)
        self.assertEqual(
            persisted["sections"][0]["narration"],
            "أحياناً يختفي الدافع كأنك في منتصف نهارٍ صامت.",
        )
        self.assertIn("وهذا يختلف من شخص لآخر", persisted["sections"][1]["narration"])
        self.assertEqual(
            persisted["sections"][2]["narration"],
            "ابدأ بتدوين هدف صغير اليوم، ثم اكتب خطوة واحدة لتحقيقه.",
        )


if __name__ == "__main__":
    unittest.main()
