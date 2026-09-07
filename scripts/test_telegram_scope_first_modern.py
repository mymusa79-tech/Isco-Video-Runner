from __future__ import annotations

import unittest
from pathlib import Path

from scripts import telegram_scope_first_modern as scope_first


ROOT = Path(__file__).resolve().parents[1]


class _PanelStub:
    @staticmethod
    def _session(state, session_id):
        sessions = state.get("sessions")
        return sessions.get(session_id) if isinstance(sessions, dict) else None


class TelegramScopeFirstModernTests(unittest.TestCase):
    def test_search_surface_exposes_exact_three_modes(self) -> None:
        rows = scope_first._scoped_search_keyboard()
        callbacks = [button["callback_data"] for row in rows for button in row]
        self.assertEqual(
            callbacks[:3],
            ["cmd:topic_bundle", "cmd:topic_long", "cmd:short"],
        )
        self.assertEqual(callbacks[-1], "cmd:menu")
        self.assertIn("لا يبدأ Production", scope_first._scoped_search_text())

    def test_scope_binds_only_to_new_pending_long_action(self) -> None:
        state = {
            "pending_actions": [
                {"action_id": "old", "kind": "long", "status": "pending"},
                {"action_id": "new", "kind": "long", "status": "pending"},
            ]
        }
        changed = scope_first.bind_scope_to_new_long_request(
            state,
            requested_scope="bundle",
            before_ids={"old"},
        )
        self.assertTrue(changed)
        self.assertNotIn("requested_scope", state["pending_actions"][0])
        self.assertEqual(state["pending_actions"][1]["requested_scope"], "bundle")

    def test_existing_pending_long_request_cannot_be_silently_rescoped(self) -> None:
        state = {
            "pending_actions": [
                {
                    "action_id": "same",
                    "kind": "long",
                    "status": "pending",
                    "requested_scope": "bundle",
                }
            ]
        }
        changed = scope_first.bind_scope_to_new_long_request(
            state,
            requested_scope="long",
            before_ids={"same"},
        )
        self.assertFalse(changed)
        self.assertEqual(state["pending_actions"][0]["requested_scope"], "bundle")

    def test_saved_or_legacy_session_never_inherits_scope(self) -> None:
        self.assertIsNone(
            scope_first.scope_for_session(
                {"kind": "long", "source": "saved_suggestion", "session_id": "saved"}
            )
        )
        self.assertIsNone(scope_first.scope_for_session({"kind": "short", "requested_scope": "bundle"}))
        self.assertEqual(
            scope_first.scope_for_session({"kind": "long", "requested_scope": "long"}),
            "long",
        )

    def test_refresh_keeps_selected_long_scope(self) -> None:
        rows = [
            [{"text": "pick", "callback_data": "pick:s:0"}],
            [{"text": "more", "callback_data": "refresh:long"}],
        ]
        bundle = scope_first._rewrite_refresh_rows(rows, requested_scope="bundle")
        long_only = scope_first._rewrite_refresh_rows(rows, requested_scope="long")
        self.assertEqual(bundle[1][0]["callback_data"], "cmd:topic_bundle")
        self.assertEqual(long_only[1][0]["callback_data"], "cmd:topic_long")
        self.assertEqual(rows[1][0]["callback_data"], "refresh:long")

    def test_scoped_pick_translates_only_when_session_has_bound_scope(self) -> None:
        state = {
            "sessions": {
                "scoped": {"kind": "long", "requested_scope": "bundle"},
                "saved": {"kind": "long", "source": "saved_suggestion"},
            }
        }
        updates = [
            {"callback_query": {"data": "pick:scoped:1"}},
            {"callback_query": {"data": "pick:saved:0"}},
        ]
        rewritten = scope_first._rewrite_scoped_picks(_PanelStub, state, updates)
        self.assertEqual(rewritten[0]["callback_query"]["data"], "scope:scoped:1:bundle")
        self.assertEqual(rewritten[1]["callback_query"]["data"], "pick:saved:0")
        self.assertEqual(updates[0]["callback_query"]["data"], "pick:scoped:1")

    def test_live_entrypoints_install_same_policy(self) -> None:
        research = (ROOT / "scripts" / "telegram_topic_research_v2.py").read_text(encoding="utf-8")
        replay = (ROOT / "scripts" / "telegram_webhook_replay.py").read_text(encoding="utf-8")
        self.assertIn("telegram_scope_first_modern as scope_first", research)
        self.assertIn("scope_first.install(panel=core.panel, creator_v5=creator_v5, research_core=core)", research)
        self.assertIn("telegram_scope_first_modern as scope_first", replay)
        self.assertIn("scope_first.install(panel=core.panel, creator_v5=creator_v5)", replay)

    def test_edge_search_is_scope_first_but_scope_buttons_remain_stateful(self) -> None:
        worker = (ROOT / "cloudflare" / "telegram-control-worker" / "editorial-worker-v7.js").read_text(encoding="utf-8")
        for callback in ("cmd:topic_bundle", "cmd:topic_long", "cmd:short"):
            self.assertIn(callback, worker)
        self.assertIn('value === "cmd:search_menu"', worker)
        self.assertIn('kind === "long" ? "cmd:search_menu" : "cmd:short"', worker)
        # Edge owns only the read-only scope menu. Stateful scope buttons fall
        # through to the existing authenticated GitHub webhook/control path.
        self.assertNotIn('value === "cmd:topic_bundle"', worker)
        self.assertNotIn('value === "cmd:topic_long"', worker)
        self.assertNotIn("enqueue_request", worker)
        self.assertNotIn("production_target", worker)


if __name__ == "__main__":
    unittest.main()
