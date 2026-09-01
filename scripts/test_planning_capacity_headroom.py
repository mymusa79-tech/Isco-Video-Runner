from __future__ import annotations

import inspect
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import planning_capacity_headroom as headroom
from scripts import planning_capacity_profile as profile
from scripts import planning_envelope_preflight as preflight
from scripts import planning_runtime_contract as runtime_contract
from scripts import provider_capacity_margin_audit as media_margin
from scripts import short_planning_repair


class PlanningCapacityHeadroomTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        profile.install_planning_capacity_profile()

    def test_groq_free_envelope_reserves_real_operational_headroom(self):
        self.assertEqual(headroom.groq_operational_headroom_tokens(8_000), 800)
        self.assertGreaterEqual(headroom.GROQ_OPERATIONAL_HEADROOM_RATIO, 0.10)
        # Run 154's 7,993-token request had only seven raw tokens left. The new
        # admission contract must never call that production-safe.
        self.assertGreater(7_993 + headroom.groq_operational_headroom_tokens(8_000), 8_000)

    def test_compact_initial_short_prompt_has_safe_8k_envelope(self):
        prompt = headroom.build_short_initial_prompt(
            topic="كيف تنهض عندما تفقد الدافع تمامًا؟",
            research_context={
                "approved_audience": "جمهور عربي يبحث عن خطوة عملية واضحة " * 30,
                "approved_editorial_direction": "واقعي ومفيد بلا مبالغة " * 30,
                "content_boundaries": ["لا تشخيص طبي ولا ادعاء غير موثق " * 20] * 20,
                "factuality_rule": "لا تحوّل الافتراض إلى حقيقة " * 30,
                "approved_research_pack": [
                    {
                        "title": "مصدر " * 100,
                        "source": "publisher " * 100,
                        "claim": "claim " * 100,
                        "evidence": "evidence " * 100,
                    }
                ]
                * 10,
            },
            avoid_context={"recent": ["موضوع سابق " * 40] * 20},
            revision_note="Standalone Short type is inner_dialogue. " + "keep it concrete " * 20,
        )
        estimate = headroom._assert_short_envelope(prompt, phase="unit_initial")
        limit = int(estimate["provider_tpm_limit"] or 8_000)
        operational = headroom.groq_operational_headroom_tokens(limit)
        self.assertLessEqual(
            int(estimate["estimated_request_tokens"]) + operational,
            limit,
        )
        self.assertLessEqual(
            int(estimate["effective_prompt_utf8_bytes"]),
            profile.SHORT_EFFECTIVE_PROMPT_MAX_UTF8_BYTES,
        )

    def test_worst_case_short_review_has_safe_8k_envelope(self):
        estimate = headroom.worst_case_short_review_capacity(
            "كيف تنهض عندما تفقد الدافع تمامًا؟"
        )
        limit = int(estimate["provider_tpm_limit"] or 8_000)
        operational = headroom.groq_operational_headroom_tokens(limit)
        self.assertLessEqual(
            int(estimate["estimated_request_tokens"]) + operational,
            limit,
        )
        self.assertLessEqual(
            int(estimate["effective_prompt_utf8_bytes"]),
            profile.SHORT_EFFECTIVE_PROMPT_MAX_UTF8_BYTES,
        )

    def test_short_terminal_reset_recovery_waits_once_then_retries_once(self):
        calls: list[int] = []
        sleeps: list[float] = []
        cleared: list[str] = []

        def call():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError(
                    "All free providers failed for planning subtask: "
                    "gemini:quota | groq:GROQ_TPM_WINDOW_BUSY_PRECHECK "
                    "model=openai/gpt-oss-120b remaining=8000 reset_in=1.00s "
                    "action=provider_evidence_failover_without_partial_retry | "
                    "openrouter:preflight_blocked"
                )
            return {"ok": True}

        with patch.object(headroom.time, "sleep", side_effect=lambda value: sleeps.append(value)), patch.object(
            headroom, "_clear_model_window", side_effect=lambda model: cleared.append(model)
        ):
            result = headroom._short_provider_call_with_terminal_recovery(
                call,
                phase="initial",
            )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(calls), 2)
        self.assertEqual(cleared, ["openai/gpt-oss-120b"])
        self.assertEqual(len(sleeps), 1)
        self.assertAlmostEqual(sleeps[0], 2.5, places=2)

    def test_short_terminal_recovery_never_invents_missing_reset(self):
        calls: list[int] = []

        def call():
            calls.append(1)
            raise RuntimeError(
                "All free providers failed for planning subtask: "
                "groq:GROQ_TPM_WINDOW_BUSY_PRECHECK "
                "model=openai/gpt-oss-120b remaining=1 reset_in=unknown"
            )

        with self.assertRaises(RuntimeError):
            headroom._short_provider_call_with_terminal_recovery(call, phase="review")
        self.assertEqual(len(calls), 1)

    def test_short_repair_envelope_keeps_large_margin_below_operational_limit(self):
        # Existing #469 repair cap remains intentionally much smaller than the new
        # initial/review envelope. Use the largest relevant completion reserve (2400)
        # to prove it stays below the 8K limit even after the 10% operational margin.
        prompt_tokens = math.ceil(
            short_planning_repair.SHORT_REPAIR_PROMPT_MAX_BYTES
            / headroom.capacity.GROQ_ESTIMATED_UTF8_BYTES_PER_TOKEN
        )
        worst_total = (
            prompt_tokens
            + 2_400
            + headroom.capacity.GROQ_TOKEN_SAFETY_RESERVE
        )
        self.assertLessEqual(
            worst_total + headroom.groq_operational_headroom_tokens(8_000),
            8_000,
        )

    def test_openrouter_preflight_guard_covers_legacy_short_call_surface(self):
        source = inspect.getsource(headroom._install_openrouter_preflight_guard)
        self.assertIn("router._openrouter_call_with_repair", source)
        self.assertIn("run125.openrouter_preflight_blocked()", source)
        self.assertIn("openrouter_preflight_block_detail", source)

    def test_runtime_installs_profile_and_headroom_after_run125_ownership(self):
        source = inspect.getsource(runtime_contract.install_runtime_planning_contracts)
        run125_at = source.index("install_run125_cache_prefix_contract()")
        profile_at = source.index("install_planning_capacity_profile()")
        headroom_at = source.index("install_planning_capacity_headroom()")
        self.assertLess(run125_at, profile_at)
        self.assertLess(profile_at, headroom_at)

    def test_moment_preflight_is_no_longer_not_applicable(self):
        source = inspect.getsource(preflight.certify_planning_envelope)
        self.assertIn('if fmt == "moment":', source)
        self.assertIn("_certify_short_envelope", source)
        self.assertIn("audit_media_capacity_margin()", source)
        short_source = inspect.getsource(preflight._certify_short_envelope)
        self.assertIn("P0_SHORT_MIN_PROVIDER_FAMILIES", short_source)
        self.assertIn("worst_case_short_review_capacity", short_source)

    def test_long_preflight_uses_same_groq_headroom_filter_as_runtime(self):
        source = inspect.getsource(preflight.certify_planning_envelope)
        self.assertIn("_require_provider_redundancy", source)
        self.assertIn("groq_operational_headroom", source)

    def test_short_profile_is_format_native_and_bounded(self):
        self.assertLessEqual(profile.SHORT_MAX_RESEARCH_ITEMS, 3)
        self.assertLessEqual(profile.SHORT_MAX_BOUNDARY_ITEMS, 4)
        self.assertLessEqual(profile.SHORT_MAX_AVOID_ITEMS, 6)
        self.assertLessEqual(profile.SHORT_EFFECTIVE_PROMPT_MAX_UTF8_BYTES, 16_000)

    def test_media_request_reserve_is_derived_from_live_topology(self):
        self.assertEqual(
            media_margin.LONGFORM_MEDIA_SEARCH_RESERVE,
            media_margin.MAX_LONGFORM_SECTIONS * 2,
        )
        self.assertEqual(
            media_margin.SHORT_MEDIA_SEARCH_RESERVE,
            1 + ((media_margin.MAX_SHORT_SHOTS - 1) * 2),
        )
        self.assertEqual(
            media_margin.MEDIA_SEARCH_REQUEST_RESERVE,
            max(
                media_margin.LONGFORM_MEDIA_SEARCH_RESERVE,
                media_margin.SHORT_MEDIA_SEARCH_RESERVE,
            ),
        )

    def test_pexels_one_remaining_is_not_called_production_ready(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "provider-preflight.json"
            path.write_text(
                json.dumps(
                    {
                        "checks": [
                            {
                                "provider": "pexels",
                                "status": "pass",
                                "capacity_remaining": 1,
                            },
                            {
                                "provider": "pixabay",
                                "status": "pass",
                                "capacity_remaining": 99,
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "PEXELS_CAPACITY_HEADROOM"):
                media_margin.audit_media_capacity_margin(path)

    def test_pixabay_low_headroom_is_degraded_but_not_false_hard_dependency(self):
        reserve = media_margin.MEDIA_SEARCH_REQUEST_RESERVE
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "provider-preflight.json"
            path.write_text(
                json.dumps(
                    {
                        "checks": [
                            {
                                "provider": "pexels",
                                "status": "pass",
                                "capacity_remaining": reserve + 10,
                            },
                            {
                                "provider": "pixabay",
                                "status": "pass",
                                "capacity_remaining": max(0, reserve - 1),
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            result = media_margin.audit_media_capacity_margin(path)
        by_provider = {item.provider: item for item in result}
        self.assertEqual(by_provider["pexels"].status, "pass")
        self.assertEqual(by_provider["pixabay"].status, "insufficient_headroom")
        self.assertFalse(by_provider["pixabay"].hard_dependency)


if __name__ == "__main__":
    unittest.main()
