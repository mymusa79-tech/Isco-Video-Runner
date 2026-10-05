from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from clean_v2.pipeline import (
    _bind_writer_visual_story_with_recovery,
    _checkpoint_artifact_paths,
    _planning_prompt,
)
from clean_v2.visual_story import _writer_searchable_intent


class _NoRecoveryRouter:
    events = []

    def route_exact_provider(self, **kwargs):
        raise AssertionError("visual binding must stay local")


class Run93SimpleClosureTests(unittest.TestCase):
    def test_non_causal_research_mode_and_visual_cluster_are_explicit(self):
        brief = {
            "approved_by_user": True,
            "approved_topic": "لماذا ننتظر الشعور المناسب قبل أن نتحرك؟",
            "format": "film",
            "language": "ar",
            "audience": "Arabic-speaking adults",
            "editorial_intent": "ملاحظة سلوكية",
            "research_pack": [
                {"claim_scope": "Context only; does not establish causality."}
            ],
            "hard_constraints": ["No fabricated facts."],
        }
        prompt = _planning_prompt(brief)
        self.assertIn("NON_CAUSAL_RESEARCH_MODE", prompt)
        self.assertIn("generic productivity-prop cluster", prompt)

    def test_task_state_on_screen_survives_text_sanitizer(self):
        intent = _writer_searchable_intent(
            "laptop screen showing completed task list hands only"
        )
        self.assertIn("completed task list", intent)
        self.assertNotEqual(intent, "laptop")

    def test_repeat_is_local_and_preserves_semantic_proof(self):
        plan = {
            "sections": [{"id": "s1"}],
            "_visual_diversity_contract": "v2_fail_closed",
        }
        story = {
            "beats": [
                {
                    "id": "b1",
                    "section_id": "s1",
                    "shot_intent": "hand writing one task in notebook",
                    "stock_query_en": "hand writing one task in notebook",
                    "semantic_must_have": ["writing one task"],
                },
                {
                    "id": "b2",
                    "section_id": "s1",
                    "shot_intent": "pen writing next task on paper",
                    "stock_query_en": "pen writing next task on paper",
                    "semantic_must_have": ["writing next task"],
                },
            ]
        }
        script = {
            "sections": [{
                "id": "s1",
                "narration": "فكرة أولى واضحة. ثم تتقدم الفكرة إلى نتيجة ثانية.",
            }]
        }
        with tempfile.TemporaryDirectory() as root:
            bound = _bind_writer_visual_story_with_recovery(
                router=_NoRecoveryRouter(),
                output_dir=Path(root),
                brief={},
                plan=plan,
                script=script,
                visual_story=story,
            )
        self.assertEqual(
            bound["beats"][1]["semantic_must_have"],
            ["writing next task"],
        )
        self.assertTrue(bound["beats"][1].get("semantic_should_avoid"))

    def test_script_checkpoint_uses_immutable_sources(self):
        with tempfile.TemporaryDirectory() as root:
            paths = {
                p.as_posix()
                for p in _checkpoint_artifact_paths(Path(root), "script")
            }
        self.assertIn("script-source.json", paths)
        self.assertIn("visual-story-source.json", paths)
        self.assertNotIn("script.json", paths)


if __name__ == "__main__":
    unittest.main()
