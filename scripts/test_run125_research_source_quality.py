"""Run 125: do not cite unrelated or near-zero-engagement YouTube clips as demand.

Uses the existing two YouTube API responses; no live calls or extra models.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock
from urllib.parse import parse_qs, urlparse

from scripts import telegram_clean_v2_control as control
from scripts.research_relevance_filter import market_sample_relevance, relevance_tokens


class MarketRelevanceAndQualityTests(unittest.TestCase):
    def test_arabic_topic_anchors_survive_diacritics_clitics_and_punctuation(self) -> None:
        self.assertEqual(relevance_tokens("المقارنة"), relevance_tokens("والمُقارنة؟"))
        self.assertEqual(relevance_tokens("المقارنة"), relevance_tokens("بالمقارنة،"))
        self.assertEqual(relevance_tokens("المقارنة"), relevance_tokens("للمقارنة"))

    def test_screenshot_samples_remain_interest_data_without_content_claims(self) -> None:
        now = datetime.now(timezone.utc)
        videos = [{
            "id": video_id,
            "snippet": {"title": title, "description": "", "channelTitle": video_id,
                        "publishedAt": (now - timedelta(days=days)).isoformat()},
            "statistics": {"viewCount": str(views), "likeCount": str(likes)},
        } for video_id, title, views, likes, days in [
            ("book", "ثق بنفسك: توقف عن مقارنة نفسك بالآخرين واستعد قوتك", 560, 9, 3),
            ("landscape", "فخ المقارنة الصامت: لماذا مقارنة خطواتك بالآخرين", 1700, 40, 14),
        ]]
        with mock.patch.object(control, "youtube_search", return_value=videos):
            _, evidence = control.market_evidence("مقارنة الآخرين")
        self.assertEqual(evidence["sample_count"], 2)
        pack = control._research_pack(evidence)
        self.assertTrue(all(source["source_role"] == "market_interest" for source in pack))
        self.assertTrue(all("source_excerpt" not in source for source in pack))
        self.assertIn("لم يتوفر مقتطف معرفي صالح", control.render_selection_confirmation({
            "scope": "short", "approved_topic": "المقارنة", "research_pack": pack,
        }))

    def test_generic_description_cannot_launder_unrelated_video_title(self) -> None:
        query = "التعامل مع يوم بدون إنجاز"
        unrelated = {
            "title": "قرار واحد قد يغير حياتك",
            "description": "التعامل مع يوم بدون إنجاز #تطوير_الذات",
        }
        relevant = {
            "title": "يوم بدون إنجاز: كيف تتعامل مع تعثر يومك؟",
            "description": "تجربة التعثر وكيفية العودة بعد يوم ثقيل",
        }
        sober = {
            "title": "400 days off the booze today!",
            "description": "التعامل مع يوم بدون إنجاز",
        }
        self.assertFalse(market_sample_relevance(query, unrelated)[0])
        self.assertFalse(market_sample_relevance(query, sober)[0])
        self.assertTrue(market_sample_relevance(query, relevant)[0])

    def test_youtube_search_looks_at_ten_by_relevance_with_same_two_requests(self) -> None:
        search_hits = [{"id": {"videoId": "v" + str(i)}} for i in range(10)]
        video_hits = [{"id": "v" + str(i), "snippet": {}, "statistics": {}} for i in range(10)]
        with mock.patch.dict("os.environ", {"YOUTUBE_API_KEY": "unit-test"}, clear=False), mock.patch.object(
            control, "_json_request", side_effect=[{"items": search_hits}, {"items": video_hits}]
        ) as fetch:
            results = control.youtube_search("التعامل مع يوم بدون إنجاز")
        self.assertEqual(len(results), 10)
        self.assertEqual(fetch.call_count, 2)
        search_qs = parse_qs(urlparse(fetch.call_args_list[0].args[0]).query)
        self.assertEqual(search_qs["order"], ["relevance"])
        self.assertEqual(search_qs["maxResults"], ["10"])

    def test_market_uses_real_engagement_and_does_not_promote_bad_examples(self) -> None:
        date = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat().replace("+00:00", "Z")
        def item(video_id: str, title: str, views: int, likes: int | None, desc: str = "") -> dict:
            stats = {"viewCount": str(views)}
            if likes is not None:
                stats["likeCount"] = str(likes)
            return {
                "id": video_id,
                "snippet": {
                    "title": title, "description": desc, "channelTitle": video_id,
                    "publishedAt": date,
                },
                "statistics": stats,
            }
        videos = [
            # Run 125-style sample: 6 likes out of 1,400 is weak.
            item("weak", "يوم بدون إنجاز: التعامل مع التأجيل", 1400, 6),
            # Massive popularity does not rescue a completely unrelated title.
            item("unrelated", "قرار واحد يغير حياتك", 100000, 5000,
                 "التعامل مع يوم بدون إنجاز"),
            item("tiny", "كيف تتعامل مع يوم بدون إنجاز؟", 120, 20),
            item("supported", "يوم بدون إنجاز: كيف تتعامل معه؟", 5000, 200),
            item("undisclosed", "يوم بدون إنجاز: التعامل مع الإحباط", 1800, None),
        ]
        with mock.patch.object(control, "youtube_search", return_value=videos) as search:
            score, evidence = control.market_evidence("التعامل مع يوم بدون إنجاز")
        search.assert_called_once()
        self.assertGreater(score, 0)
        self.assertEqual(evidence["sample_count"], 2)
        self.assertEqual({s["video_id"] for s in evidence["top_samples"]}, {"supported", "undisclosed"})
        supported = next(s for s in evidence["top_samples"] if s["video_id"] == "supported")
        undisclosed = next(s for s in evidence["top_samples"] if s["video_id"] == "undisclosed")
        self.assertEqual(supported["likes"], 200)
        self.assertEqual(supported["like_rate_percent"], 4.0)
        self.assertIsNone(undisclosed["likes"])
        self.assertIsNone(undisclosed["like_rate_percent"])
        pack = control._research_pack(evidence)
        self.assertEqual({p["source_url"] for p in pack}, {
            "https://youtu.be/supported", "https://youtu.be/undisclosed"
        })
        confirmation = control.render_selection_confirmation({
            "scope": "short",
            "approved_topic": "كيف تتعامل مع يوم لم تنجز فيه شيئًا؟",
            "research_pack": pack,
        })
        self.assertIn("5000 مشاهدة", confirmation)
        self.assertIn("200 إعجاب", confirmation)
        self.assertIn("الإعجابات غير معلنة", confirmation)

    def test_all_weak_or_unrelated_samples_are_not_reported_as_market_evidence(self) -> None:
        date = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat().replace("+00:00", "Z")
        videos = [{
            "id": "weak",
            "snippet": {"title": "يوم بدون إنجاز: كيف تتعامل؟", "publishedAt": date},
            "statistics": {"viewCount": "1400", "likeCount": "6"},
        }]
        with mock.patch.object(control, "youtube_search", return_value=videos):
            score, result = control.market_evidence("التعامل مع يوم بدون إنجاز")
        self.assertEqual(score, 0)
        self.assertEqual(result["sample_count"], 0)
        self.assertEqual(control._research_pack(result), [])


if __name__ == "__main__":
    unittest.main()
