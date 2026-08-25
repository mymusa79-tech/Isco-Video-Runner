from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import provider_capacity_v2 as capacity


class ProviderCapacityV2Tests(unittest.TestCase):
    def test_cache_hit_within_24h_avoids_provider_call_and_returns_copy(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "cache.json"
            cache = capacity.PersistentPixabaySearchCache(path)
            calls = []

            def fetch():
                calls.append(1)
                return [{"id": 10, "nested": {"value": "original"}}]

            with patch.object(capacity.time, "time", return_value=1000.0):
                first = cache.get_or_fetch(
                    provider="pixabay", media_kind="video", query="calm road",
                    orientation="landscape", per_page=12, fetch=fetch,
                )
            first[0]["nested"]["value"] = "mutated-by-caller"
            with patch.object(capacity.time, "time", return_value=1000.0 + 86399):
                second = cache.get_or_fetch(
                    provider="pixabay", media_kind="video", query="calm road",
                    orientation="landscape", per_page=12, fetch=fetch,
                )
            self.assertEqual(len(calls), 1)
            self.assertEqual(second[0]["nested"]["value"], "original")
            self.assertEqual(cache.hits, 1)
            self.assertEqual(cache.misses, 1)

    def test_entry_at_or_beyond_24h_is_refetched(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cache = capacity.PersistentPixabaySearchCache(Path(td) / "cache.json")
            values = iter([[{"id": 1}], [{"id": 2}]])
            with patch.object(capacity.time, "time", return_value=1000.0):
                self.assertEqual(cache.get_or_fetch(
                    provider="pixabay", media_kind="photo", query="coffee",
                    orientation="horizontal", per_page=12, fetch=lambda: next(values),
                )[0]["id"], 1)
            with patch.object(capacity.time, "time", return_value=1000.0 + capacity.PIXABAY_CACHE_TTL_SECONDS):
                self.assertEqual(cache.get_or_fetch(
                    provider="pixabay", media_kind="photo", query="coffee",
                    orientation="horizontal", per_page=12, fetch=lambda: next(values),
                )[0]["id"], 2)
            self.assertEqual(cache.misses, 2)

    def test_provider_exception_is_never_cached(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "cache.json"
            cache = capacity.PersistentPixabaySearchCache(path)
            with self.assertRaisesRegex(RuntimeError, "synthetic provider failure"):
                cache.get_or_fetch(
                    provider="pixabay", media_kind="video", query="road",
                    orientation="landscape", per_page=12,
                    fetch=lambda: (_ for _ in ()).throw(RuntimeError("synthetic provider failure")),
                )
            self.assertFalse(path.exists())

    def test_corrupt_or_future_cache_is_not_trusted(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "cache.json"
            path.write_text("not-json", encoding="utf-8")
            cache = capacity.PersistentPixabaySearchCache(path)
            with patch.object(capacity.time, "time", return_value=2000.0):
                result = cache.get_or_fetch(
                    provider="pixabay", media_kind="video", query="road",
                    orientation="landscape", per_page=12, fetch=lambda: [{"id": 3}],
                )
            self.assertEqual(result, [{"id": 3}])

            document = json.loads(path.read_text(encoding="utf-8"))
            key = next(iter(document["entries"]))
            document["entries"][key]["fetched_at"] = 999999.0
            path.write_text(json.dumps(document), encoding="utf-8")
            with patch.object(capacity.time, "time", return_value=2001.0):
                result = cache.get_or_fetch(
                    provider="pixabay", media_kind="video", query="road",
                    orientation="landscape", per_page=12, fetch=lambda: [{"id": 4}],
                )
            self.assertEqual(result, [{"id": 4}])

    def test_cache_key_separates_media_orientation_and_page_size(self) -> None:
        keys = {
            capacity._canonical_key(media_kind="video", query="same", orientation="landscape", per_page=12),
            capacity._canonical_key(media_kind="photo", query="same", orientation="landscape", per_page=12),
            capacity._canonical_key(media_kind="video", query="same", orientation="portrait", per_page=12),
            capacity._canonical_key(media_kind="video", query="same", orientation="landscape", per_page=20),
        }
        self.assertEqual(len(keys), 4)

    def test_non_pixabay_provider_delegates_to_existing_process_cache(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cache = capacity.PersistentPixabaySearchCache(Path(td) / "cache.json")
            with patch.object(capacity.process_cache, "get_or_fetch", return_value=[{"id": 8}]) as delegated:
                result = cache.get_or_fetch(
                    provider="pexels", media_kind="video", query="road",
                    orientation="landscape", per_page=12, fetch=lambda: [],
                )
            self.assertEqual(result, [{"id": 8}])
            delegated.assert_called_once()

    def test_atomic_cache_write_leaves_no_tmp(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "cache.json"
            cache = capacity.PersistentPixabaySearchCache(path)
            with patch.object(capacity.time, "time", return_value=1000.0):
                cache.get_or_fetch(
                    provider="pixabay", media_kind="video", query="road",
                    orientation="landscape", per_page=12, fetch=lambda: [{"id": 1}],
                )
            self.assertTrue(path.is_file())
            self.assertFalse(path.with_name(path.name + ".tmp").exists())


if __name__ == "__main__":
    unittest.main()
