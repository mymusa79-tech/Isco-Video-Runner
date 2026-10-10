from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from clean_v2.media import StockVisualSource, _reuse_existing_visual_competitor


def _candidate(provider: str, asset_id: str, score: float = 0.5) -> dict:
    return {
        "provider": provider,
        "asset_id": asset_id,
        "download_url": f"https://example.org/{provider}/{asset_id}.mp4",
        "local_rank_score": score,
        "metadata_semantic_score": score,
        "retrieval_description": "cluttered work desk crumpled paper laptop",
    }


class Run116UnifiedVisualShortlistTests(unittest.TestCase):
    def test_single_provider_recovery_reuses_previous_pexels_result(self) -> None:
        # Run116 recovered with three Pixabay videos despite already finding a
        # Pexels contender for the opening from the same original provider search.
        source = StockVisualSource()
        source._initial_visual_competitors[("s1", "b1")] = [_candidate("pexels", "initial-p")]
        pixabay = [_candidate("pixabay", str(i), 0.9 - i * 0.1) for i in range(3)]
        downloaded: list[str] = []

        def fake_download(url: str, destination: Path) -> None:
            downloaded.append(url)
            Path(destination).write_bytes(b"fixture-video-data")

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(source, "_pexels_recovery_pool", return_value=[]) as pexels,
            patch.object(source, "_pixabay_recovery_pool", return_value=pixabay) as pixabay_search,
            patch.object(source, "_coverr_recovery_pool", return_value=[]) as coverr,
            patch("clean_v2.media._download_media", side_effect=fake_download),
        ):
            result = source.acquire_replacement_candidates(
                "cluttered desk crumpled papers laptop",
                Path(tmp), "short",
                destination_name="visual-01.mp4",
                section_id="s1",
                beat_id="b1",
                max_candidates=3,
            )
            self.assertEqual(
                [r["provider"] for _clip, r in result],
                ["pixabay", "pexels", "pixabay"],
            )
            self.assertEqual(len(downloaded), 3)
            pexels.assert_called_once()
            pixabay_search.assert_called_once()
            coverr.assert_called_once()
            self.assertTrue(all(Path(clip).is_file() for clip, _row in result))

    def test_previously_used_or_unavailable_competitor_not_reused(self) -> None:
        ranked = [_candidate("pixabay", str(i), 0.9 - i * 0.1) for i in range(3)]
        prior = [_candidate("pexels", "old")]
        self.assertEqual(
            _reuse_existing_visual_competitor(ranked, prior, used_assets={("pexels", "old")}, limit=3),
            ranked,
        )
        self.assertEqual(
            _reuse_existing_visual_competitor(ranked, prior, used_assets=set(), limit=1),
            ranked,
        )

    def test_existing_provider_diversity_preserved(self) -> None:
        ranked = [_candidate("pixabay", "p"), _candidate("pexels", "a"), _candidate("pixabay", "b")]
        prior = [_candidate("coverr", "c")]
        self.assertEqual(
            _reuse_existing_visual_competitor(ranked, prior, used_assets=set(), limit=3),
            ranked,
        )

    def test_original_candidate_budget_never_expanded(self) -> None:
        ranked = [_candidate("pixabay", str(i)) for i in range(7)]
        prior = [_candidate("pexels", "a")]
        mixed = _reuse_existing_visual_competitor(ranked, prior, used_assets=set(), limit=3)
        self.assertEqual(len(mixed), 8)  # pool metadata only, not QA reviews
        self.assertEqual(len(mixed[:3]), 3)
        self.assertEqual(mixed[1]["provider"], "pexels")


if __name__ == "__main__":
    unittest.main()
