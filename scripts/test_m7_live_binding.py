from __future__ import annotations

import os
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from isco_video_agent.models import ProductionPlan, ScriptSection

import scripts.m7_live_binding as bridge


class M7RunnerInstallerTests(unittest.TestCase):
    def tearDown(self) -> None:
        current = bridge.orchestrator.produce
        original = getattr(current, "_isco_m7_original", None)
        if original is not None:
            bridge.orchestrator.produce = original

    def test_installer_is_idempotent_captures_keys_and_chains_security(self) -> None:
        calls = []

        def core(*args, **kwargs):
            calls.append(("core", args, kwargs))
            return "out"

        @contextmanager
        def scope(module, *, pexels_api_key, pixabay_api_key):
            calls.append(("scope", pexels_api_key, pixabay_api_key, module is bridge.orchestrator))
            yield

        # This test owns only the M7 installer/idempotence contract. Keep the optional
        # M11 archive lane out of scope here; dedicated tests below exercise its new
        # Security V1 and color-authority handoffs directly.
        with patch.object(bridge.orchestrator, "produce", core), patch.object(
            bridge, "live_m7_binding_scope", scope
        ), patch.object(
            bridge, "_load_m11_runtime", return_value=None
        ), patch.object(
            bridge, "install_security_v1_live_binding"
        ) as security, patch.dict(
            os.environ,
            {"PEXELS_API_KEY": "pexels-secret", "PIXABAY_API_KEY": "pixabay-secret"},
            clear=False,
        ):
            bridge.install_m7_live_binding()
            wrapped = bridge.orchestrator.produce
            bridge.install_m7_live_binding()
            self.assertIs(bridge.orchestrator.produce, wrapped)
            result = wrapped(topic="x")

        self.assertEqual(result, "out")
        self.assertEqual(calls[0], ("scope", "pexels-secret", "pixabay-secret", True))
        self.assertEqual(calls[1][0], "core")
        self.assertEqual(security.call_count, 2)

    def test_missing_pexels_preserves_core_authoritative_failure_path(self) -> None:
        calls = []

        def core(*args, **kwargs):
            calls.append("core")
            raise RuntimeError("missing pexels from core")

        with patch.object(bridge.orchestrator, "produce", core), patch.object(
            bridge, "install_security_v1_live_binding"
        ), patch.dict(
            os.environ, {"PEXELS_API_KEY": "", "PIXABAY_API_KEY": ""}, clear=False
        ):
            bridge.install_m7_live_binding()
            with self.assertRaisesRegex(RuntimeError, "missing pexels from core"):
                bridge.orchestrator.produce()
        self.assertEqual(calls, ["core"])

    def test_m11_security_firewall_blocks_before_vision_budget_or_cloud_review(self) -> None:
        ledger = MagicMock()
        audit = MagicMock()
        candidate = SimpleNamespace(provider=SimpleNamespace(value="the_met"), object_id="42")
        review = bridge._m11_review_fn(
            output_dir=Path("out"),
            gemini_api_key="gemini",
            content_model="gemini-2.5-flash",
            ledger=ledger,
            audit_fn=audit,
        )
        with patch.object(
            bridge.security_v1,
            "_scan_media_before_vision",
            side_effect=RuntimeError(
                bridge.security_v1._FIREWALL_BLOCK_PREFIX + "prompt_like_text_detected"
            ),
        ):
            result = review(Path("archive.jpg"), {"body_index": 0}, candidate)

        self.assertEqual(result["status"], "block")
        self.assertEqual(result["local_media_rejection"], "prompt_like_text_detected")
        ledger.register_task.assert_not_called()
        ledger.authorize.assert_not_called()
        audit.assert_not_called()

    def test_m11_archive_render_reenters_live_prepare_clip_color_authority(self) -> None:
        runtime = SimpleNamespace(_render_archive_clip=MagicMock())
        render = bridge._m11_color_authority_render_fn(runtime)
        destination = Path("archive-final.mp4")
        expected_raw = Path("archive-final.m11-pre-color.mp4")
        with patch.object(
            bridge.media_ffmpeg, "prepare_clip", return_value=destination
        ) as prepare:
            result = render(Path("archive.jpg"), destination, 7.5, fps=30)

        self.assertEqual(result, destination)
        runtime._render_archive_clip.assert_called_once_with(
            Path("archive.jpg"), expected_raw, 7.5, fps=30
        )
        prepare.assert_called_once_with(
            expected_raw, destination, 7.5, portrait=False, fps=30
        )

    def test_m7_semantic_body_resolves_live_m8_prepare_instead_of_import_time_default(self) -> None:
        captured = {}

        def materialize(_timeline, **kwargs):
            captured.update(kwargs)
            return [], [], []

        def live_prepare(*_args, **_kwargs):
            return Path("prepared.mp4")

        with patch.object(bridge.engine_m7, "materialize_semantic_body", materialize), patch.object(
            bridge.media_ffmpeg, "prepare_clip", live_prepare
        ):
            with bridge._m7_dynamic_prepare_scope():
                bridge.engine_m7.materialize_semantic_body({"final_cut_visuals": []})

        self.assertIs(captured["prepare_clip_fn"], live_prepare)

    def test_m7_timeline_preserves_every_adaptive_opening_clip_and_duration(self) -> None:
        plan = ProductionPlan(
            topic="topic",
            pillar="understand",
            format="film",
            hook="hook",
            title_options=["title"],
            thumbnail_concepts=["thumb"],
            sections=[
                ScriptSection("s1", "opening", "opening visual"),
                ScriptSection("s2", "body", "body visual"),
            ],
            cta="cta",
            closing_payoff="payoff",
        )
        slots = (
            ("cold_open", 1, False, True),
            ("escalation", 2, False, True),
            ("promise", 3, True, False),
            ("body_1", 4, False, False),
        )
        audits = [{
            "section": "s1",
            "status": "block",
            "provider": "pexels",
            "candidate_id": 999,
            "reason": "earlier rejected review",
        }] + [
            {
                "section": "s1",
                "status": "pass",
                "opening_slot": slot,
                "provider": "pexels",
                "candidate_id": asset_id,
                "is_selected": selected,
                "is_final_cut_auxiliary": auxiliary,
                "is_section_sequence_member": slot == "body_1",
            }
            for slot, asset_id, selected, auxiliary in slots
        ] + [{
            "section": "s2",
            "status": "pass",
            "provider": "pixabay",
            "candidate_id": 5,
            "is_selected": True,
            "is_final_cut_auxiliary": False,
        }]
        original = bridge.engine_m7._legacy_entries_from_audits
        with bridge._m7_adaptive_opening_scope():
            entries = bridge.engine_m7._legacy_entries_from_audits(
                plan,
                section_durations=[62.3, 20.0],
                visual_audits=audits,
            )

        self.assertEqual(
            [entry["slot_key"] for entry in entries],
            ["cold_open", "escalation", "promise", "body_1", "single"],
        )
        self.assertAlmostEqual(sum(entry["duration_seconds"] for entry in entries[:4]), 62.3)
        self.assertAlmostEqual(entries[4]["duration_seconds"], 20.0)
        self.assertEqual(
            [entry["final_cut_audit_reference"]["json_pointer"] for entry in entries],
            ["/1", "/2", "/3", "/4", "/5"],
        )
        self.assertIs(bridge.engine_m7._legacy_entries_from_audits, original)


if __name__ == "__main__":
    unittest.main()
