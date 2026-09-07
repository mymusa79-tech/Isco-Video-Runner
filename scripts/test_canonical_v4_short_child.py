from __future__ import annotations

import copy
import inspect
import unittest

from scripts import canonical_v4_short_child as child


def _request() -> dict:
    request = {
        "schema_version": 1,
        "request_id": "canonical-run-s1",
        "source": child.SOURCE,
        "kind": "short",
        "format": "moment",
        "approval_scope": "short_sibling",
        "approved_by_user": True,
        "approval_inherited_from_approved_brief": True,
        "approval_inherited_from_parent_bundle": True,
        "approved_topic": "فكرة مستقلة",
        "parent_approved_brief_sha256": "a" * 64,
        "source_episode_excerpt": {"source_section_id": "s1"},
        "production_dispatch_authorized": False,
        "status": "approved_waiting_production_activation",
        "youtube_publish_mode": "manual_in_youtube_studio",
    }
    request["request_sha256"] = child._canonical_hash(request)
    return request


class CanonicalV4ShortChildTests(unittest.TestCase):
    def test_valid_stored_request_is_non_dispatching_and_manual(self) -> None:
        request = _request()
        self.assertIs(child.validate_request(request, request["request_sha256"]), request)
        self.assertFalse(request["production_dispatch_authorized"])
        self.assertEqual(request["youtube_publish_mode"], "manual_in_youtube_studio")

    def test_hash_tamper_is_rejected(self) -> None:
        request = _request()
        expected = request["request_sha256"]
        request["approved_topic"] = "تم التلاعب"
        with self.assertRaisesRegex(RuntimeError, "changed after approval inheritance"):
            child.validate_request(request, expected)

    def test_wrong_source_or_missing_inherited_approval_is_rejected(self) -> None:
        for field, value, message in (
            ("source", "telegram_editorial_control_panel", "unsupported source"),
            ("approval_inherited_from_approved_brief", False, "lacks inherited user approval"),
        ):
            request = _request()
            request[field] = value
            request["request_sha256"] = child._canonical_hash(request)
            with self.assertRaisesRegex(RuntimeError, message):
                child.validate_request(request, request["request_sha256"])

    def test_stored_dispatch_authority_cannot_be_enabled(self) -> None:
        request = _request()
        request["production_dispatch_authorized"] = True
        request["request_sha256"] = child._canonical_hash(request)
        with self.assertRaisesRegex(RuntimeError, "must remain non-dispatching"):
            child.validate_request(request, request["request_sha256"])

    def test_youtube_mode_cannot_escape_manual(self) -> None:
        request = _request()
        request["youtube_publish_mode"] = "automatic"
        request["request_sha256"] = child._canonical_hash(request)
        with self.assertRaisesRegex(RuntimeError, "manual YouTube publication"):
            child.validate_request(request, request["request_sha256"])

    def test_exact_parent_section_provenance_is_required_before_runtime_capsule(self) -> None:
        request = _request()
        request["source_episode_excerpt"] = {}
        request["request_sha256"] = child._canonical_hash(request)
        with self.assertRaisesRegex(RuntimeError, "exact parent section provenance"):
            child.validate_request(request, request["request_sha256"])

    def test_execute_patches_live_router_and_parent_video_capsule_seams(self) -> None:
        source = inspect.getsource(child.execute)
        self.assertNotIn("production.install_router", source)
        self.assertIn("planning_runtime_contract.install_router", source)
        self.assertIn("build_parent_visual_capsule", source)
        self.assertIn("source_visual_capsule", source)
        self.assertIn("ISCO_SOURCE_PARENT_OUTPUT_DIR", inspect.getsource(child))

    def test_short_scope_and_moment_format_are_immutable(self) -> None:
        for field, value in (("format", "film"), ("approval_scope", "short_only"), ("kind", "long")):
            request = copy.deepcopy(_request())
            request[field] = value
            request["request_sha256"] = child._canonical_hash(request)
            with self.assertRaises(RuntimeError):
                child.validate_request(request, request["request_sha256"])


if __name__ == "__main__":
    unittest.main()