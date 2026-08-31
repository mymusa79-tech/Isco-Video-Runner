from __future__ import annotations

import ast
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import orchestration_media_port as media_port
from scripts import provider_capacity_v2 as capacity


WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "produce-resilient-v4.yml"
CACHE_ACTION_SHA = "1bd1e32a3bdc45362d1e726936510720a7c30a57"


def _call_names(function: ast.FunctionDef) -> list[str]:
    result: list[tuple[int, str]] = []
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
        if name:
            result.append((node.lineno, name))
    return [name for _, name in sorted(result)]


class Pixabay24hCacheTests(unittest.TestCase):
    def _fetch(self, calls: list[str], label: str):
        def run() -> list[dict]:
            calls.append(label)
            return [{"id": label, "pageURL": f"https://pixabay.com/{label}"}]

        return run

    def test_fresh_result_is_reused_without_second_provider_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = capacity.PersistentPixabaySearchCache(Path(tmp) / "cache.json")
            calls: list[str] = []
            with patch.object(capacity.time, "time", side_effect=[1000.0, 1001.0]):
                first = cache.get_or_fetch(
                    provider="pixabay",
                    media_kind="video",
                    query="calm sunrise",
                    orientation="horizontal",
                    per_page=20,
                    fetch=self._fetch(calls, "first"),
                )
                second = cache.get_or_fetch(
                    provider="pixabay",
                    media_kind="video",
                    query="  CALM   sunrise ",
                    orientation="HORIZONTAL",
                    per_page=20,
                    fetch=self._fetch(calls, "second"),
                )
            self.assertEqual(calls, ["first"])
            self.assertEqual(first, second)
            self.assertEqual(cache.hits, 1)
            self.assertEqual(cache.misses, 1)

    def test_entry_at_or_beyond_24h_ttl_is_refetched(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = capacity.PersistentPixabaySearchCache(Path(tmp) / "cache.json")
            calls: list[str] = []
            with patch.object(
                capacity.time,
                "time",
                side_effect=[1000.0, 1000.0 + capacity.PIXABAY_CACHE_TTL_SECONDS],
            ):
                cache.get_or_fetch(
                    provider="pixabay",
                    media_kind="image",
                    query="mountain road",
                    orientation="horizontal",
                    per_page=20,
                    fetch=self._fetch(calls, "old"),
                )
                refreshed = cache.get_or_fetch(
                    provider="pixabay",
                    media_kind="image",
                    query="mountain road",
                    orientation="horizontal",
                    per_page=20,
                    fetch=self._fetch(calls, "fresh"),
                )
            self.assertEqual(calls, ["old", "fresh"])
            self.assertEqual(refreshed[0]["id"], "fresh")
            self.assertEqual(capacity.PIXABAY_CACHE_TTL_SECONDS, 24 * 60 * 60)

    def test_corrupt_cache_is_fail_open_for_search_and_rewritten_safely(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.json"
            path.write_text("{ definitely not json", encoding="utf-8")
            cache = capacity.PersistentPixabaySearchCache(path)
            calls: list[str] = []
            with patch.object(capacity.time, "time", return_value=2000.0):
                result = cache.get_or_fetch(
                    provider="pixabay",
                    media_kind="video",
                    query="quiet city",
                    orientation="horizontal",
                    per_page=20,
                    fetch=self._fetch(calls, "recovered"),
                )
            self.assertEqual(result[0]["id"], "recovered")
            self.assertEqual(calls, ["recovered"])
            document = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(document["schema_version"], capacity.CACHE_SCHEMA_VERSION)
            self.assertEqual(len(document["entries"]), 1)

    def test_persistence_sanitizer_deletes_corrupt_or_fully_expired_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            corrupt = Path(tmp) / "corrupt.json"
            corrupt.write_text("not-json", encoding="utf-8")
            with patch.object(capacity.time, "time", return_value=5000.0):
                self.assertFalse(capacity.prepare_cache_for_persistence(corrupt))
            self.assertFalse(corrupt.exists())

            expired = Path(tmp) / "expired.json"
            expired.write_text(
                json.dumps(
                    {
                        "schema_version": capacity.CACHE_SCHEMA_VERSION,
                        "entries": {
                            "x": {
                                "fetched_at": 5000.0 - capacity.PIXABAY_CACHE_TTL_SECONDS,
                                "response": [{"id": "old"}],
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(capacity.time, "time", return_value=5000.0):
                self.assertFalse(capacity.prepare_cache_for_persistence(expired))
            self.assertFalse(expired.exists())

    def test_persistence_sanitizer_keeps_only_fresh_valid_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mixed.json"
            now = 8000.0
            path.write_text(
                json.dumps(
                    {
                        "schema_version": capacity.CACHE_SCHEMA_VERSION,
                        "entries": {
                            "fresh": {"fetched_at": now - 60, "response": [{"id": "fresh"}]},
                            "expired": {
                                "fetched_at": now - capacity.PIXABAY_CACHE_TTL_SECONDS,
                                "response": [{"id": "expired"}],
                            },
                            "bad": {"fetched_at": now - 60, "response": "not-a-list"},
                        },
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(capacity.time, "time", return_value=now):
                self.assertTrue(capacity.prepare_cache_for_persistence(path))
            document = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(set(document["entries"]), {"fresh"})

    def test_media_port_installs_capacity_before_media_trust(self):
        source = Path(media_port.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        install = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "install_media_runtime_port"
        )
        calls = _call_names(install)
        self.assertIn("install_provider_capacity_v2", calls)
        self.assertIn("install_media_trust_boundary_v2", calls)
        self.assertLess(calls.index("install_provider_capacity_v2"), calls.index("install_media_trust_boundary_v2"))

    def test_production_workflow_restores_and_saves_only_the_pixabay_cache(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        restore_name = "Restore Pixabay 24h search cache"
        produce_marker = "id: produce_video"
        prepare_name = "Prepare Pixabay cache for cross-run save"
        save_name = "Save Pixabay 24h search cache"
        self.assertIn(restore_name, text)
        self.assertIn(produce_marker, text)
        self.assertIn(prepare_name, text)
        self.assertIn(save_name, text)
        self.assertLess(text.index(restore_name), text.index(produce_marker))
        self.assertLess(text.index(produce_marker), text.index(prepare_name))
        self.assertLess(text.index(prepare_name), text.index(save_name))
        self.assertIn(f"actions/cache/restore@{CACHE_ACTION_SHA}", text)
        self.assertIn(f"actions/cache/save@{CACHE_ACTION_SHA}", text)
        self.assertIn("${{ runner.temp }}/isco-pixabay-api-cache", text)
        self.assertIn("pixabay-search-v2-${{ runner.os }}-${{ github.run_id }}", text)
        prepare_section = text[text.index(prepare_name):text.index(save_name)]
        self.assertIn(
            "from scripts.provider_capacity_v2 import prepare_cache_for_persistence",
            prepare_section,
        )
        self.assertIn("allowed = prepare_cache_for_persistence(path)", prepare_section)
        self.assertEqual(capacity.PIXABAY_CACHE_TTL_SECONDS, 24 * 60 * 60)
        restore_section = text[text.index(restore_name):text.index(produce_marker)]
        save_section = text[text.index(save_name):]
        self.assertNotIn("secrets.", restore_section)
        self.assertNotIn("PIXABAY_API_KEY", restore_section)
        self.assertNotIn("PIXABAY_API_KEY", save_section[:1000])


if __name__ == "__main__":
    unittest.main()
