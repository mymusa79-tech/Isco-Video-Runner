from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from isco_video_agent.brief_approval_binding import attach_approval_binding
from scripts import canonical_v4_bundle as bundle
from scripts.packaging_delivery_contract import seal_gold_packaging_acceptance


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _editorial_intent() -> dict:
    return {
        "editorial_thesis": "التأجيل يتغذى على انتظار شعور كامل بالاستعداد قبل الحركة.",
        "viewer_starting_belief": "المشاهد يعتقد أن نقص الدافع هو السبب المباشر لعدم البدء.",
        "hidden_assumption": "الافتراض الخفي أن الثقة يجب أن تسبق أي خطوة عملية صغيرة.",
        "editorial_turn": "التحول أن الحركة الصغيرة يمكن أن تسبق الثقة وتبنيها تدريجيًا.",
        "stakes": "استمرار الانتظار يجعل المهام الصغيرة تبدو أكبر ويطيل دائرة الجمود.",
        "viewer_promise": "سيفهم المشاهد لماذا تكفي بداية صغيرة لكسر انتظار الاستعداد الكامل.",
        "evidence_boundaries": ["نلتزم بما تثبته الحلقة الأم ولا نضيف ادعاءات جديدة."],
        "earned_payoff": "يخرج المشاهد بخطوة واحدة صغيرة يبدأ بها اليوم بدل انتظار الدافع.",
        "persona_version": 1,
    }


def _source_plan() -> dict:
    return {
        "topic": "موضوع معتمد",
        "pillar": "understand",
        "format": "film",
        "hook": "خطاف",
        "editorial_intent": _editorial_intent(),
        "sections": [
            {
                "id": f"s{i}",
                "key_point": f"فكرة مستقلة {i}",
                "narration": f"هذه رواية القسم {i}. تحمل معنى مستقلا واضحا للمشاهد.",
                "visual_query": f"visual concept {i}",
                "on_screen_text": f"نص {i}",
                "emotion": "hopeful",
            }
            for i in range(1, 5)
        ],
    }


def _make_long_root(root: Path) -> tuple[dict, str]:
    _write_json(root / "plan.json", _source_plan())
    (root / "final.mp4").write_bytes(b"x" * 2048)
    _write_json(root / "quality-final.json", {"format": "film", "duration_ok": True, "audio_ok": True})
    _write_json(root / "factuality-audit.json", {"status": "pass"})
    _write_json(root / "quality-precheck.json", {"research_source_count": 3})
    critic = {
        "status": "pass",
        "hard_blocks": [],
        "model_review": {
            "status": "pass",
            "critical_issues": [],
            "opening_strength": 0.92,
            "narrative_progression": 0.91,
            "human_feel": 0.93,
            "cultural_fit": 0.96,
        },
    }
    _write_json(root / "final-critic.json", critic)
    _write_json(
        root / "gold-enforce-report.json",
        {
            "phase": "4",
            "mode": "enforce",
            "release_authority": "gold",
            "single_render": True,
            "gold": {"accepted": True},
            "same_render": {"artifact_divergence": False},
        },
    )
    candidates = []
    thumb_rights = []
    for i in range(1, 4):
        name = f"thumbnail-{i}.jpg"
        (root / name).write_bytes(b"j" * 2048)
        candidates.append(
            {
                "candidate_id": f"c{i}",
                "file": name,
                "experiment_slot": chr(64 + i),
                "title_ar": f"عنوان {i}",
                "text_ar": f"نص {i}",
                "packaging_hypothesis": "اختبار",
            }
        )
        thumb_rights.append(
            {
                "output_file": name,
                "provider": "pexels",
                "license_url": "https://www.pexels.com/license/",
                "provider_asset_id": i,
            }
        )
    _write_json(root / "thumbnail-plan.json", {"status": "ready", "candidates": candidates})
    _write_json(root / "rights-manifest.json", {"thumbnails": thumb_rights, "visuals": [{"provider": "pexels"}]})
    _write_json(root / "production-manifest.json", {"format": "film"})
    seal_gold_packaging_acceptance(root, critic=critic)

    brief = attach_approval_binding(
        {
            "approved_by_user": True,
            "approved_topic": "موضوع معتمد",
            "format": "film",
            "approved_at": "2026-08-23T00:00:00Z",
            "weekly_option_id": "w1",
            "research_pack": [{"url": "a"}, {"url": "b"}],
            "content_boundaries": ["safe"],
        }
    )
    brief_path = root / "approved-brief.json"
    _write_json(brief_path, brief)
    return brief, str(brief["approved_hash"])


class CanonicalV4BundleTests(unittest.TestCase):
    def test_parent_uses_approved_brief_and_real_post_gold_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            brief, approved_hash = _make_long_root(root)
            with patch.dict(
                os.environ,
                {
                    "ISCO_APPROVED_BRIEF_PATH": str(root / "approved-brief.json"),
                    "ISCO_APPROVED_BRIEF_SHA256": approved_hash,
                    "ISCO_PRODUCTION_ID": "v4:test:1",
                },
                clear=False,
            ):
                parent = bundle.build_parent_request(root)
            self.assertEqual(parent["source"], bundle.SOURCE)
            self.assertTrue(parent["approval_inherited_from_approved_brief"])
            self.assertFalse(parent["production_dispatch_authorized"])
            self.assertEqual(parent["youtube_publish_mode"], "manual_in_youtube_studio")
            self.assertEqual(parent["parent_approved_brief_sha256"], approved_hash)
            self.assertEqual(parent["candidate"]["hook_potential"], 0.92)
            self.assertEqual(parent["candidate"]["retention_potential"], 0.91)
            self.assertEqual(parent["candidate"]["score_origin"], "post_gold_source_episode_evidence_only")
            self.assertIn("opening_strength", parent["candidate"]["evidence_map"]["hook_potential"])
            self.assertEqual(parent["request_sha256"], bundle._canonical_hash(parent))

    def test_child_requests_are_distinct_source_derived_and_non_dispatching(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _, approved_hash = _make_long_root(root)
            with patch.dict(
                os.environ,
                {
                    "ISCO_APPROVED_BRIEF_PATH": str(root / "approved-brief.json"),
                    "ISCO_APPROVED_BRIEF_SHA256": approved_hash,
                },
                clear=False,
            ):
                parent = bundle.build_parent_request(root)
                sibling_plan = bundle.build_sibling_plan(root, parent)
                children = bundle.build_child_requests(parent, sibling_plan, _source_plan())
            self.assertIn(len(children), {2, 3})
            self.assertEqual(len({x["approved_topic"] for x in children}), len(children))
            self.assertEqual(
                [request["source_short_plan"]["template"] for request in children],
                ["why_reframe"] * len(children),
            )
            for request in children:
                self.assertEqual(request["source"], bundle.SOURCE)
                self.assertEqual(request["format"], "moment")
                self.assertFalse(request["production_dispatch_authorized"])
                self.assertTrue(request["approval_inherited_from_approved_brief"])
                self.assertTrue(request["approval_inherited_from_parent_bundle"])
                self.assertEqual(request["youtube_publish_mode"], "manual_in_youtube_studio")
                self.assertEqual(len(request["source_episode_excerpt"]["source_narration_sha256"]), 64)
                self.assertIsInstance(request["source_short_plan"], dict)
                self.assertEqual(
                    request["source_editorial_intent"]["editorial_thesis"],
                    _editorial_intent()["editorial_thesis"],
                )
                self.assertTrue(request["source_editorial_intent"]["editorial_fingerprint"])
                self.assertEqual(request["request_sha256"], bundle._canonical_hash(request))

    def test_missing_long_editorial_intent_fails_before_child_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _, approved_hash = _make_long_root(root)
            source_plan = _source_plan()
            source_plan.pop("editorial_intent")
            with patch.dict(
                os.environ,
                {
                    "ISCO_APPROVED_BRIEF_PATH": str(root / "approved-brief.json"),
                    "ISCO_APPROVED_BRIEF_SHA256": approved_hash,
                },
                clear=False,
            ):
                parent = bundle.build_parent_request(root)
                sibling_plan = bundle.build_sibling_plan(root, parent)
                with self.assertRaisesRegex(RuntimeError, "source long EditorialIntent"):
                    bundle.build_child_requests(parent, sibling_plan, source_plan)

    def test_bundle_is_skipped_for_moment_or_explicit_control_plane(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_json(root / "plan.json", {"format": "moment"})
            self.assertIsNone(bundle.build_canonical_v4_bundle(root))

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_json(root / "plan.json", {"format": "film"})
            with patch.dict(os.environ, {"ISCO_CONTROL_REQUEST_ID": "telegram-explicit"}, clear=False):
                self.assertIsNone(bundle.build_canonical_v4_bundle(root))

    def test_child_failure_blocks_partial_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _, approved_hash = _make_long_root(root)
            with patch.dict(
                os.environ,
                {
                    "ISCO_APPROVED_BRIEF_PATH": str(root / "approved-brief.json"),
                    "ISCO_APPROVED_BRIEF_SHA256": approved_hash,
                },
                clear=False,
            ), patch.object(bundle, "_execute_child", side_effect=RuntimeError("child failed")):
                with self.assertRaisesRegex(RuntimeError, "child failed"):
                    bundle.build_canonical_v4_bundle(root)
            self.assertFalse((root / "delivery-manifest.json").exists())
            self.assertFalse((root / "sibling-short-results.json").exists())

    def test_child_subprocess_has_hard_timeout_and_fails_closed(self) -> None:
        request = {"request_id": "canonical-x-s1", "request_sha256": "a" * 64}
        with tempfile.TemporaryDirectory() as td, patch.object(
            bundle.subprocess,
            "run",
            side_effect=bundle.subprocess.TimeoutExpired(cmd=["python"], timeout=bundle.SHORT_CHILD_TIMEOUT_SECONDS),
        ) as run:
            with self.assertRaisesRegex(RuntimeError, "sibling child timed out"):
                bundle._execute_child(request, runtime_root=Path(td))
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.kwargs["timeout"], bundle.SHORT_CHILD_TIMEOUT_SECONDS)
        self.assertTrue(run.call_args.kwargs["check"])

    def test_tampered_approved_brief_hash_fails_before_child_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _, _ = _make_long_root(root)
            with patch.dict(
                os.environ,
                {
                    "ISCO_APPROVED_BRIEF_PATH": str(root / "approved-brief.json"),
                    "ISCO_APPROVED_BRIEF_SHA256": "0" * 64,
                },
                clear=False,
            ), patch.object(bundle, "_execute_child") as execute:
                with self.assertRaises(Exception):
                    bundle.build_parent_request(root)
                execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
