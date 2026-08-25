from __future__ import annotations

import re
import unittest
from pathlib import Path


WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "produce-resilient-v4.yml"
CACHE_ACTION_SHA = "55cc8345863c7cc4c66a329aec7e433d2d1c52a9"
CACHE_PATH = "${{ runner.temp }}/isco-pixabay-api-cache"


class ProviderCapacityWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = WORKFLOW.read_text(encoding="utf-8")

    def test_restore_and_save_cache_actions_are_full_sha_pinned(self) -> None:
        self.assertIn(f"uses: actions/cache/restore@{CACHE_ACTION_SHA}", self.text)
        self.assertIn(f"uses: actions/cache/save@{CACHE_ACTION_SHA}", self.text)
        self.assertNotIn("uses: actions/cache@v", self.text)

    def test_cache_path_is_public_metadata_only_and_bound_into_production(self) -> None:
        self.assertIn(f"path: {CACHE_PATH}", self.text)
        self.assertIn(
            "ISCO_PIXABAY_CACHE_PATH: ${{ runner.temp }}/isco-pixabay-api-cache/search-cache-v2.json",
            self.text,
        )
        cache_blocks = re.findall(
            r"- name: (?:Restore|Persist) 24h Pixabay API search cache.*?(?=\n\s{6}- name:|\Z)",
            self.text,
            flags=re.S,
        )
        self.assertEqual(len(cache_blocks), 2)
        joined = "\n".join(cache_blocks).casefold()
        for forbidden in ("isco-secrets", "api_key", "token", "password", "credential"):
            self.assertNotIn(forbidden, joined)

    def test_cache_is_saved_even_when_production_fails(self) -> None:
        match = re.search(
            r"- name: Persist 24h Pixabay API search cache\n(?P<body>.*?)(?=\n\s{6}- name:)",
            self.text,
            flags=re.S,
        )
        self.assertIsNotNone(match)
        body = match.group("body")
        self.assertIn("if: always()", body)
        self.assertIn("pixabay-api-v2-${{ runner.os }}-${{ github.run_id }}-${{ github.run_attempt }}", body)

    def test_restore_uses_prefix_but_new_run_gets_unique_save_key(self) -> None:
        self.assertIn("restore-keys: |\n            pixabay-api-v2-${{ runner.os }}-", self.text)
        key = "pixabay-api-v2-${{ runner.os }}-${{ github.run_id }}-${{ github.run_attempt }}"
        self.assertGreaterEqual(self.text.count(key), 2)


if __name__ == "__main__":
    unittest.main()
