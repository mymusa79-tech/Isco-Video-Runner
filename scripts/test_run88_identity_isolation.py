from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import isco_video_agent.resilient_planner as staged
import scripts.append_retry_guard as append_retry_guard
import scripts.brand_anchor_guard as brand_anchor_guard


class Run88IdentityIsolationTests(unittest.TestCase):
    def test_writer_policy_redacts_literal_brand_anchors(self) -> None:
        policy = {
            "brand_signature": {
                "opener": "الافتتاح الحرفي",
                "closer": "الخاتمة الحرفية",
                "other": "keep-me",
            },
            "tone": {"mode": "calm"},
        }
        with patch.object(staged, "_writer_policy_json", None, create=True):
            redacted = json.loads(
                brand_anchor_guard._redact_writer_policy_json(
                    json.dumps(policy, ensure_ascii=False)
                )
            )
        serialized = json.dumps(redacted, ensure_ascii=False)
        self.assertNotIn("الافتتاح الحرفي", serialized)
        self.assertNotIn("الخاتمة الحرفية", serialized)
        self.assertEqual(redacted["brand_signature"]["other"], "keep-me")
        self.assertIs(redacted["brand_signature"]["host_managed"], True)
        self.assertIn("exactly one identity closer", redacted["brand_signature"]["runtime_rule"])

    def test_terminal_closer_is_masked_for_prompt_without_mutating_plan_or_word_count(self) -> None:
        closer = "خذ الفكرة معك الآن"
        sections = [
            staged.ScriptSection(
                id="s1",
                narration="فكرة أولى مستقلة",
                visual_query="room",
                key_point="k1",
            ),
            staged.ScriptSection(
                id="s2",
                narration=f"تفصيل ختامي مفيد. {closer}",
                visual_query="window",
                key_point="k2",
            ),
        ]
        original = sections[-1].narration
        original_words = staged._word_count(original)
        masked = brand_anchor_guard._mask_terminal_closer_for_writer(sections, closer)

        self.assertEqual(sections[-1].narration, original)
        self.assertIsNot(masked[-1], sections[-1])
        self.assertNotIn(closer, masked[-1].narration)
        self.assertEqual(staged._word_count(masked[-1].narration), original_words)

    def test_installed_guard_sanitizes_runner_residual_repair_context(self) -> None:
        closer = "خذ الفكرة معك الآن"
        sections = [
            staged.ScriptSection(
                id="s1",
                narration=" ".join(["فكرة"] * 120),
                visual_query="room",
                key_point="k1",
            ),
            staged.ScriptSection(
                id="s2",
                narration=" ".join(["تفصيل"] * 90) + f" {closer}",
                visual_query="window",
                key_point="k2",
            ),
        ]
        original_closing = sections[-1].narration
        canonical_closer = "ضع مقاييسك الشخصية بوعي كامل"
        policy_json = json.dumps(
            {
                "brand_signature": {
                    "opener": "افتتاح معروف",
                    "closer": canonical_closer,
                }
            },
            ensure_ascii=False,
        )
        captured: dict = {}

        def fake_original(
            api_key,
            *,
            topic,
            model,
            sections,
            policy_json,
            research_json,
            narrative_format,
            current_words,
            minimum,
            editorial_intent_json="",
        ):
            del api_key, topic, model, research_json, narrative_format, current_words, minimum, editorial_intent_json
            captured["sections"] = sections
            captured["policy_json"] = policy_json
            return {"s2": "إضافة جديدة آمنة"}

        saved_runner = append_retry_guard._repair_all_residual_underlength
        saved_staged = staged._script_doctor_underlength_retry
        try:
            append_retry_guard._repair_all_residual_underlength = fake_original
            staged._script_doctor_underlength_retry = fake_original
            append_retry_guard._ACTIVE_CLOSER.set(closer)
            brand_anchor_guard._install_append_retry_identity_isolation()

            result = append_retry_guard._repair_all_residual_underlength(
                "key",
                topic="topic",
                model="model",
                sections=sections,
                policy_json=policy_json,
                research_json="{}",
                narrative_format="direct_cinematic",
                current_words=210,
                minimum=800,
            )

            self.assertEqual(result, {"s2": "إضافة جديدة آمنة"})
            self.assertEqual(sections[-1].narration, original_closing)
            self.assertNotIn(closer, captured["sections"][-1].narration)
            self.assertEqual(
                staged._word_count(captured["sections"][-1].narration),
                staged._word_count(original_closing),
            )
            self.assertNotIn(canonical_closer, captured["policy_json"])
            self.assertIn("host_managed", captured["policy_json"])
            self.assertIs(staged._script_doctor_underlength_retry, append_retry_guard._repair_all_residual_underlength)
        finally:
            append_retry_guard._repair_all_residual_underlength = saved_runner
            staged._script_doctor_underlength_retry = saved_staged
            append_retry_guard._ACTIVE_CLOSER.set(None)


if __name__ == "__main__":
    unittest.main()
