from __future__ import annotations

import json
import unittest

from clean_v2.channel_persona import load_channel_persona, with_channel_persona
from clean_v2.pipeline import _planning_prompt, _script_prompt
from clean_v2.providers import (
    ProviderAdapter,
    ProviderRouter,
    _mistral_planning_response_schema,
    _mistral_script_response_schema,
)


def _brief(fmt: str = "film") -> dict:
    return {
        "approved_topic": "لماذا تفشل خطط إدارة الوقت في الحياة اليومية",
        "format": fmt,
        "research_pack": [],
    }


def _plan(count: int = 5) -> dict:
    return {
        "title": "عنوان",
        "promise": "وعد واضح",
        "sections": [
            {
                "id": f"s{i}",
                "heading": f"قسم {i}",
                "purpose": f"غرض {i}",
                "visual_query_en": f"daily routine object {i}",
            }
            for i in range(1, count + 1)
        ],
    }


class CleanV2ChannelPersonaTests(unittest.TestCase):
    def test_clean_v2_uses_full_legacy_persona_payload(self):
        persona = load_channel_persona()
        self.assertEqual(persona["channel"], "نداء اليقظة")
        self.assertIn("في عالمنا المتسارع", persona["writing_voice"]["banned_ai_phrases"])
        self.assertIn("كشف الافتراض الخفي", persona["writing_voice"]["signature_moves"])
        self.assertIn("generic_rejection_rule", persona["analysis_lens"])

    def test_planning_and_script_prompts_are_enriched_and_idempotent(self):
        planning = _planning_prompt(_brief())
        script = _script_prompt(_brief(), _plan())
        for prompt in (planning, script):
            self.assertEqual(prompt.count("<CHANNEL_PERSONA>"), 1)
            self.assertEqual(prompt.count("</CHANNEL_PERSONA>"), 1)
            self.assertIn("في عالمنا المتسارع", prompt)
            self.assertIn("كشف الافتراض الخفي", prompt)
            self.assertIn("generic_rejection_rule", prompt)
            self.assertEqual(with_channel_persona(prompt), prompt)

    def test_same_enriched_prompt_reaches_every_content_provider(self):
        for stage, prompt in (
            ("planning", _planning_prompt(_brief())),
            ("script", _script_prompt(_brief(), _plan())),
        ):
            for provider_name in ("gemini", "groq", "openrouter", "mistral"):
                seen = []

                def call(value: str, max_tokens: int) -> dict:
                    seen.append(value)
                    return {"ok": True}

                router = ProviderRouter(
                    [ProviderAdapter(provider_name, call, stages=frozenset({stage}))]
                )
                result = router.route(
                    stage=stage,
                    prompt=prompt,
                    max_tokens=100,
                    validator=lambda value: value,
                )
                self.assertEqual(result, {"ok": True})
                self.assertEqual(seen, [prompt])
                self.assertIn("<CHANNEL_PERSONA>", seen[0])

    def test_persona_suffix_does_not_break_mistral_contract_parsing(self):
        planning_schema = _mistral_planning_response_schema(_planning_prompt(_brief()))
        self.assertEqual(planning_schema["properties"]["sections"]["minItems"], 5)
        self.assertEqual(planning_schema["properties"]["sections"]["maxItems"], 5)

        script_schema = _mistral_script_response_schema(_script_prompt(_brief(), _plan()))
        sections = script_schema["properties"]["sections"]
        self.assertEqual(sections["minItems"], 5)
        self.assertEqual(sections["maxItems"], 5)
        self.assertEqual(
            [item["properties"]["id"]["const"] for item in sections["prefixItems"]],
            ["s1", "s2", "s3", "s4", "s5"],
        )


if __name__ == "__main__":
    unittest.main()
