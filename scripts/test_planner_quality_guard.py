from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import isco_video_agent.orchestrator as orchestrator
import isco_video_agent.resilient_planner as staged
from scripts.planner_quality_guard import (
    _QUESTION_ANSWER_RUNTIME_RULE,
    _first_spoken_sentence,
    _neutralize_harsh_directives,
    _safe_opening_visual_query,
    _single_use_transition_slots,
    _spoken_hook,
    install_planner_quality_guard,
)


RUN54_PRIOR_METADATA_HOOK = (
    "هل جربت يومًا أن تتوقف عن فعل شيء ما، فارتفع داخل رأسك صوتٌ يعاتبك، "
    "كأن هناك قاضيًا خفيًا يراقب كل قرار تكرهه؟"
)
RUN54_CURRENT_METADATA_HOOK = (
    "هل جربت يومًا أن تتوقف عن فعل شيء ما، فارتفع داخل رأسك صوتٌ يعاتبك، "
    "كأن هناك قاضيًا خفيًا يراقب كل قرار تتخذه؟"
)
RUN54_CURRENT_SPOKEN_HOOK = (
    "هل جربت يومًا أن تنهي مكالمة عمل أو نقاشًا عاديًا، ثم تقضي بقية يومك "
    "تراجع كل كلمة تفوهت بها كأنك تقف أمام لجنة تأديبية؟"
)
RUN54_BLOCKED_TONE_SENTENCE = (
    "افطم نفسك تدريجيًا عن هذا المراقبة المستمرة، واستعد زمام حواسك لتكون حاضرًا فيما تفعله هنا والآن."
)
RUN54_NEUTRAL_TONE_SENTENCE = (
    "يمكنك أن تقلل تدريجيًا من هذه المراقبة المستمرة، واستعد زمام حواسك لتكون حاضرًا فيما تفعله هنا والآن."
)


def _plan(*, metadata_hook: str = RUN54_CURRENT_METADATA_HOOK, narration: str = RUN54_CURRENT_SPOKEN_HOOK):
    return SimpleNamespace(
        topic="صوت الآخرين في رأسك",
        hook=metadata_hook,
        sections=[SimpleNamespace(id="sec_1", narration=narration, visual_query="table room notebook")],
    )


class PlannerQualityGuardTests(unittest.TestCase):
    def test_film_transition_hints_are_never_recycled(self) -> None:
        slots = _single_use_transition_slots(["أ", "ب", "ج"], 8)
        self.assertEqual(slots, ["أ", "ب", "ج", "", "", "", ""])
        self.assertEqual(slots.count("أ"), 1)
        self.assertEqual(slots.count("ب"), 1)
        self.assertEqual(slots.count("ج"), 1)

    def test_story_transition_hints_are_never_recycled(self) -> None:
        self.assertEqual(
            _single_use_transition_slots(["أ", "ب", "ج"], 5),
            ["أ", "ب", "ج", ""],
        )

    def test_empty_transition_values_are_not_forced(self) -> None:
        self.assertEqual(
            _single_use_transition_slots(["أ", "", "  ", "ب"], 6),
            ["أ", "ب", "", "", ""],
        )

    def test_question_answer_rule_requires_spoken_structure_not_metadata_only(self) -> None:
        self.assertIn("SPOKEN narration itself", _QUESTION_ANSWER_RUNTIME_RULE)
        self.assertIn("do not collapse", _QUESTION_ANSWER_RUNTIME_RULE)
        self.assertIn("metadata", _QUESTION_ANSWER_RUNTIME_RULE)

    def test_run54_identifiable_person_query_is_reduced_to_objects_and_environment(self) -> None:
        self.assertEqual(
            _safe_opening_visual_query(
                "person sitting by wooden table in sunlit room looking pensively at empty notebook"
            ),
            "table room notebook",
        )

    def test_explicit_non_identifiable_framing_is_preserved(self) -> None:
        self.assertEqual(
            _safe_opening_visual_query("hands writing notebook"),
            "hands writing notebook",
        )
        self.assertEqual(
            _safe_opening_visual_query("silhouette person walking road"),
            "silhouette person walking road",
        )

    def test_non_person_query_is_preserved(self) -> None:
        self.assertEqual(
            _safe_opening_visual_query("notebook desk window morning light"),
            "notebook desk window morning light",
        )

    def test_human_only_query_uses_safe_broad_fallback(self) -> None:
        self.assertEqual(
            _safe_opening_visual_query("person sitting alone thinking"),
            "quiet room natural light",
        )

    def test_run54_harsh_directive_is_neutralized_and_grammar_fixed(self) -> None:
        self.assertEqual(
            _neutralize_harsh_directives(RUN54_BLOCKED_TONE_SENTENCE),
            RUN54_NEUTRAL_TONE_SENTENCE,
        )

    def test_harsh_directive_without_gradual_word_is_neutralized(self) -> None:
        self.assertEqual(
            _neutralize_harsh_directives("افطم نفسك عن مراقبة آراء الآخرين."),
            "يمكنك أن تقلل من مراقبة آراء الآخرين.",
        )

    def test_ordinary_practical_suggestions_are_not_rewritten(self) -> None:
        samples = [
            "جرب أن تدوّن الفكرة قبل أن تحكم عليها.",
            "خذ نفسًا عميقًا قبل أن ترد.",
            "يمكنك أن تسأل نفسك ما الذي تريده فعلًا.",
        ]
        for sample in samples:
            self.assertEqual(_neutralize_harsh_directives(sample), sample)

    def test_build_plan_tone_wrapper_adds_zero_provider_calls_and_normalizes_output(self) -> None:
        calls: list[tuple[tuple, dict]] = []
        fake_plan = SimpleNamespace(
            hook="hook",
            sections=[SimpleNamespace(id="sec_4", narration=RUN54_BLOCKED_TONE_SENTENCE)],
        )

        def fake_build(*args, **kwargs):
            calls.append((args, kwargs))
            return fake_plan

        with (
            patch.object(staged, "_outline", lambda *a, **k: {"section_briefs": []}),
            patch.object(staged, "_write_full_script", lambda *a, **k: {}),
            patch.object(staged, "build_plan", fake_build),
            patch.object(orchestrator, "_novelty_flags", lambda *a, **k: []),
            patch.object(orchestrator, "append_history", lambda record: record),
        ):
            install_planner_quality_guard()
            result = staged.build_plan("key", "topic", "film", "model")

        self.assertIs(result, fake_plan)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result.sections[0].narration, RUN54_NEUTRAL_TONE_SENTENCE)

    def test_first_spoken_sentence_strips_dialogue_label(self) -> None:
        self.assertEqual(
            _first_spoken_sentence("A: لماذا تفعل ذلك؟\nB: لأنني خائف."),
            "لماذا تفعل ذلك؟",
        )

    def test_spoken_hook_falls_back_to_metadata_when_narration_is_empty(self) -> None:
        plan = _plan(metadata_hook="افتتاحية احتياطية", narration="")
        self.assertEqual(_spoken_hook(plan), "افتتاحية احتياطية")

    def test_run54_spoken_hook_is_the_actual_first_narrated_sentence(self) -> None:
        plan = _plan(narration=RUN54_CURRENT_SPOKEN_HOOK + " وهذه جملة ثانية.")
        self.assertEqual(_spoken_hook(plan), RUN54_CURRENT_SPOKEN_HOOK)
        self.assertNotEqual(_spoken_hook(plan), plan.hook)

    def test_installed_novelty_guard_keeps_threshold_but_uses_spoken_opening(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            history = Path(td) / "history.json"
            history.write_text(
                json.dumps(
                    {
                        "videos": [
                            {
                                "topic": "صوت الآخرين في رأسك",
                                "hook": RUN54_PRIOR_METADATA_HOOK,
                                "visual_queries": [],
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            plan = _plan()
            original_novelty = orchestrator._novelty_flags

            with (
                patch.dict(os.environ, {"ISCO_HISTORY_PATH": str(history)}, clear=False),
                patch.object(staged, "_outline", lambda *a, **k: {"section_briefs": []}),
                patch.object(staged, "_write_full_script", lambda *a, **k: {}),
                patch.object(orchestrator, "_novelty_flags", original_novelty),
                patch.object(orchestrator, "append_history", lambda record: record),
            ):
                metadata_flags = original_novelty(plan, None, auto_topic=False)
                self.assertIn("hook_too_similar_to_recent", metadata_flags)

                install_planner_quality_guard()
                spoken_flags = orchestrator._novelty_flags(plan, None, auto_topic=False)

            self.assertEqual(spoken_flags, [])
            self.assertEqual(plan.hook, RUN54_CURRENT_METADATA_HOOK)

    def test_history_guard_stores_spoken_hook_and_preserves_metadata_for_diagnostics(self) -> None:
        captured: list[dict] = []

        def fake_append(record: dict):
            captured.append(dict(record))
            return record

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            out = root / "output" / "run54"
            out.mkdir(parents=True)
            (out / "plan.json").write_text(
                json.dumps(
                    {
                        "hook": RUN54_CURRENT_METADATA_HOOK,
                        "sections": [{"id": "s1", "narration": RUN54_CURRENT_SPOKEN_HOOK + " جملة أخرى."}],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            record = {
                "hook": RUN54_CURRENT_METADATA_HOOK,
                "output": "output/run54/final.mp4",
            }

            with (
                patch.object(staged, "_outline", lambda *a, **k: {"section_briefs": []}),
                patch.object(staged, "_write_full_script", lambda *a, **k: {}),
                patch.object(orchestrator, "_novelty_flags", lambda *a, **k: []),
                patch.object(orchestrator, "append_history", fake_append),
                patch.object(orchestrator, "ROOT", root),
            ):
                install_planner_quality_guard()
                orchestrator.append_history(record)

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["hook"], RUN54_CURRENT_SPOKEN_HOOK)
        self.assertEqual(captured[0]["metadata_hook"], RUN54_CURRENT_METADATA_HOOK)
        self.assertEqual(record["hook"], RUN54_CURRENT_METADATA_HOOK)
        self.assertNotIn("metadata_hook", record)

    def test_installed_outline_wrapper_sanitizes_only_first_longform_brief_without_extra_call(self) -> None:
        outline_calls: list[dict] = []

        def fake_outline(*args, **kwargs):
            outline_calls.append(kwargs)
            return {
                "section_briefs": [
                    {
                        "id": "s1",
                        "visual_query": "person sitting by wooden table in sunlit room looking pensively at empty notebook",
                    },
                    {"id": "s2", "visual_query": "person walking city street"},
                ]
            }

        def fake_write(*args, **kwargs):
            return {"ok": True}

        with (
            patch.object(staged, "_outline", fake_outline),
            patch.object(staged, "_write_full_script", fake_write),
            patch.object(orchestrator, "_novelty_flags", lambda *a, **k: []),
            patch.object(orchestrator, "append_history", lambda record: record),
        ):
            install_planner_quality_guard()
            result = staged._outline("key", topic="موضوع", fmt="film", model="model")

        self.assertEqual(len(outline_calls), 1)
        self.assertEqual(result["section_briefs"][0]["visual_query"], "table room notebook")
        self.assertEqual(result["section_briefs"][1]["visual_query"], "person walking city street")

    def test_installed_wrapper_passes_single_use_slots_without_extra_call(self) -> None:
        calls: list[dict] = []

        def fake_outline(*args, **kwargs):
            return {"section_briefs": []}

        def fake_write(*args, **kwargs):
            calls.append(kwargs)
            return {"ok": True}

        with (
            patch.object(staged, "_outline", fake_outline),
            patch.object(staged, "_write_full_script", fake_write),
            patch.object(orchestrator, "_novelty_flags", lambda *a, **k: []),
            patch.object(orchestrator, "append_history", lambda record: record),
        ):
            install_planner_quality_guard()
            result = staged._write_full_script(
                "key",
                topic="موضوع",
                fmt="film",
                model="model",
                briefs=[{"id": f"s{i}"} for i in range(1, 9)],
                narrative_format="question_answer",
                target_per_section=120,
                transition_variants=["أ", "ب", "ج"],
                research_json="{}",
                avoid_json="{}",
                policy_json="{}",
                revision_note="",
            )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["transition_variants"], ["أ", "ب", "ج", "", "", "", ""])


if __name__ == "__main__":
    unittest.main()
