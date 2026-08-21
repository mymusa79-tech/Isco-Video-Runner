from __future__ import annotations

from pathlib import Path
from textwrap import dedent


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected one match, found {count}")
    return text.replace(old, new, 1)


def main() -> None:
    thumbnail_path = Path("src/isco_video_agent/thumbnail.py")
    text = thumbnail_path.read_text(encoding="utf-8")

    old = dedent('''\
        def _review_items_for_query(
            *,
            query: str,
            pexels_key: str,
            pixabay_key: str | None,
            thumb_dir: Path,
            concept_index: int,
        ) -> list[dict]:
            """Build one provider-aware board for one query without consuming an AI call."""
            review_items: list[dict] = []
            pexels_photos = search_photos(
                pexels_key,
                query,
                orientation="landscape",
                per_page=12,
            )
            pexels_slots = PEXELS_PRIMARY_BOARD_SLOTS if pixabay_key else MAX_VISUAL_CANDIDATES_PER_HYPOTHESIS
            _append_review_items(
                review_items=review_items,
                provider="pexels",
                photos=pexels_photos,
                file_fn=photo_file,
                download_fn=download,
                thumb_dir=thumb_dir,
                concept_index=concept_index,
                max_total=pexels_slots,
            )

            if pixabay_key and len(review_items) < MAX_VISUAL_CANDIDATES_PER_HYPOTHESIS:
                pixabay_photos = pixabay_provider.search_photos(
                    pixabay_key,
                    query,
                    orientation="landscape",
                    per_page=12,
                )
                _append_review_items(
                    review_items=review_items,
                    provider="pixabay",
                    photos=pixabay_photos,
                    file_fn=pixabay_provider.photo_file,
                    download_fn=pixabay_provider.download,
                    thumb_dir=thumb_dir,
                    concept_index=concept_index,
                    max_total=MAX_VISUAL_CANDIDATES_PER_HYPOTHESIS,
                )
            return review_items
    ''')
    new = dedent('''\
        def _provider_review_sources(
            *,
            provider: str,
            photos: list[dict],
            file_fn,
            max_items: int,
        ) -> list[dict]:
            """Collect usable provider candidates without assigning board positions yet."""
            sources: list[dict] = []
            for photo in photos:
                if len(sources) >= max_items:
                    break
                image_url = file_fn(photo)
                if not image_url:
                    continue
                sources.append({"provider": provider, "photo": photo, "image_url": image_url})
            return sources


        def _interleave_provider_sources(
            primary: list[dict], fallback: list[dict], *, max_total: int
        ) -> list[dict]:
            """Mix providers deterministically while keeping Pexels first when available.

            Candidate-board Vision models can develop position bias. A clustered
            P,P,P,P,B,B board makes provider identity correlate with position. Keep
            Pexels as candidate 1, alternate available Pixabay fill candidates, then
            append any remaining Pexels candidates without increasing board size.
            """
            mixed: list[dict] = []
            primary_index = 0
            fallback_index = 0
            while len(mixed) < max_total and (
                primary_index < len(primary) or fallback_index < len(fallback)
            ):
                if primary_index < len(primary):
                    mixed.append(primary[primary_index])
                    primary_index += 1
                if len(mixed) >= max_total:
                    break
                if fallback_index < len(fallback):
                    mixed.append(fallback[fallback_index])
                    fallback_index += 1
            return mixed


        def _review_items_for_query(
            *,
            query: str,
            pexels_key: str,
            pixabay_key: str | None,
            thumb_dir: Path,
            concept_index: int,
        ) -> list[dict]:
            """Build one provider-aware, position-decorrelated board without an AI call."""
            pexels_photos = search_photos(
                pexels_key,
                query,
                orientation="landscape",
                per_page=12,
            )
            pexels_slots = PEXELS_PRIMARY_BOARD_SLOTS if pixabay_key else MAX_VISUAL_CANDIDATES_PER_HYPOTHESIS
            pexels_sources = _provider_review_sources(
                provider="pexels",
                photos=pexels_photos,
                file_fn=photo_file,
                max_items=pexels_slots,
            )

            pixabay_sources: list[dict] = []
            if pixabay_key:
                pixabay_photos = pixabay_provider.search_photos(
                    pixabay_key,
                    query,
                    orientation="landscape",
                    per_page=12,
                )
                pixabay_sources = _provider_review_sources(
                    provider="pixabay",
                    photos=pixabay_photos,
                    file_fn=pixabay_provider.photo_file,
                    max_items=MAX_VISUAL_CANDIDATES_PER_HYPOTHESIS - len(pexels_sources),
                )

            sources = _interleave_provider_sources(
                pexels_sources,
                pixabay_sources,
                max_total=MAX_VISUAL_CANDIDATES_PER_HYPOTHESIS,
            )
            review_items: list[dict] = []
            for attempt, source in enumerate(sources, 1):
                destination = thumb_dir / f"{concept_index}-review-{attempt}.jpg"
                if source["provider"] == "pexels":
                    raw_path = download(source["image_url"], destination)
                else:
                    raw_path = pixabay_provider.download(source["image_url"], destination)
                preview = make_image_review_preview(
                    raw_path, thumb_dir / f"{concept_index}-preview-{attempt}.jpg"
                )
                review_items.append(
                    {
                        "provider": source["provider"],
                        "photo": source["photo"],
                        "raw": raw_path,
                        "preview": preview,
                    }
                )
            return review_items
    ''')
    text = replace_once(text, old, new, "thumbnail provider board block")
    text = replace_once(
        text,
        '"up_to_4_pexels_then_fill_to_6_with_pixabay" if pixabay_key else "up_to_6_pexels"',
        '"up_to_4_pexels_plus_pixabay_fill_interleaved_pexels_first" if pixabay_key else "up_to_6_pexels"',
        "thumbnail board policy string",
    )
    thumbnail_path.write_text(text, encoding="utf-8")

    test_path = Path("tests/test_thumbnail_pixabay_fallback.py")
    tests = test_path.read_text(encoding="utf-8")
    tests = replace_once(
        tests,
        "selected_index: int = 6",
        "selected_index: int = 4",
        "selected-index mixed-board fixture",
    )
    tests = replace_once(
        tests,
        "package = self._build(pexels_photos=[], pixabay_photos=pixabay_photos)",
        "package = self._build(pexels_photos=[], pixabay_photos=pixabay_photos, selected_index=6)",
        "empty-Pexels fallback fixture",
    )

    rights_marker = "\n\nclass ThumbnailRightsTests(unittest.TestCase):\n"
    order_test = dedent('''\

        class ThumbnailProviderOrderTests(unittest.TestCase):
            def test_mixed_board_interleaves_providers_with_pexels_first(self) -> None:
                pexels_photos = [
                    {"id": index, "url": f"https://www.pexels.com/photo/{index}/"}
                    for index in range(1, 7)
                ]
                pixabay_photos = [
                    {"id": 200 + index, "url": f"https://pixabay.com/photos/{200 + index}/"}
                    for index in range(1, 7)
                ]

                with tempfile.TemporaryDirectory() as tmp, patch.object(
                    thumbnail, "search_photos", return_value=pexels_photos
                ) as pexels_search, patch.object(
                    thumbnail, "photo_file", side_effect=lambda photo: f"https://images.pexels.com/{photo['id']}.jpg"
                ), patch.object(
                    thumbnail, "download", side_effect=lambda _url, path: _write(path)
                ), patch.object(
                    thumbnail.pixabay_provider, "search_photos", return_value=pixabay_photos
                ) as pixabay_search, patch.object(
                    thumbnail.pixabay_provider, "photo_file", side_effect=lambda photo: f"https://cdn.pixabay.com/{photo['id']}.jpg"
                ), patch.object(
                    thumbnail.pixabay_provider, "download", side_effect=lambda _url, path: _write(path)
                ), patch.object(
                    thumbnail, "make_image_review_preview", side_effect=lambda _raw, path: _write(path, b"preview")
                ):
                    items = thumbnail._review_items_for_query(
                        query="sunrise path",
                        pexels_key="p",
                        pixabay_key="x",
                        thumb_dir=Path(tmp),
                        concept_index=1,
                    )

                self.assertEqual(pexels_search.call_count, 1)
                self.assertEqual(pixabay_search.call_count, 1)
                self.assertEqual(
                    [item["provider"] for item in items],
                    ["pexels", "pixabay", "pexels", "pixabay", "pexels", "pexels"],
                )
                self.assertEqual(
                    [item["photo"]["id"] for item in items],
                    [1, 201, 2, 202, 3, 4],
                )
    ''')
    tests = replace_once(
        tests,
        rights_marker,
        order_test + rights_marker,
        "ThumbnailRightsTests insertion marker",
    )
    test_path.write_text(tests, encoding="utf-8")


if __name__ == "__main__":
    main()
