from __future__ import annotations

import unittest

from clean_v2.human_feel import load_human_feel, with_human_feel
from clean_v2.pipeline import _planning_prompt, _script_prompt
from clean_v2.providers import (
    ProviderAdapter,
    ProviderRouter,
    _mistral_planning_response_schema,
    _mistral_script_response_schema,
)


EXPECTED_PREFER = [
    "specific observations",
    "natural sentence-length variation",
    "measured uncertainty when appropriate",
    "concrete examples",
    "small moments of recognition",
    "earned emotional movement",
]
EXPECTED_REJECT = [
    "generic AI filler",
    "motivational poster language",
    "constant rhetorical questions",
    "perfectly symmetrical paragraphs",
    "repetitive three-part slogans",
    "fake personal stories",
    "unearned certainty",
]


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


class CleanV2HumanFeelTests(unittest.TestCase):
    def test_exact_legacy_human_feel_rules_are_loaded(self):
        rules = load_human_feel()
        self.assertEqual(rules["prefer"], EXPECTED_PREFER)
        self.assertEqual(rules["reject"], EXPECTED_REJECT)

    def test_planning_and_script_receive_human_feel_once(self):
        for prompt in (_planning_prompt(_brief()), _script_prompt(_brief(), _plan())):
            self.assertEqual(prompt.count("<HUMAN_FEEL>"), 1)
            self.assertEqual(prompt.count("</HUMAN_FEEL>"), 1)
            for value in EXPECTED_PREFER + EXPECTED_REJECT:
                self.assertIn(value, prompt)
            self.assertEqual(with_human_feel(prompt), prompt)

    def test_same_human_feel_prompt_reaches_every_content_provider(self):
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
                self.assertIn("<HUMAN_FEEL>", seen[0])

    def test_human_feel_suffix_preserves_mistral_contract_parsing(self):
        planning_schema = _mistral_planning_response_schema(_planning_prompt(_brief()))
        self.assertEqual(planning_schema["properties"]["sections"]["minItems"], 5)
        self.assertEqual(planning_schema["properties"]["sections"]["maxItems"], 5)

        script_schema = _mistral_script_response_schema(_script_prompt(_brief(), _plan()))
        sections = script_schema["properties"]["sections"]
        self.assertEqual(
            [item["properties"]["id"]["const"] for item in sections["prefixItems"]],
            ["s1", "s2", "s3", "s4", "s5"],
        )


if __name__ == "__main__":
    unittest.main()
