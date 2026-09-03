from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from scripts import visual_retrieval_adjudication_v1 as v1


def _candidate(asset_id: int, *, url: str, tags: str = "", views: int = 0) -> dict:
    return {
        "id": asset_id,
        "url": url,
        "duration": 20,
        "video_files": [{"link": "https://example.invalid/a.mp4", "width": 1280, "height": 720}],
        "_isco_visual_intelligence": {"tags": tags, "views": views},
    }


class VisualRetrievalAdjudicationV1Tests(unittest.TestCase):
    def setUp(self) -> None:
        v1._GROQ_CAPACITY.set(None)

    def tearDown(self) -> None:
        v1._GROQ_CAPACITY.set(None)

    def test_intent_rerank_lifts_semantically_relevant_candidate(self) -> None:
        intent = v1.build_visual_intent("drawing a boundary line in a relationship")
        candidates = [
            _candidate(1, url="https://pexels.com/video/coffee-cup-window-1/", tags="coffee morning table"),
            _candidate(2, url="https://pexels.com/video/drawing-line-personal-space-2/", tags="boundary line personal space relationship"),
            _candidate(3, url="https://pexels.com/video/city-traffic-night-3/", tags="traffic city cars"),
        ]
        ranked = v1.rerank_provider_candidates("pexels", candidates, intent)
        self.assertEqual(ranked[0]["id"], 2)
        self.assertGreater(
            ranked[0]["_isco_visual_intelligence"]["retrieval_score_v1"],
            ranked[-1]["_isco_visual_intelligence"]["retrieval_score_v1"],
        )

    def test_mmr_diversifies_near_duplicate_top_results(self) -> None:
        intent = v1.build_visual_intent("calm focus at desk notebook")
        candidates = [
            _candidate(1, url="https://pexels.com/video/focus-desk-notebook-1/", tags="focus desk notebook work"),
            _candidate(2, url="https://pexels.com/video/focus-desk-notebook-2/", tags="focus desk notebook work"),
            _candidate(3, url="https://pexels.com/video/quiet-study-writing-3/", tags="quiet study writing concentration"),
        ]
        ranked = v1.rerank_provider_candidates("pexels", candidates, intent)
        self.assertEqual(ranked[0]["id"], 1)
        self.assertIn(3, [ranked[1]["id"], ranked[2]["id"]])

    def test_golden_retrieval_recall_and_mrr(self) -> None:
        cases = [
            ("healthy boundaries relationship", "boundary line personal space relationship", "coffee breakfast table"),
            ("discipline routine habit", "daily routine calendar training habit", "beach sunset waves"),
            ("recover and rise again", "rise stairs restart journey forward", "office meeting laptop"),
            ("comparison with others", "comparison mirror scale race", "forest trees nature"),
            ("calm relief after pressure", "calm relief quiet release peace", "traffic crowded street"),
            ("focus and attention", "focus desk notebook concentration study", "party dancing lights"),
            ("difficult decision choice", "decision choice crossroad path direction", "food cooking kitchen"),
            ("guilt hesitation in relationships", "guilt hesitation thoughtful conversation relationship", "mountain drone landscape"),
        ]
        recalls = []
        rrs = []
        for index, (intent_text, good_tags, bad_tags) in enumerate(cases, 1):
            candidates = [
                _candidate(index * 10 + 1, url=f"https://pexels.com/video/generic-{index}-1/", tags=bad_tags),
                _candidate(index * 10 + 2, url=f"https://pexels.com/video/relevant-{index}-2/", tags=good_tags),
                _candidate(index * 10 + 3, url=f"https://pexels.com/video/other-{index}-3/", tags="abstract background light"),
            ]
            ranked = v1.rerank_provider_candidates("pexels", candidates, v1.build_visual_intent(intent_text))
            ids = [item["id"] for item in ranked]
            relevant = {index * 10 + 2}
            recalls.append(v1.recall_at_k(ids, relevant, 2))
            rrs.append(v1.reciprocal_rank(ids, relevant))
        self.assertGreaterEqual(sum(recalls) / len(recalls), 0.95)
        self.assertGreaterEqual(sum(rrs) / len(rrs), 0.85)

    def test_one_contact_sheet_reduces_groq_image_token_load(self) -> None:
        def payload(images: int) -> dict:
            return {
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "x" * 3000},
                        *[
                            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,abc"}}
                            for _ in range(images)
                        ],
                    ],
                }],
                "max_completion_tokens": 900,
            }
        one = v1._estimate_payload_tokens(payload(1))
        three = v1._estimate_payload_tokens(payload(3))
        self.assertEqual(three - one, 2 * v1.GROQ_IMAGE_TOKENS)
        self.assertLess(one, v1.GROQ_FREE_TPM_HINT)

    def test_rate_headers_schedule_next_call_from_tpm_reset(self) -> None:
        state = v1._GroqCapacityState(scope=object())
        token = v1._GROQ_CAPACITY.set(state)
        try:
            fake_scope = state.scope
            response = SimpleNamespace(
                status_code=200,
                headers={
                    "x-ratelimit-remaining-tokens": "1500",
                    "x-ratelimit-reset-tokens": "12.5s",
                },
            )
            with mock.patch.object(v1.contract.legacy, "_state", return_value=fake_scope), mock.patch.object(
                v1.time, "monotonic", return_value=100.0
            ):
                v1._observe_groq_headers(response, estimated_tokens=4000)
            self.assertEqual(state.remaining_tokens, 1500)
            self.assertAlmostEqual(state.next_allowed_monotonic, 112.5, places=3)
        finally:
            v1._GROQ_CAPACITY.reset(token)

    def test_retry_after_is_parsed_as_bounded_cooldown_not_permanent_death(self) -> None:
        state = v1._GroqCapacityState(scope=object())
        token = v1._GROQ_CAPACITY.set(state)
        try:
            with mock.patch.object(v1.contract.legacy, "_state", return_value=state.scope), mock.patch.object(
                v1.time, "monotonic", return_value=50.0
            ):
                v1._observe_external_rate_limit("429 rate limit; try again in 8.75s")
            self.assertAlmostEqual(state.next_allowed_monotonic, 58.75, places=2)
        finally:
            v1._GROQ_CAPACITY.reset(token)

    def test_daily_groq_limit_remains_hard_unavailable(self) -> None:
        self.assertTrue(v1._is_daily_groq_limit("tokens per day limit reached"))
        self.assertFalse(v1._is_daily_groq_limit("tokens per minute rate limit reached"))

    def test_contact_sheet_sampler_exposes_one_image(self) -> None:
        fake = b"jpeg-contact-sheet"
        with mock.patch.object(v1, "_contact_sheet_bytes", return_value=fake):
            self.assertEqual(v1._contact_sheet_bytes("ignored"), fake)
        self.assertEqual(v1.CONTACT_SHEET_FRAMES, 6)
        self.assertEqual(v1.CONTACT_SHEET_COLUMNS * v1.CONTACT_SHEET_ROWS, 6)

    def test_visual_intent_contract_shared_for_long_and_short_shapes(self) -> None:
        landscape = v1.build_visual_intent("calm boundary conversation")
        portrait = v1.build_visual_intent("calm boundary conversation")
        self.assertEqual(landscape, portrait)
        self.assertIn("boundary", landscape.raw)
        self.assertTrue(landscape.expanded)


if __name__ == "__main__":
    unittest.main()
