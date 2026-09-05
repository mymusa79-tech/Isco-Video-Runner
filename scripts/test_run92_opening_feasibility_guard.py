from __future__ import annotations

import unittest
from unittest.mock import patch

import isco_video_agent.opening_director as opening_director
import isco_video_agent.orchestrator as orchestrator
import isco_video_agent.section_visual_sequence as section_visual_sequence
import isco_video_agent.final_critic as final_critic
from isco_video_agent.visual_selection import VisualCandidateCache, VisualSelectionResult

from scripts.opening_feasibility_guard import (
    SHORT_VISUAL_QUALITY_FLOOR_CONTRACT,
    SHORT_VISUAL_QUALITY_MINIMUM,
    SHORT_VISUAL_RELEVANCE_MINIMUM,
    STOCK_CANDIDATE_POOL,
    _adaptive_review_cap,
    _adaptive_section_review_cap,
    _format_aware_single_audit,
    _install_selection_wrappers,
    _install_stock_search_wrappers,
    _log_candidate_pool_size,
    _normalize_adaptive_opening_audits,
    _preserve_outline_visual_intent,
    _short_visual_quality_floor,
    _stable_intent_audit,
    adaptive_opening_slot_specs,
    stock_safe_search_query,
)


def _candidate(asset_id: int, seconds: float) -> dict:
    return {
        "id": asset_id,
        "duration": seconds,
        "video_files": [
            {
                "link": f"https://videos.pexels.com/video-{asset_id}.mp4",
                "width": 1920,
                "height": 1080,
            }
        ],
    }


class Run92OpeningFeasibilityGuardTests(unittest.TestCase):
    def test_run92_query_keeps_environment_semantics_for_search_only(self) -> None:
        original = "person sitting by wooden table in sunlit room looking pensively at empty notebook"
        self.assertEqual(
            stock_safe_search_query(original),
            "wooden table sunlit room empty notebook",
        )

    def test_safe_non_identifiable_framing_is_preserved(self) -> None:
        self.assertEqual(stock_safe_search_query("hands writing notebook"), "hands writing notebook")
        self.assertEqual(
            stock_safe_search_query("silhouette person walking road at sunset"),
            "silhouette person walking road at sunset",
        )

    def test_human_only_query_uses_broad_safe_fallback(self) -> None:
        self.assertEqual(
            stock_safe_search_query("person sitting alone thinking"),
            "quiet room natural light",
        )

    def test_outline_keeps_original_visual_intent_and_adds_search_derivative(self) -> None:
        original = "person sitting by wooden table in sunlit room looking pensively at empty notebook"
        outline = {"section_briefs": [{"id": "s1", "visual_query": original}]}
        result = _preserve_outline_visual_intent(outline, fmt="film")
        self.assertEqual(result["section_briefs"][0]["visual_query"], original)
        self.assertEqual(
            result["section_briefs"][0]["stock_search_query"],
            "wooden table sunlit room empty notebook",
        )

    def test_run92_duration_becomes_fixed_30s_opening_plus_stock_friendly_tail(self) -> None:
        specs = adaptive_opening_slot_specs(62.3)
        self.assertEqual([slot.key for slot in specs], ["cold_open", "escalation", "promise", "body_1"])
        self.assertAlmostEqual(sum(slot.seconds for slot in specs), 62.3)
        self.assertEqual([slot.seconds for slot in specs[:3]], [7.0, 11.0, 12.0])
        self.assertAlmostEqual(specs[3].seconds, 32.3)
        self.assertTrue(all(slot.seconds <= 45.0 for slot in specs))

    def test_long_first_section_tail_is_split_without_looping_requirement(self) -> None:
        specs = adaptive_opening_slot_specs(100.0)
        self.assertEqual([slot.key for slot in specs], ["cold_open", "escalation", "promise", "body_1", "body_2"])
        self.assertAlmostEqual(sum(slot.seconds for slot in specs), 100.0)
        self.assertAlmostEqual(specs[3].seconds, 35.0)
        self.assertAlmostEqual(specs[4].seconds, 35.0)
        self.assertTrue(all(slot.seconds <= 45.0 for slot in specs))

    def test_135s_engine_ceiling_remains_bounded(self) -> None:
        specs = adaptive_opening_slot_specs(135.0)
        self.assertEqual(len(specs), 6)
        self.assertAlmostEqual(sum(slot.seconds for slot in specs), 135.0)
        self.assertTrue(all(slot.seconds <= 45.0 for slot in specs))
        self.assertEqual(_adaptive_review_cap(135.0), 8)

    def test_short_first_section_still_uses_legacy_path(self) -> None:
        self.assertEqual(adaptive_opening_slot_specs(25.0), [])

    def test_run105_all_opening_geometries_reserve_two_rejection_reviews(self) -> None:
        self.assertEqual(_adaptive_review_cap(30.0), 5)
        self.assertEqual(_adaptive_review_cap(62.3), 6)
        self.assertEqual(_adaptive_review_cap(100.0), 7)
        self.assertEqual(_adaptive_review_cap(135.0), 8)

    def test_run105_same_failure_class_body_review_cap_is_bounded(self) -> None:
        self.assertEqual(_adaptive_section_review_cap(45.0), 4)
        self.assertEqual(_adaptive_section_review_cap(62.3), 4)
        self.assertEqual(_adaptive_section_review_cap(100.0), 5)
        self.assertEqual(_adaptive_section_review_cap(135.0), 5)

    def test_run92_geometry_can_select_four_distinct_real_assets(self) -> None:
        calls: list[int] = []

        def audit_fn(**kwargs):
            calls.append(int(kwargs["candidate"]["id"]))
            return {"status": "pass", "relevance": 0.95, "visual_quality": 0.95}

        with patch.object(opening_director, "opening_slot_specs", adaptive_opening_slot_specs):
            result = opening_director.select_opening_sequence(
                {
                    "pexels": [
                        _candidate(1, 40),
                        _candidate(2, 20),
                        _candidate(3, 15),
                        _candidate(4, 10),
                        _candidate(5, 8),
                    ]
                },
                section_seconds=62.3,
                portrait=False,
                narration_context="opening narration",
                intended_visual="original visual intent",
                audit_fn=audit_fn,
                cache=VisualCandidateCache(excluded_assets={}),
                max_reviews=_adaptive_review_cap(62.3),
            )

        self.assertEqual(result.status, "selected")
        self.assertEqual(len(result.slots), 4)
        selected_ids = [slot.review.candidate["id"] for slot in result.slots]
        self.assertEqual(len(set(selected_ids)), 4)
        self.assertLessEqual(len(calls), 6)

    def test_adaptive_opening_projects_one_composite_and_only_two_gold_auxiliaries(self) -> None:
        def audit_fn(**_kwargs):
            return {
                "status": "pass",
                "relevance": 0.91,
                "visual_quality": 0.89,
            }

        with patch.object(opening_director, "opening_slot_specs", adaptive_opening_slot_specs):
            result = opening_director.select_opening_sequence(
                {"pexels": [_candidate(index, 70) for index in range(1, 6)]},
                section_seconds=62.3,
                portrait=False,
                narration_context="opening narration",
                intended_visual="original visual intent",
                audit_fn=audit_fn,
                cache=VisualCandidateCache(excluded_assets={}),
                max_reviews=_adaptive_review_cap(62.3),
            )
        _normalize_adaptive_opening_audits(result, section_seconds=62.3)

        audits = [{"section": "s1", **dict(review.audit)} for review in result.reviewed]
        selected = [item for item in audits if item.get("is_selected") is True]
        auxiliaries = [item for item in audits if item.get("is_final_cut_auxiliary") is True]
        members = [item for item in audits if item.get("is_section_sequence_member") is True]
        self.assertEqual(len(selected), 1)
        self.assertTrue(selected[0]["is_adaptive_opening_composite"])
        self.assertEqual(
            [item["slot"] for item in selected[0]["sequence_members"]],
            ["promise", "body_1"],
        )
        self.assertEqual(
            {item["opening_slot"] for item in auxiliaries},
            {"cold_open", "escalation"},
        )
        self.assertEqual([item["opening_slot"] for item in members], ["body_1"])
        blocks = final_critic._deterministic_blocks(
            quality={"duration_ok": True, "audio_ok": True},
            visual_audits=audits,
            rights_manifest={"visuals": [{"provider": "pexels"}]},
            monetization_check={"status": "PASS"},
            opening_visual_audit={"status": "pass"},
            section_ids=["s1"],
        )
        self.assertNotIn("section_visual_selection_integrity_failed", blocks)

    def test_adaptive_opening_composite_fails_closed_when_member_scores_are_missing(self) -> None:
        def audit_fn(**kwargs):
            if int(kwargs["candidate"]["id"]) == 4:
                return {"status": "pass", "relevance": 0.91}
            return {"status": "pass", "relevance": 0.91, "visual_quality": 0.89}

        with patch.object(opening_director, "opening_slot_specs", adaptive_opening_slot_specs):
            result = opening_director.select_opening_sequence(
                {"pexels": [_candidate(index, 70) for index in range(1, 6)]},
                section_seconds=62.3,
                portrait=False,
                narration_context="opening narration",
                intended_visual="original visual intent",
                audit_fn=audit_fn,
                cache=VisualCandidateCache(excluded_assets={}),
                max_reviews=_adaptive_review_cap(62.3),
            )
        _normalize_adaptive_opening_audits(result, section_seconds=62.3)

        primary = next(slot.review.audit for slot in result.slots if slot.spec.key == "promise")
        self.assertEqual(primary["status"], "block")
        self.assertIn("lacks a complete passing score", primary["reason"])

    def test_run105_normal_three_slot_opening_recovers_after_two_semantic_blocks(self) -> None:
        calls: list[int] = []

        def audit_fn(**kwargs):
            calls.append(int(kwargs["candidate"]["id"]))
            if len(calls) <= 2:
                return {"status": "block", "reason": "semantic mismatch"}
            return {"status": "pass", "relevance": 0.95, "visual_quality": 0.95}

        with patch.object(opening_director, "opening_slot_specs", adaptive_opening_slot_specs):
            result = opening_director.select_opening_sequence(
                {
                    "pexels": [
                        _candidate(151, 40),
                        _candidate(152, 40),
                        _candidate(153, 40),
                        _candidate(154, 40),
                        _candidate(155, 40),
                    ]
                },
                section_seconds=30.0,
                portrait=False,
                narration_context="opening narration",
                intended_visual="original visual intent",
                audit_fn=audit_fn,
                cache=VisualCandidateCache(excluded_assets={}),
                max_reviews=_adaptive_review_cap(30.0),
            )

        self.assertEqual(result.status, "selected")
        self.assertEqual(len(result.slots), 3)
        self.assertEqual(len(calls), 5)
        selected_ids = [slot.review.candidate["id"] for slot in result.slots]
        self.assertEqual(len(set(selected_ids)), 3)

    def test_run105_four_slots_recover_after_two_semantic_blocks(self) -> None:
        calls: list[int] = []

        def audit_fn(**kwargs):
            calls.append(int(kwargs["candidate"]["id"]))
            if len(calls) <= 2:
                return {"status": "block", "reason": "semantic mismatch"}
            return {"status": "pass", "relevance": 0.95, "visual_quality": 0.95}

        with patch.object(opening_director, "opening_slot_specs", adaptive_opening_slot_specs):
            result = opening_director.select_opening_sequence(
                {
                    "pexels": [
                        _candidate(201, 70),
                        _candidate(202, 70),
                        _candidate(203, 70),
                        _candidate(204, 70),
                        _candidate(205, 70),
                        _candidate(206, 70),
                    ]
                },
                section_seconds=62.3,
                portrait=False,
                narration_context="opening narration",
                intended_visual="original visual intent",
                audit_fn=audit_fn,
                cache=VisualCandidateCache(excluded_assets={}),
                max_reviews=_adaptive_review_cap(62.3),
            )

        self.assertEqual(result.status, "selected")
        self.assertEqual(len(result.slots), 4)
        self.assertEqual(len(calls), 6)
        selected_ids = [slot.review.candidate["id"] for slot in result.slots]
        self.assertEqual(len(set(selected_ids)), 4)

    def test_run105_three_slot_body_recovers_after_two_semantic_blocks(self) -> None:
        calls: list[int] = []

        def audit_fn(**kwargs):
            calls.append(int(kwargs["candidate"]["id"]))
            if len(calls) <= 2:
                return {"status": "block", "reason": "semantic mismatch"}
            return {"status": "pass", "relevance": 0.95, "visual_quality": 0.95}

        result = section_visual_sequence.select_section_sequence(
            {
                "pexels": [
                    _candidate(301, 70),
                    _candidate(302, 70),
                    _candidate(303, 70),
                    _candidate(304, 70),
                    _candidate(305, 70),
                ]
            },
            section_seconds=100.0,
            portrait=False,
            narration_context="body narration",
            intended_visual="original body visual intent",
            audit_fn=audit_fn,
            cache=VisualCandidateCache(excluded_assets={}),
            max_reviews=_adaptive_section_review_cap(100.0),
        )

        self.assertEqual(result.status, "selected")
        self.assertEqual(len(result.slots), 3)
        self.assertEqual(len(calls), 5)
        selected_ids = [slot.review.candidate["id"] for slot in result.slots]
        self.assertEqual(len(set(selected_ids)), 3)

    def test_alternate_search_never_redefines_vision_intent(self) -> None:
        seen_intents: list[str] = []
        original_intent = "person at a desk resisting outside pressure"

        def raw_audit(**kwargs):
            seen_intents.append(str(kwargs["intended_visual"]))
            return {"status": "pass", "relevance": 0.9, "visual_quality": 0.9}

        stable_audit = _stable_intent_audit(raw_audit, original_intent)

        with patch.object(opening_director, "opening_slot_specs", adaptive_opening_slot_specs):
            result = opening_director.select_opening_sequence(
                {"pexels": []},
                section_seconds=30.0,
                portrait=False,
                narration_context="opening narration",
                intended_visual=original_intent,
                audit_fn=stable_audit,
                cache=VisualCandidateCache(excluded_assets={}),
                alternate_query_fn=lambda: "empty desk notebook window",
                alternate_search_fn=lambda _query: {
                    "pexels": [_candidate(101, 20), _candidate(102, 15), _candidate(103, 10)]
                },
                max_reviews=4,
            )

        self.assertEqual(result.status, "selected")
        self.assertEqual(seen_intents, [original_intent, original_intent, original_intent])

    def test_stock_pool_wrapper_expands_only_core_12_result_request(self) -> None:
        import isco_video_agent.orchestrator as orchestrator

        calls: list[tuple[str, int]] = []

        def fake_pexels(_key, query, orientation="landscape", per_page=15):
            calls.append((query, per_page))
            return []

        with patch.object(orchestrator, "pexels_search_videos", fake_pexels):
            _install_stock_search_wrappers()
            orchestrator.pexels_search_videos(
                "key",
                "person sitting by wooden table in sunlit room looking pensively at empty notebook",
                orientation="landscape",
                per_page=12,
            )
            orchestrator.pexels_search_videos("key", "forest road", per_page=7)

        self.assertEqual(calls[0], ("wooden table sunlit room empty notebook", STOCK_CANDIDATE_POOL))
        self.assertEqual(calls[1], ("forest road", 7))

    def test_run93_guard_shortens_before_a_previously_installed_length_validator(self) -> None:
        """Regression for Run #93: the guard must wrap AROUND Security V1, not be wrapped
        BY it. Security V1's real stock-search wrapper validates query length before
        calling the original function. When it is installed first (as
        install_m7_live_binding() does as a side effect) and this guard installs after,
        the guard must become the outer layer so its shortening runs before that
        validator ever sees the long original query - otherwise a long human-subject
        opening query is rejected as visual_query_too_long before it can be shortened.
        """
        import isco_video_agent.orchestrator as orchestrator

        calls: list[str] = []

        def fake_pexels(_key, query, orientation="landscape", per_page=15):
            calls.append(query)
            return []

        max_allowed_length = 40

        def validating_wrapper(*args, **kwargs):
            query = args[1] if len(args) >= 2 else kwargs["query"]
            if len(str(query)) > max_allowed_length:
                raise RuntimeError("visual_query_too_long")
            return fake_pexels(*args, **kwargs)

        original = "person sitting by wooden table in sunlit room looking pensively at empty notebook"
        self.assertGreater(len(original), max_allowed_length)

        with patch.object(orchestrator, "pexels_search_videos", validating_wrapper):
            _install_stock_search_wrappers()
            orchestrator.pexels_search_videos("key", original, orientation="landscape", per_page=12)

        self.assertEqual(calls, ["wooden table sunlit room empty notebook"])


class VisionProviderFailureResilienceTests(unittest.TestCase):
    """Regression for a real 2026-09-01 production failure: a Gemini Vision candidate
    review hit a real 429 quota error (google.genai._gaos.lib.compat_errors.RateLimitError)
    and it crashed the entire production instead of skipping that one candidate and
    trying the next, even though visual_selection.review_candidates() already has a
    bounded multi-candidate recovery loop built for exactly this kind of per-candidate
    failure. provider_retry_ownership.py deliberately forbids a retry loop *inside*
    Engine's own Vision boundaries, so the fix belongs in this Runner-owned audit_fn
    wrapper instead."""

    def test_transient_provider_failure_is_converted_to_a_skippable_block(self) -> None:
        def raising_audit(**kwargs):
            raise RuntimeError("Error code: 429 - quota exceeded, please retry in 43s")

        wrapped = _stable_intent_audit(raising_audit, "a calm person at a desk")
        result = wrapped(provider="pexels", candidate={"id": 1}, narration_context="ctx")

        self.assertEqual(result["status"], "block")
        self.assertFalse(result["vision_review_performed"])
        self.assertEqual(result["review_origin"], "runner_vision_provider_call_failure")

    def test_timeout_and_network_failures_are_also_treated_as_transient(self) -> None:
        for message in ("Connection timed out", "Network error: connection reset"):
            def raising_audit(**kwargs):
                raise RuntimeError(message)

            wrapped = _stable_intent_audit(raising_audit, "a calm person at a desk")
            result = wrapped(provider="pexels", candidate={"id": 1}, narration_context="ctx")
            self.assertEqual(result["status"], "block")
            self.assertFalse(result["vision_review_performed"])

    def test_non_transient_failure_still_propagates(self) -> None:
        def raising_audit(**kwargs):
            raise RuntimeError("AI budget authorization denied for task X")

        wrapped = _stable_intent_audit(raising_audit, "a calm person at a desk")
        with self.assertRaisesRegex(RuntimeError, "AI budget authorization denied"):
            wrapped(provider="pexels", candidate={"id": 1}, narration_context="ctx")

    def test_programming_bug_still_propagates(self) -> None:
        def raising_audit(**kwargs):
            raise TypeError("unexpected keyword argument")

        wrapped = _stable_intent_audit(raising_audit, "a calm person at a desk")
        with self.assertRaises(TypeError):
            wrapped(provider="pexels", candidate={"id": 1}, narration_context="ctx")

    def test_real_verdict_from_a_successful_call_is_untouched(self) -> None:
        def passing_audit(**kwargs):
            return {"status": "pass", "relevance": 0.9, "visual_quality": 0.9}

        wrapped = _stable_intent_audit(passing_audit, "a calm person at a desk")
        result = wrapped(provider="pexels", candidate={"id": 1}, narration_context="ctx")
        self.assertEqual(result, {"status": "pass", "relevance": 0.9, "visual_quality": 0.9})

    def test_transient_failure_lets_recovery_loop_select_the_next_candidate(self) -> None:
        """End-to-end through the real bounded recovery loop, not just the wrapper unit."""
        attempts: list[int] = []

        def flaky_audit(*, candidate, **kwargs):
            attempts.append(candidate["id"])
            if candidate["id"] == 101:
                raise RuntimeError("Error code: 429 - quota exceeded")
            return {"status": "pass", "relevance": 0.9, "visual_quality": 0.9}

        stable_audit = _stable_intent_audit(flaky_audit, "a calm person at a desk")

        with patch.object(opening_director, "opening_slot_specs", adaptive_opening_slot_specs):
            result = opening_director.select_opening_sequence(
                {
                    "pexels": [
                        _candidate(101, 20),
                        _candidate(102, 15),
                        _candidate(103, 10),
                        _candidate(104, 12),
                    ]
                },
                section_seconds=30.0,
                portrait=False,
                narration_context="opening narration",
                intended_visual="a calm person at a desk",
                audit_fn=stable_audit,
                cache=VisualCandidateCache(excluded_assets={}),
                max_reviews=5,
            )

        self.assertEqual(result.status, "selected")
        self.assertIn(101, attempts)


class ShortVisualQualityFloorTests(unittest.TestCase):
    def test_run201_borderline_pass_is_rejected_for_short(self) -> None:
        audit = _short_visual_quality_floor(
            lambda **_: {
                "status": "pass",
                "relevance": 0.65,
                "visual_quality": 0.65,
                "reason": "setting matches the reflective mood",
                "verdict_authority": "vision",
            }
        )
        result = audit()
        self.assertEqual(result["status"], "block")
        self.assertTrue(result["short_visual_quality_floor_applied"])
        self.assertEqual(
            result["short_visual_quality_floor_contract"],
            SHORT_VISUAL_QUALITY_FLOOR_CONTRACT,
        )
        self.assertEqual(result["verdict_authority"], "vision")

    def test_threshold_passes_and_existing_blocks_are_never_reinterpreted(self) -> None:
        threshold = {
            "status": "pass",
            "relevance": SHORT_VISUAL_RELEVANCE_MINIMUM,
            "visual_quality": SHORT_VISUAL_QUALITY_MINIMUM,
        }
        accepted = _short_visual_quality_floor(lambda **_: threshold)()
        self.assertEqual(accepted["status"], "pass")
        self.assertTrue(accepted["short_visual_quality_floor_evaluated"])
        self.assertEqual(
            accepted["short_visual_quality_floor_contract"],
            SHORT_VISUAL_QUALITY_FLOOR_CONTRACT,
        )

        existing_block = {
            "status": "block",
            "relevance": 0.99,
            "visual_quality": 0.99,
            "cultural_conflict": True,
        }
        self.assertIs(_short_visual_quality_floor(lambda **_: existing_block)(), existing_block)

    def test_format_aware_adapter_keeps_long_floor_and_strengthens_short_only(self) -> None:
        def borderline(**kwargs):
            return {
                "status": "pass",
                "relevance": 0.65,
                "visual_quality": 0.65,
                "seen_intent": kwargs.get("intended_visual"),
            }

        short = _format_aware_single_audit(
            borderline,
            "a person reflecting after a conversation",
            portrait=True,
        )
        long = _format_aware_single_audit(
            borderline,
            "a person reflecting after a conversation",
            portrait=False,
        )
        self.assertEqual(short(intended_visual="search-only words")["status"], "block")
        long_result = long(intended_visual="search-only words")
        self.assertEqual(long_result["status"], "pass")
        self.assertEqual(long_result["seen_intent"], "a person reflecting after a conversation")


class CandidatePoolSizeDiagnosticsTests(unittest.TestCase):
    """A "no safe/relevant candidate" failure gives no way to tell, from the log alone,
    whether the bounded local-inspection budget (visual_selection.py's
    max_candidates_per_attempt/max_total_inspections) was actually the limiting factor,
    or whether Pexels/Pixabay simply returned too few raw results for that query to
    matter. Raising that budget without knowing which is true would be a guess, not a
    fix. This is observability only - no selection behavior changes."""

    def test_logs_total_and_per_provider_counts(self) -> None:
        with patch("builtins.print") as mock_print:
            _log_candidate_pool_size(
                {"pexels": [{"id": 1}, {"id": 2}], "pixabay": [{"id": 3}]},
                scope="single",
            )
        printed = " ".join(str(call.args[0]) for call in mock_print.call_args_list)
        self.assertIn("scope=single", printed)
        self.assertIn("total=3", printed)
        self.assertIn("'pexels': 2", printed)
        self.assertIn("'pixabay': 1", printed)

    def test_non_dict_input_is_ignored_safely(self) -> None:
        with patch("builtins.print") as mock_print:
            _log_candidate_pool_size(None, scope="single")
        mock_print.assert_not_called()

    def test_single_select_path_logs_pool_size_before_selecting(self) -> None:
        original = orchestrator.select_with_recovery

        def fake_select_with_recovery(candidates_by_provider, **kwargs):
            return VisualSelectionResult(
                status="failed", chosen=None, reviewed=[], used_alternate_query=False, alternate_query=None,
            )

        orchestrator.select_with_recovery = fake_select_with_recovery
        try:
            _install_selection_wrappers()
            with patch("scripts.opening_feasibility_guard.print") as mock_print:
                orchestrator.select_with_recovery(
                    {"pexels": [_candidate(101, 15), _candidate(102, 10)]},
                    portrait=False,
                    target_seconds=15.0,
                    narration_context="ctx",
                    intended_visual="a calm person at a desk",
                    audit_fn=lambda **kw: {"status": "pass"},
                    cache=VisualCandidateCache(excluded_assets={}),
                )
        finally:
            orchestrator.select_with_recovery = original
        printed = " ".join(str(call.args[0]) for call in mock_print.call_args_list)
        self.assertIn("scope=single", printed)
        self.assertIn("total=2", printed)


class SingleSelectAlternatePhaseHeadroomTests(unittest.TestCase):
    """Regression for the real production gap PR #506's diagnostic exposed: a section
    fetched 80 raw stock candidates but visual_selection.select_with_recovery()'s own
    defaults let only 7 of them ever get a local preflight look before giving up. This
    proves, against the real Engine function (not a stub), that raising only
    max_total_inspections - never max_candidates_per_attempt, the real paid Vision-call
    ceiling - lets the bounded alternate-query recovery phase reach a passing candidate
    deep in an already-fetched pool that the unmodified defaults cannot reach."""

    _QUARANTINE = {
        "status": "block",
        "review_origin": "local_stock_media_preflight",
        "vision_review_performed": False,
        "local_media_rejection": "qr_code_detected",
    }

    def _flaky_audit(self, *, candidate, **kwargs):
        if candidate["id"] == 125:
            return {"status": "pass", "relevance": 0.9, "visual_quality": 0.9}
        return dict(self._QUARANTINE)

    def test_unmodified_defaults_cannot_reach_a_deep_passing_alternate_candidate(self) -> None:
        from isco_video_agent.visual_selection import select_with_recovery as real_select_with_recovery

        primary = {"pexels": [_candidate(i, 15) for i in range(1, 11)]}
        alternate = {"pexels": [_candidate(100 + i, 15) for i in range(1, 31)]}

        result = real_select_with_recovery(
            primary,
            portrait=False, target_seconds=15.0, narration_context="ctx",
            intended_visual="a calm person at a desk", audit_fn=self._flaky_audit,
            cache=VisualCandidateCache(excluded_assets={}),
            alternate_query_fn=lambda: "alt", alternate_search_fn=lambda _q: alternate,
        )
        self.assertEqual(result.status, "failed")

    def test_generous_max_total_inspections_reaches_the_same_candidate(self) -> None:
        from isco_video_agent.visual_selection import select_with_recovery as real_select_with_recovery

        primary = {"pexels": [_candidate(i, 15) for i in range(1, 11)]}
        alternate = {"pexels": [_candidate(100 + i, 15) for i in range(1, 31)]}

        result = real_select_with_recovery(
            primary,
            portrait=False, target_seconds=15.0, narration_context="ctx",
            intended_visual="a calm person at a desk", audit_fn=self._flaky_audit,
            cache=VisualCandidateCache(excluded_assets={}),
            alternate_query_fn=lambda: "alt", alternate_search_fn=lambda _q: alternate,
            max_total_inspections=STOCK_CANDIDATE_POOL,
        )
        self.assertEqual(result.status, "selected")

    def test_single_select_path_installs_the_generous_default(self) -> None:
        captured: dict = {}

        def fake_select_with_recovery(candidates_by_provider, **kwargs):
            captured.update(kwargs)
            return VisualSelectionResult(
                status="failed", chosen=None, reviewed=[], used_alternate_query=False, alternate_query=None,
            )

        original = orchestrator.select_with_recovery
        orchestrator.select_with_recovery = fake_select_with_recovery
        try:
            _install_selection_wrappers()
            orchestrator.select_with_recovery(
                {"pexels": [_candidate(1, 15)]},
                portrait=False, target_seconds=15.0, narration_context="ctx",
                intended_visual="a calm person at a desk",
                audit_fn=lambda **kw: {"status": "pass"},
                cache=VisualCandidateCache(excluded_assets={}),
            )
        finally:
            orchestrator.select_with_recovery = original
        self.assertEqual(captured.get("max_total_inspections"), STOCK_CANDIDATE_POOL)
        self.assertNotIn("max_candidates_per_attempt", captured)


if __name__ == "__main__":
    unittest.main()
