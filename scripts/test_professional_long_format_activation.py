from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from isco_video_agent.brief_approval_binding import verify_brief_approval
from scripts.control_approved_brief import materialize_approved_brief, resolve_control_format
from scripts import telegram_long_format_policy


POLICY = {
    "version": "professional_long_format_router_v1",
    "requested": "auto",
    "resolution_stage": "v4_before_approved_brief_binding",
    "extra_ai_calls": 0,
}


def _source(index: int) -> dict[str, str]:
    return {
        "source_title": f"source {index}",
        "source_url": f"https://example.com/{index}",
        "claim_scope": "background only",
    }


def _long_request(topic: str, *, fmt: str = "auto") -> dict:
    request = {
        "kind": "long",
        "request_id": "req-format-test",
        "request_sha256": "a" * 64,
        "approved_by_user": True,
        "approved_topic": topic,
        "approved_at": "2026-09-04T12:00:00+00:00",
        "format": fmt,
        "research_pack": [_source(1), _source(2), _source(3)],
        "content_boundaries": [],
        "candidate": {"pillar": "understand"},
    }
    if fmt == "auto":
        request["format_policy"] = dict(POLICY)
    return request


class ProfessionalLongFormatActivationTests(unittest.TestCase):
    def test_analytical_long_resolves_to_film_before_binding(self) -> None:
        request = _long_request("لماذا نفقد الدافع بعد بداية قوية؟")
        self.assertEqual(resolve_control_format(request), "film")
        with tempfile.TemporaryDirectory() as td:
            path, approved_hash = materialize_approved_brief(request, Path(td) / "brief.json")
            brief = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(brief["format"], "film")
        self.assertNotEqual(brief["format"], "auto")
        self.assertEqual(verify_brief_approval(brief), approved_hash)

    def test_story_cluster_resolves_to_story_before_binding(self) -> None:
        request = _long_request("قصة الرجل الذي انتظر سنوات ثم بدأ من جديد")
        self.assertEqual(resolve_control_format(request), "story")
        with tempfile.TemporaryDirectory() as td:
            path, approved_hash = materialize_approved_brief(request, Path(td) / "brief.json")
            brief = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(brief["format"], "story")
        self.assertEqual(verify_brief_approval(brief), approved_hash)

    def test_auto_without_certified_policy_fails_closed(self) -> None:
        request = _long_request("قصة رجل بدأ من جديد")
        request.pop("format_policy")
        with self.assertRaisesRegex(RuntimeError, "lacks certified Telegram format policy"):
            resolve_control_format(request)

    def test_auto_with_wrong_policy_stage_fails_closed(self) -> None:
        request = _long_request("قصة رجل بدأ من جديد")
        request["format_policy"]["resolution_stage"] = "after_binding"
        with self.assertRaisesRegex(RuntimeError, "resolution stage is invalid"):
            resolve_control_format(request)

    def test_long_auto_cannot_resolve_to_moment(self) -> None:
        request = _long_request("شورت عن التسويف")
        with self.assertRaisesRegex(RuntimeError, "non-long format"):
            resolve_control_format(request)

    def test_auto_long_keeps_research_gate(self) -> None:
        request = _long_request("لماذا نفقد الدافع؟")
        request["research_pack"] = []
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaisesRegex(RuntimeError, "completed approved research pack"):
                materialize_approved_brief(request, Path(td) / "brief.json")

    def test_legacy_explicit_long_override_is_preserved(self) -> None:
        request = _long_request("قصة رجل بدأ من جديد", fmt="film")
        self.assertEqual(resolve_control_format(request), "film")

    def test_short_remains_moment(self) -> None:
        request = {
            "kind": "short",
            "format": "auto",
            "approved_topic": "لماذا نؤجل البداية؟",
        }
        self.assertEqual(resolve_control_format(request), "moment")

    def test_telegram_policy_turns_only_new_long_approval_into_auto_and_rehashes(self) -> None:
        state = {"requests": {}, "production_target": {"request_id": "req-1", "request_sha256": "old"}}

        def base_approve(state_obj, session, index, scope):
            request = {
                "kind": session["kind"],
                "request_id": "req-1",
                "request_sha256": "old",
                "format": "film" if session["kind"] == "long" else "moment",
            }
            state_obj["requests"]["req-1"] = request
            return request

        panel = SimpleNamespace(
            _approve=base_approve,
            _canonical_hash=lambda request: "rehash-" + str(request.get("format")),
        )
        telegram_long_format_policy.install(panel=panel)
        request = panel._approve(state, {"kind": "long"}, 0, "long")
        self.assertEqual(request["format"], "auto")
        self.assertEqual(request["format_policy"]["version"], telegram_long_format_policy.POLICY_VERSION)
        self.assertEqual(request["request_sha256"], "rehash-auto")
        self.assertEqual(state["production_target"]["request_sha256"], "rehash-auto")


if __name__ == "__main__":
    unittest.main()
