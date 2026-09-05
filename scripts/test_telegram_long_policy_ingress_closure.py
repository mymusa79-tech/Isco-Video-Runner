from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import telegram_control_panel as panel
from scripts import telegram_long_format_policy as policy
from scripts import telegram_webhook_replay as replay
from scripts.control_approved_brief import resolve_control_format


def _source(index: int) -> dict[str, str]:
    return {
        "source_title": f"source {index}",
        "source_url": f"https://example.com/{index}",
        "claim_scope": "background only",
    }


def _long_request(
    *,
    request_id: str = "req-run203",
    fmt: str = "moment",
    legacy_pack: bool = True,
) -> dict:
    request = {
        "schema_version": 1,
        "request_id": request_id,
        "source": "telegram_editorial_control_panel",
        "kind": "long",
        "approval_scope": "long_only",
        "approved_by_user": True,
        "approved_at": "2026-09-05T14:00:00+00:00",
        "approved_topic": "كيف تنهض عندما تفقد الدافع تمامًا؟",
        "format": fmt,
        "production_dispatch_authorized": False,
        "status": "approved_waiting_production_activation",
    }
    pack = [_source(1), _source(2), _source(3)]
    if legacy_pack:
        request["approved_research_pack"] = pack
    else:
        request["research_pack"] = pack
    request["request_sha256"] = panel._canonical_hash(request)
    return request


def _short_request() -> dict:
    request = {
        "schema_version": 1,
        "request_id": "req-short",
        "source": "telegram_editorial_control_panel",
        "kind": "short",
        "approval_scope": "short_only",
        "approved_by_user": True,
        "approved_at": "2026-09-05T14:00:00+00:00",
        "approved_topic": "ابدأ قبل أن يعود الدافع",
        "format": "moment",
        "production_dispatch_authorized": False,
        "status": "approved_waiting_production_activation",
    }
    request["request_sha256"] = panel._canonical_hash(request)
    return request


def _state_for(request: dict, *, queue: list[dict] | None = None) -> dict:
    return {
        "schema_version": 1,
        "sessions": {},
        "requests": {request["request_id"]: request},
        "pending_actions": [],
        "production_queue": list(queue or []),
        "production_target": {
            "request_id": request["request_id"],
            "request_sha256": request["request_sha256"],
            "session_id": "session-run203",
        },
    }


def _assert_hash_valid(test: unittest.TestCase, request: dict) -> None:
    subject = {key: value for key, value in request.items() if key != "request_sha256"}
    test.assertEqual(request["request_sha256"], panel._canonical_hash(subject))


class Run203LongPolicyIngressClosureTests(unittest.TestCase):
    def test_new_long_policy_canonicalizes_pack_routes_auto_and_rehashes_target(self) -> None:
        request = _long_request(fmt="film")
        old_hash = request["request_sha256"]
        state = _state_for(request)

        result = policy.apply_new_long_format_policy(state, request, panel=panel)

        self.assertIs(result, request)
        self.assertEqual(request["format"], "auto")
        self.assertEqual(request["format_policy"], policy._policy_document())
        self.assertIn("research_pack", request)
        self.assertNotIn("approved_research_pack", request)
        self.assertNotEqual(request["request_sha256"], old_hash)
        self.assertEqual(state["production_target"]["request_sha256"], request["request_sha256"])
        _assert_hash_valid(self, request)

    def test_policy_does_not_steal_approved_brief_research_sufficiency_gate(self) -> None:
        request = _long_request(fmt="film", legacy_pack=False)
        request.pop("research_pack")
        request.pop("request_sha256")
        request["request_sha256"] = panel._canonical_hash(request)
        state = _state_for(request)

        policy.apply_new_long_format_policy(state, request, panel=panel)
        self.assertEqual(request["format"], "auto")
        self.assertNotIn("research_pack", request)
        _assert_hash_valid(self, request)

    def test_current_legacy_long_migrates_atomically_and_idempotently(self) -> None:
        request = _long_request(fmt="moment")
        old_hash = request["request_sha256"]
        state = _state_for(
            request,
            queue=[
                {
                    "request_id": request["request_id"],
                    "request_sha256": old_hash,
                    "status": "failed",
                }
            ],
        )

        self.assertTrue(policy.migrate_current_production_target(state, panel=panel))
        new_hash = request["request_sha256"]
        self.assertNotEqual(new_hash, old_hash)
        self.assertEqual(request["format"], "auto")
        self.assertEqual(request["format_policy"], policy._policy_document())
        self.assertIn("research_pack", request)
        self.assertNotIn("approved_research_pack", request)
        self.assertEqual(state["production_target"]["request_sha256"], new_hash)
        _assert_hash_valid(self, request)

        self.assertFalse(policy.migrate_current_production_target(state, panel=panel))
        self.assertEqual(request["request_sha256"], new_hash)
        self.assertEqual(state["production_target"]["request_sha256"], new_hash)

    def test_current_explicit_film_preserves_human_format_while_canonicalizing_pack(self) -> None:
        request = _long_request(fmt="film")
        state = _state_for(request)

        self.assertTrue(policy.migrate_current_production_target(state, panel=panel))
        self.assertEqual(request["format"], "film")
        self.assertNotIn("format_policy", request)
        self.assertIn("research_pack", request)
        self.assertNotIn("approved_research_pack", request)
        _assert_hash_valid(self, request)

    def test_short_target_remains_moment_and_is_not_migrated(self) -> None:
        request = _short_request()
        old_hash = request["request_sha256"]
        state = _state_for(request)

        self.assertFalse(policy.migrate_current_production_target(state, panel=panel))
        self.assertEqual(request["format"], "moment")
        self.assertEqual(request["request_sha256"], old_hash)

    def test_live_dispatch_blocks_migration_without_mutating_identity(self) -> None:
        request = _long_request(fmt="moment")
        old_hash = request["request_sha256"]
        state = _state_for(
            request,
            queue=[
                {
                    "request_id": request["request_id"],
                    "request_sha256": old_hash,
                    "status": "dispatch_reserved",
                }
            ],
        )

        self.assertFalse(policy.migrate_current_production_target(state, panel=panel))
        self.assertEqual(request["format"], "moment")
        self.assertEqual(request["request_sha256"], old_hash)
        self.assertIn("approved_research_pack", request)
        self.assertEqual(state["production_target"]["request_sha256"], old_hash)

    def test_tampered_current_target_fails_closed_before_migration(self) -> None:
        request = _long_request(fmt="moment")
        state = _state_for(request)
        request["approved_topic"] = "tampered after approval"

        with self.assertRaisesRegex(RuntimeError, "request hash is invalid"):
            policy.migrate_current_production_target(state, panel=panel)
        self.assertEqual(request["format"], "moment")

    def test_raw_long_moment_still_fails_at_downstream_approved_brief_guard(self) -> None:
        request = _long_request(fmt="moment", legacy_pack=False)
        with self.assertRaisesRegex(RuntimeError, "Unsupported long control format: moment"):
            resolve_control_format(request)

    def test_webhook_installs_long_policy_after_final_ui_and_authority_wrappers(self) -> None:
        from scripts import telegram_creator_control_center_v5 as creator_v5
        from scripts import telegram_operator_mission_control as mission
        from scripts import telegram_session_continuity as continuity

        active = replay.core.active
        original_install = active._install
        had_flag = hasattr(active, "_isco_v5_replay_hooked")
        old_flag = getattr(active, "_isco_v5_replay_hooked", None)
        calls: list[str] = []
        try:
            active._install = lambda: calls.append("active")
            active._isco_v5_replay_hooked = False
            with mock.patch.object(creator_v5, "install", side_effect=lambda *args, **kwargs: calls.append("v5")), \
                 mock.patch.object(continuity, "install", side_effect=lambda **kwargs: calls.append("continuity")), \
                 mock.patch.object(mission, "install", side_effect=lambda: calls.append("mission")), \
                 mock.patch.object(policy, "install", side_effect=lambda **kwargs: calls.append("long_policy")):
                replay._install_v5_after_active()
                active._install()
            self.assertEqual(calls, ["active", "v5", "continuity", "mission", "long_policy"])
        finally:
            active._install = original_install
            if had_flag:
                active._isco_v5_replay_hooked = old_flag
            else:
                delattr(active, "_isco_v5_replay_hooked")

    def test_wrapper_repair_persists_migrated_target_to_state_file(self) -> None:
        request = _long_request(fmt="moment")
        state = _state_for(request)
        old_hash = request["request_sha256"]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            panel.save_state(path, state)

            self.assertTrue(replay._repair_current_long_target_if_available(path))
            restored = panel.load_state(path)

        migrated = restored["requests"][request["request_id"]]
        self.assertEqual(migrated["format"], "auto")
        self.assertNotEqual(migrated["request_sha256"], old_hash)
        self.assertEqual(restored["production_target"]["request_sha256"], migrated["request_sha256"])
        self.assertIn("research_pack", migrated)
        self.assertNotIn("approved_research_pack", migrated)
        _assert_hash_valid(self, migrated)


if __name__ == "__main__":
    unittest.main()
