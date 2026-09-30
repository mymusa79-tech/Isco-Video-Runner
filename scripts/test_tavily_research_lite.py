from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from scripts.tavily_research_lite import (
    MAX_RESULTS,
    TAVILY_SEARCH_URL,
    collect_tavily_grounding,
)


ROOT = Path(__file__).resolve().parents[1]


class TavilyResearchLiteTests(unittest.TestCase):
    def test_missing_key_is_fail_open(self) -> None:
        result = collect_tavily_grounding("", "long")
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["reason"], "missing_api_key")
        self.assertEqual(result["memo"], "")

    def test_basic_search_is_one_compact_call(self) -> None:
        calls = []

        def fake_post(url: str, *, headers: dict[str, str], body: bytes, timeout: int):
            calls.append((url, headers, json.loads(body.decode("utf-8")), timeout))
            return {
                "results": [
                    {
                        "title": "Arabic productivity discussion",
                        "url": "https://example.com/a",
                        "content": "A useful compact snippet about everyday burnout and routines.",
                    },
                    {
                        "title": "Duplicate",
                        "url": "https://example.com/a",
                        "content": "duplicate",
                    },
                    {
                        "title": "Habits",
                        "url": "https://example.com/b",
                        "content": "A second source about habits.",
                    },
                ]
            }

        result = collect_tavily_grounding(
            "tvly-secret-value",
            "short",
            post=fake_post,
            now=datetime(2026, 9, 30, tzinfo=timezone.utc),
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["result_count"], 2)
        self.assertEqual(result["credits_expected"], 1)
        self.assertEqual(len(calls), 1)
        url, headers, payload, timeout = calls[0]
        self.assertEqual(url, TAVILY_SEARCH_URL)
        self.assertEqual(headers["Authorization"], "Bearer tvly-secret-value")
        self.assertEqual(payload["search_depth"], "basic")
        self.assertEqual(payload["max_results"], MAX_RESULTS)
        self.assertFalse(payload["include_answer"])
        self.assertFalse(payload["include_raw_content"])
        self.assertIn("September 2026", payload["query"])
        self.assertNotIn("tvly-secret-value", result["memo"])
        self.assertIn("https://example.com/a", result["memo"])
        self.assertGreater(timeout, 0)

    def test_provider_error_is_fail_open_and_does_not_echo_secret(self) -> None:
        def fail(*_args, **_kwargs):
            raise RuntimeError("provider unavailable")

        result = collect_tavily_grounding("tvly-private", "long", post=fail)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["reason"], "RuntimeError")
        self.assertEqual(result["memo"], "")
        self.assertNotIn("tvly-private", json.dumps(result))

    def test_core_caches_one_tavily_result_per_pending_action(self) -> None:
        source = (ROOT / "scripts" / "telegram_topic_research_v2_core.py").read_text(encoding="utf-8")
        self.assertIn('tavily = pending.get("tavily_grounding")', source)
        self.assertIn("if not isinstance(tavily, dict):", source)
        self.assertIn('pending["tavily_grounding"] = tavily', source)
        self.assertIn('signals["grounded_research"] = tavily_memo', source)

    def test_secret_is_scoped_to_editorial_research_workflow(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "telegram-editorial-control.yml").read_text(encoding="utf-8")
        self.assertIn("TAVILY_API_KEY: ${{ secrets.TAVILY_API_KEY }}", workflow)
        self.assertEqual(workflow.count("TAVILY_API_KEY:"), 1)


if __name__ == "__main__":
    unittest.main()
