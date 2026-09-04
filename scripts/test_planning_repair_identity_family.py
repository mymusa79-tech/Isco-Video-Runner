from __future__ import annotations

import unittest
from pathlib import Path

from scripts import native_short_stage_contract as short_stage
from scripts import planning_repair_identity_family as family
from scripts import planning_stage_contract as stage_contract


ROOT = Path(__file__).resolve().parents[1]


def valid_patch() -> dict:
    return {
        "pillar": "understand",
        "hook": "لماذا يبدو كل نقد وكأنه حكم نهائي؟",
        "title_options": ["عنوان 1", "عنوان 2", "عنوان 3"],
        "thumbnail_concepts": ["concept 1", "concept 2", "concept 3"],
        "sections": [
            {
                "id": "s1",
                "narration": "",
                "visual_query": "person rereading a message at a quiet desk",
                "on_screen_text": "ليس كل نقد حكمًا عليك؛ أحيانًا هو معلومة عن موقف واحد.",
                "emotion": "reflective",
                "expected_seconds": 15,
                "key_point": "افصل الملاحظة المحددة عن الحكم على الذات.",
            }
        ],
        "cta": "احفظها عندما تحتاج هذا التذكير.",
        "closing_payoff": "الملاحظة تصف لحظة، لا هويتك كلها.",
    }


class RepairIdentityFamilyTests(unittest.TestCase):
    def test_short_repair_is_strict_patch_and_keeps_transport_policy(self) -> None:
        base = short_stage.moment_stage_spec("short_repair", "موضوع معتمد")
        patched = family._repair_patch_spec(base)
        properties = set(patched.output_schema["properties"])
        self.assertNotIn("topic", properties)
        self.assertNotIn("format", properties)
        self.assertNotIn("topic", patched.output_schema["required"])
        self.assertNotIn("format", patched.output_schema["required"])
        self.assertEqual(patched.contract_id, family.SHORT_REPAIR_CONTRACT_ID)
        self.assertEqual(patched.provider_policy, base.provider_policy)
        self.assertEqual(patched.cache_policy, base.cache_policy)
        self.assertEqual(patched.semantic_rules["approved_topic"], "موضوع معتمد")
        self.assertEqual(patched.semantic_rules["format"], "moment")

    def test_draft_and_review_contracts_remain_full_identity_contracts(self) -> None:
        for stage_kind in ("short_draft", "short_review"):
            spec = short_stage.moment_stage_spec(stage_kind, "موضوع معتمد")
            self.assertIn("topic", spec.output_schema["properties"])
            self.assertIn("format", spec.output_schema["properties"])
            self.assertEqual(spec.contract_id, f"planning.{stage_kind}.v1")

    def test_valid_patch_is_semantically_checked_with_host_owned_identity(self) -> None:
        spec = family._repair_patch_spec(short_stage.moment_stage_spec("short_repair", "موضوع معتمد"))
        contract = stage_contract.bind_request_contract(spec, "repair prompt")
        payload = valid_patch()
        self.assertIs(family._validate_short_repair_patch(contract, payload), payload)
        self.assertNotIn("topic", payload)
        self.assertNotIn("format", payload)

    def test_provider_cannot_echo_or_change_immutable_identity(self) -> None:
        spec = family._repair_patch_spec(short_stage.moment_stage_spec("short_repair", "موضوع معتمد"))
        contract = stage_contract.bind_request_contract(spec, "repair prompt")
        for field, value in (("topic", "موضوع مختلف"), ("format", "film")):
            payload = valid_patch()
            payload[field] = value
            with self.assertRaises(stage_contract.PlanningStageError) as ctx:
                family._validate_short_repair_patch(contract, payload)
            self.assertEqual(ctx.exception.code, stage_contract.PlanningErrorCode.STRUCTURAL_INVALID)
            self.assertIn("unexpected=", str(ctx.exception))

    def test_prompt_exposes_patch_ownership_and_current_duration_contract(self) -> None:
        source = "\n".join(
            [
                "Return one complete corrected plan using EXACTLY the same JSON schema as CURRENT_MOMENT.",
                "- format stays moment and sections stays EXACTLY one section.",
                "- Keep duration 7-20 seconds and max two short on-screen lines.",
                "- Preserve the approved topic and the useful promise unless a blocking issue requires a wording correction.",
                "Return JSON only with keys: topic,pillar,format,hook,title_options,thumbnail_concepts,sections,cta,closing_payoff.",
            ]
        )
        rewritten = family._rewrite_short_repair_prompt(source)
        self.assertIn("host-owned immutable identity", rewritten)
        self.assertIn("Keep duration 12-20 seconds", rewritten)
        self.assertIn(
            "mutable keys: pillar,hook,title_options,thumbnail_concepts,sections,cta,closing_payoff",
            rewritten,
        )
        self.assertIn("Do not return `topic` or `format`", rewritten)
        self.assertNotIn("same JSON schema as CURRENT_MOMENT", rewritten)

    def test_long_dossier_repair_already_uses_section_patch_only(self) -> None:
        spec = stage_contract.script_stage_spec("dossier_repair", ["s1", "s2"])
        self.assertEqual(set(spec.output_schema["properties"]), {"sections"})
        section_properties = set(spec.output_schema["properties"]["sections"]["items"]["properties"])
        self.assertEqual(section_properties, {"id", "narration", "key_point"})
        self.assertTrue(set(family.IMMUTABLE_IDENTITY_FIELDS).isdisjoint(section_properties))

    def test_canonical_runtime_seam_installs_the_family(self) -> None:
        text = (ROOT / "scripts" / "short_repair_reset_recovery.py").read_text(encoding="utf-8")
        self.assertIn("install_planning_repair_identity_family", text)
        self.assertIn("install_planning_repair_identity_family()", text)


if __name__ == "__main__":
    unittest.main()
