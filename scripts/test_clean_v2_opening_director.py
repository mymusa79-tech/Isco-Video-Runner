from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from clean_v2.media import render_video
from clean_v2.opening_director import (
    CleanV2OpeningBlock,
    CleanV2OpeningInfrastructure,
    opening_slot_specs,
    run_opening_director,
)
from clean_v2.pipeline import OPENING_STAGE, STAGES, VISUAL_QA_STAGE, _Journal
from clean_v2.visual_qa import CleanV2VisualQABlock, CleanV2VisualQAInfrastructure


class _FakeOpeningVisualSource:
    def __init__(self, candidates: list[tuple[Path, dict]]) -> None:
        self.candidates = candidates
        self.events = [{"wire_attempted": True, "result": "recovery_pool_ready"}]

    def acquire_replacement_candidates(self, *_args, **_kwargs):
        return list(self.candidates)

    def commit_replacement(self, replacement: Path, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(replacement, destination)
        sidecar = replacement.with_suffix(".m8.json")
        if sidecar.is_file():
            os.replace(sidecar, destination.with_suffix(".m8.json"))
        return destination


class _QueryOpeningVisualSource(_FakeOpeningVisualSource):
    def __init__(self, pools: dict[str, list[tuple[Path, dict]]]) -> None:
        super().__init__([])
        self.pools = pools
        self.events = []
        self.queries: list[str] = []

    def acquire_replacement_candidates(self, query, *_args, **_kwargs):
        self.queries.append(str(query))
        self.events.append(
            {
                "wire_attempted": True,
                "result": "recovery_pool_ready",
                "query": str(query),
            }
        )
        return list(self.pools.get(str(query), []))


class _FakeRecoveryRouter:
    def __init__(self, alternate_query: str) -> None:
        self.alternate_query = alternate_query
        self.events: list[dict] = []
        self.calls = 0

    def route(self, *, stage, prompt, max_tokens, validator):
        self.calls += 1
        self.events.append(
            {
                "stage": stage,
                "max_tokens": max_tokens,
                "prompt_has_opening_context": "Actual section narration" in prompt,
            }
        )
        return validator({"alternate_query": self.alternate_query})


def _plan() -> dict:
    return {
        "sections": [
            {
                "id": "s1",
                "heading": "افتتاح",
                "purpose": "فتح الفكرة",
                "visual_query_en": "quiet desk morning light",
            },
            {
                "id": "s2",
                "heading": "ثان",
                "purpose": "تقدم",
                "visual_query_en": "hands notebook task",
            },
        ]
    }


def _script() -> dict:
    return {
        "sections": [
            {"id": "s1", "narration": "افتتاح واضح يضع المشكلة أمام المشاهد ثم يفتح باب الفكرة."},
            {"id": "s2", "narration": "ثم تتقدم الفكرة إلى تفسير عملي أكثر تحديدًا."},
        ]
    }


def _prepare_primary(root: Path) -> list[dict]:
    visuals = root / "visuals"
    visuals.mkdir(parents=True, exist_ok=True)
    (visuals / "primary.mp4").write_bytes(b"x" * 2048)
    (root / "final-cut-visual-qa.json").write_text(
        json.dumps({"status": "pass"}), encoding="utf-8"
    )
    (root / "visual-audit.json").write_text(
        json.dumps(
            [
                {
                    "section": "s1",
                    "is_selected": True,
                    "final_cut_readiness": "ready",
                }
            ]
        ),
        encoding="utf-8",
    )
    return [
        {
            "provider": "pexels",
            "asset_id": "primary",
            "local_file": "primary.mp4",
            "section_id": "s1",
            "query": "quiet desk morning light",
        },
        {
            "provider": "pixabay",
            "asset_id": "body2",
            "local_file": "body2.mp4",
            "section_id": "s2",
            "query": "hands notebook task",
        },
    ]


class CleanV2OpeningDirectorTests(unittest.TestCase):
    def test_legacy_first_30_slot_math_is_exact(self):
        slots = opening_slot_specs()
        self.assertEqual([item["key"] for item in slots], ["cold_open", "escalation", "promise_and_body"])
        self.assertEqual([(item["start"], item["end"]) for item in slots], [(0.0, 7.0), (7.0, 18.0), (18.0, 30.0)])
        self.assertEqual(sum(float(item["seconds"]) for item in slots), 30.0)

    def test_stage_is_after_visual_qa_and_before_render(self):
        self.assertLess(STAGES.index(VISUAL_QA_STAGE), STAGES.index(OPENING_STAGE))
        self.assertLess(STAGES.index(OPENING_STAGE), STAGES.index("render"))

    def test_two_auxiliaries_can_survive_one_block_with_three_candidate_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rights = _prepare_primary(root)
            candidate_paths = []
            candidates = []
            for index in range(1, 4):
                path = root / "visuals" / f".candidate-{index}.mp4"
                path.write_bytes(bytes([index]) * 2048)
                candidate_paths.append(path)
                candidates.append(
                    (
                        path,
                        {
                            "provider": "pexels",
                            "asset_id": f"aux-{index}",
                            "local_file": "opening-auxiliary.mp4",
                            "section_id": "s1",
                        },
                    )
                )
            source = _FakeOpeningVisualSource(candidates)
            passed = {
                "report": {"status": "pass"},
                "audit": {"final_cut_readiness": "ready", "fit_score_10": 9.0},
            }
            with patch("clean_v2.opening_director.probe_duration", return_value=120.0), patch(
                "clean_v2.opening_director._candidate_audit",
                side_effect=[passed, CleanV2VisualQABlock("not ready"), passed],
            ):
                report = run_opening_director(
                    output_dir=root,
                    plan=_plan(),
                    script=_script(),
                    rights=rights,
                    fmt="film",
                    narration_path=root / "voice.wav",
                    visual_source=source,
                )

            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["audited_shot_count"], 3)
            self.assertEqual(report["candidate_review_count"], 3)
            self.assertTrue((root / "visuals" / "opening-cold_open.mp4").is_file())
            self.assertTrue((root / "visuals" / "opening-escalation.mp4").is_file())
            self.assertEqual(report["slots"][2]["local_file"], "primary.mp4")
            self.assertEqual(report["slots"][2]["start"], 18.0)
            self.assertEqual(report["slots"][2]["end"], 30.0)

    def test_blocked_primary_search_uses_one_bounded_alternate_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rights = _prepare_primary(root)
            primary = root / "visuals" / ".primary-candidate.mp4"
            alt1 = root / "visuals" / ".alternate-candidate-1.mp4"
            alt2 = root / "visuals" / ".alternate-candidate-2.mp4"
            for index, path in enumerate((primary, alt1, alt2), start=1):
                path.write_bytes(bytes([index]) * 2048)

            original_query = _plan()["sections"][0]["visual_query_en"]
            alternate_query = "person writing priorities in notebook"
            source = _QueryOpeningVisualSource(
                {
                    original_query: [
                        (primary, {"provider": "pexels", "asset_id": "primary-weak"})
                    ],
                    alternate_query: [
                        (alt1, {"provider": "pixabay", "asset_id": "alt-1"}),
                        (alt2, {"provider": "pexels", "asset_id": "alt-2"}),
                    ],
                }
            )
            router = _FakeRecoveryRouter(alternate_query)
            passed = {
                "report": {"status": "pass"},
                "audit": {"final_cut_readiness": "ready", "fit_score_10": 9.0},
            }
            with patch("clean_v2.opening_director.probe_duration", return_value=120.0), patch(
                "clean_v2.opening_director._candidate_audit",
                side_effect=[CleanV2VisualQABlock("not ready"), passed, passed],
            ):
                report = run_opening_director(
                    output_dir=root,
                    plan=_plan(),
                    script=_script(),
                    rights=rights,
                    fmt="film",
                    narration_path=root / "voice.wav",
                    visual_source=source,
                    router=router,
                )

            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["candidate_review_limit"], 4)
            self.assertEqual(report["candidate_review_count"], 3)
            self.assertTrue(report["alternate_query_attempted"])
            self.assertEqual(report["alternate_query"], alternate_query)
            self.assertEqual(source.queries, [original_query, alternate_query])
            self.assertEqual(router.calls, 1)
            self.assertEqual(
                [item["search_attempt"] for item in report["candidate_reviews"]],
                ["primary", "alternate", "alternate"],
            )

    def test_alternate_recovery_stays_fail_closed_after_four_reviews(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rights = _prepare_primary(root)
            paths = [
                root / "visuals" / f".bounded-candidate-{index}.mp4"
                for index in range(1, 5)
            ]
            for index, path in enumerate(paths, start=1):
                path.write_bytes(bytes([index]) * 2048)

            original_query = _plan()["sections"][0]["visual_query_en"]
            alternate_query = "person writing priorities in notebook"
            source = _QueryOpeningVisualSource(
                {
                    original_query: [
                        (paths[0], {"provider": "pexels", "asset_id": "p1"})
                    ],
                    alternate_query: [
                        (paths[1], {"provider": "pixabay", "asset_id": "a1"}),
                        (paths[2], {"provider": "pexels", "asset_id": "a2"}),
                        (paths[3], {"provider": "pixabay", "asset_id": "a3"}),
                    ],
                }
            )
            router = _FakeRecoveryRouter(alternate_query)
            passed = {
                "report": {"status": "pass"},
                "audit": {"final_cut_readiness": "ready", "fit_score_10": 9.0},
            }
            with patch("clean_v2.opening_director.probe_duration", return_value=120.0), patch(
                "clean_v2.opening_director._candidate_audit",
                side_effect=[
                    CleanV2VisualQABlock("not ready 1"),
                    CleanV2VisualQABlock("not ready 2"),
                    passed,
                    CleanV2VisualQABlock("not ready 4"),
                ],
            ) as audit:
                with self.assertRaisesRegex(
                    CleanV2OpeningBlock,
                    "two_opening_auxiliaries_not_final_cut_ready",
                ):
                    run_opening_director(
                        output_dir=root,
                        plan=_plan(),
                        script=_script(),
                        rights=rights,
                        fmt="film",
                        narration_path=root / "voice.wav",
                        visual_source=source,
                        router=router,
                    )

            self.assertEqual(audit.call_count, 4)
            self.assertEqual(router.calls, 1)
            self.assertEqual(source.queries, [original_query, alternate_query])

    def test_successful_stock_search_with_too_few_candidates_is_quality_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rights = _prepare_primary(root)
            path = root / "visuals" / ".candidate-1.mp4"
            path.write_bytes(b"a" * 2048)
            source = _FakeOpeningVisualSource(
                [(path, {"provider": "pexels", "asset_id": "a1"})]
            )
            with patch("clean_v2.opening_director.probe_duration", return_value=120.0):
                with self.assertRaisesRegex(
                    CleanV2OpeningBlock,
                    "insufficient_distinct_stock_candidates",
                ):
                    run_opening_director(
                        output_dir=root,
                        plan=_plan(),
                        script=_script(),
                        rights=rights,
                        fmt="film",
                        narration_path=root / "voice.wav",
                        visual_source=source,
                    )

    def test_opening_vision_infrastructure_is_not_content_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rights = _prepare_primary(root)
            path1 = root / "visuals" / ".candidate-1.mp4"
            path2 = root / "visuals" / ".candidate-2.mp4"
            path1.write_bytes(b"a" * 2048)
            path2.write_bytes(b"b" * 2048)
            source = _FakeOpeningVisualSource(
                [
                    (path1, {"provider": "pexels", "asset_id": "a1"}),
                    (path2, {"provider": "pixabay", "asset_id": "a2"}),
                ]
            )
            with patch("clean_v2.opening_director.probe_duration", return_value=120.0), patch(
                "clean_v2.opening_director._candidate_audit",
                side_effect=CleanV2VisualQAInfrastructure("mesh unavailable"),
            ):
                with self.assertRaisesRegex(
                    CleanV2OpeningInfrastructure,
                    "CLEAN_V2_OPENING_INFRASTRUCTURE",
                ):
                    run_opening_director(
                        output_dir=root,
                        plan=_plan(),
                        script=_script(),
                        rights=rights,
                        fmt="film",
                        narration_path=root / "voice.wav",
                        visual_source=source,
                    )

    def test_journal_classifies_opening_block_and_infrastructure_separately(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for message, expected_status, expected_class in (
                ("CLEAN_V2_OPENING_BLOCK reason=two_opening_auxiliaries_not_final_cut_ready", "quality_pending", "new-layer-block"),
                ("CLEAN_V2_OPENING_INFRASTRUCTURE reason=opening_stock_providers_unavailable", "failed", "infrastructure"),
            ):
                journal = _Journal(root / (expected_class + ".json"), runner_sha="b" * 40, engine_sha="a" * 40)
                journal.payload["stages"] = [
                    {"name": name, "status": "pass"}
                    for name in STAGES[: STAGES.index(OPENING_STAGE)]
                ]
                with self.assertRaises(RuntimeError):
                    journal.run(OPENING_STAGE, lambda m=message: (_ for _ in ()).throw(RuntimeError(m)))
                self.assertEqual(journal.payload["status"], expected_status)
                self.assertEqual(journal.payload["failure_classification"], expected_class)
                if expected_status == "quality_pending":
                    self.assertEqual(journal.payload["quality_pending_stage"], OPENING_STAGE)
                else:
                    self.assertNotIn("quality_pending_stage", journal.payload)

    def test_renderer_uses_exact_7_11_12_then_body_from_second_30(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            narration = root / "voice.wav"
            output = root / "final.mp4"
            paths = [
                root / "opening-cold_open.mp4",
                root / "opening-escalation.mp4",
                root / "body1.mp4",
                root / "body2.mp4",
                root / "body3.mp4",
            ]
            for path in paths:
                path.write_bytes(b"x" * 2048)
            (root / "opening-director.json").write_text(
                json.dumps(
                    {
                        "status": "pass",
                        "mode": "legacy_first_30_three_audited_shots",
                        "slots": [
                            {"local_file": "opening-cold_open.mp4", "seconds": 7.0},
                            {"local_file": "opening-escalation.mp4", "seconds": 11.0},
                            {"local_file": "body1.mp4", "seconds": 12.0},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            captured: list[list[str]] = []

            def fake_run(command, *, timeout):
                captured.append(command)
                if str(command[-1]).endswith(".rendering.mp4"):
                    Path(command[-1]).write_bytes(b"x" * 2048)
                return None

            with patch("clean_v2.media.probe_duration", return_value=120.0), patch(
                "clean_v2.media._run", side_effect=fake_run
            ):
                render_video(narration, paths, output, "film")

            # render_video() now pre-trims/grades each body clip in its own
            # ffmpeg pass (visual pacing + color grading) before the final
            # concat pass - with no rights-manifest.json here, each of the
            # two body clips is its own ungrouped pass (no dissolve), so the
            # 45.12s body slot shows up on those pre-trim passes' -vf
            # argument, not the final filter_complex.
            body_slot = ((120.0 - 30.0) / 2.0) + 0.12
            trim_calls = [
                command
                for command in captured
                if "-vf" in command
                and f"trim=duration={body_slot:.3f}" in command[command.index("-vf") + 1]
            ]
            self.assertEqual(len(trim_calls), 2)

            final_command = captured[-1]
            filters = final_command[final_command.index("-filter_complex") + 1]
            self.assertIn("trim=duration=7.000", filters)
            self.assertIn("trim=duration=11.000", filters)
            self.assertIn("trim=duration=12.000", filters)
            self.assertNotIn(f"trim=duration={body_slot:.3f}", filters)


if __name__ == "__main__":
    unittest.main()
