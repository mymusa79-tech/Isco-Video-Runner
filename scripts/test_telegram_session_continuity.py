from __future__ import annotations

import unittest
from types import SimpleNamespace

from scripts import telegram_session_continuity as continuity


class TelegramSessionContinuityTests(unittest.TestCase):
    def _modules(self):
        active = SimpleNamespace(
            ACTIVE_RESEARCH_SESSION_KEY="active_research_session_id",
            PRODUCTION_TARGET_KEY="production_target",
        )

        def original_clear(state):
            state.pop(active.PRODUCTION_TARGET_KEY, None)
            state.pop(active.ACTIVE_RESEARCH_SESSION_KEY, None)

        def original_current_target(state):
            target = state.get(active.PRODUCTION_TARGET_KEY)
            if not isinstance(target, dict):
                return None
            if str(target.get("session_id") or "") != str(state.get(active.ACTIVE_RESEARCH_SESSION_KEY) or ""):
                return None
            return target

        active._clear_current_selection = original_clear
        active._current_target = original_current_target

        def original_approve(state, session, index, scope):
            session_id = str(session.get("session_id") or "")
            if session_id != str(state.get(active.ACTIVE_RESEARCH_SESSION_KEY) or ""):
                raise RuntimeError("This Telegram selection is not from the current research session")
            request = {
                "request_id": f"req-{session_id}-{index}",
                "request_sha256": "abc123",
                "approved_at": "2026-08-31T00:00:00+00:00",
                "approval_scope": "short_only" if scope == "short" else scope,
            }
            state.setdefault("requests", {})[request["request_id"]] = dict(request)
            state[active.PRODUCTION_TARGET_KEY] = {
                "request_id": request["request_id"],
                "request_sha256": request["request_sha256"],
                "session_id": session_id,
            }
            return request

        panel = SimpleNamespace(_approve=original_approve)
        return active, panel

    @staticmethod
    def _session(session_id, kind, created_at):
        return {
            "session_id": session_id,
            "kind": kind,
            "created_at": created_at,
            "candidates": [{"title": "candidate"}],
        }

    def test_failed_refresh_keeps_last_successful_session_and_approved_target(self):
        active, panel = self._modules()
        continuity.install(active=active, panel=panel)
        short = self._session("short-a", "short", "2026-08-31T00:00:00+00:00")
        state = {
            "sessions": {"short-a": short},
            "requests": {
                "req-short-a-0": {
                    "request_id": "req-short-a-0",
                    "request_sha256": "abc123",
                }
            },
            "active_research_session_id": "short-a",
            "production_target": {
                "request_id": "req-short-a-0",
                "request_sha256": "abc123",
                "session_id": "short-a",
            },
        }

        active._clear_current_selection(state)

        self.assertEqual(state["active_research_session_id"], "short-a")
        self.assertIn("production_target", state)
        self.assertEqual(active._current_target(state)["request_id"], "req-short-a-0")

    def test_invalid_active_pointer_is_removed_without_revoking_approval(self):
        active, panel = self._modules()
        continuity.install(active=active, panel=panel)
        state = {
            "sessions": {},
            "requests": {
                "req-1": {"request_id": "req-1", "request_sha256": "abc123"}
            },
            "active_research_session_id": "missing",
            "production_target": {
                "request_id": "req-1",
                "request_sha256": "abc123",
                "session_id": "old-session",
            },
        }

        active._clear_current_selection(state)

        self.assertNotIn("active_research_session_id", state)
        self.assertEqual(active._current_target(state)["request_id"], "req-1")

    def test_approved_target_survives_research_session_switch(self):
        active, panel = self._modules()
        continuity.install(active=active, panel=panel)
        short = self._session("short-a", "short", "2026-08-31T00:00:00+00:00")
        long = self._session("long-b", "long", "2026-08-31T00:05:00+00:00")
        state = {
            "sessions": {"short-a": short, "long-b": long},
            "requests": {
                "req-short-a-0": {
                    "request_id": "req-short-a-0",
                    "request_sha256": "abc123",
                }
            },
            "active_research_session_id": "long-b",
            "production_target": {
                "request_id": "req-short-a-0",
                "request_sha256": "abc123",
                "session_id": "short-a",
            },
        }

        target = active._current_target(state)

        self.assertIsNotNone(target)
        self.assertEqual(target["request_id"], "req-short-a-0")

    def test_tampered_or_missing_request_hash_is_rejected(self):
        active, panel = self._modules()
        continuity.install(active=active, panel=panel)
        state = {
            "sessions": {},
            "requests": {"req-1": {"request_id": "req-1", "request_sha256": "good"}},
            "production_target": {
                "request_id": "req-1",
                "request_sha256": "tampered",
                "session_id": "s",
            },
        }
        self.assertIsNone(active._current_target(state))
        state["production_target"]["request_sha256"] = "good"
        del state["requests"]["req-1"]
        self.assertIsNone(active._current_target(state))

    def test_latest_short_remains_selectable_when_long_is_globally_active(self):
        active, panel = self._modules()
        continuity.install(active=active, panel=panel)
        short = self._session("short-a", "short", "2026-08-31T00:00:00+00:00")
        long = self._session("long-b", "long", "2026-08-31T00:05:00+00:00")
        state = {
            "sessions": {"short-a": short, "long-b": long},
            "requests": {},
            "active_research_session_id": "long-b",
        }

        request = panel._approve(state, short, 0, "short")

        self.assertEqual(request["request_id"], "req-short-a-0")
        self.assertEqual(state["active_research_session_id"], "short-a")
        self.assertEqual(state["production_target"]["session_id"], "short-a")

    def test_older_short_is_still_rejected_when_newer_short_exists(self):
        active, panel = self._modules()
        continuity.install(active=active, panel=panel)
        old_short = self._session("short-old", "short", "2026-08-31T00:00:00+00:00")
        new_short = self._session("short-new", "short", "2026-08-31T00:05:00+00:00")
        long = self._session("long-current", "long", "2026-08-31T00:10:00+00:00")
        state = {
            "sessions": {
                "short-old": old_short,
                "short-new": new_short,
                "long-current": long,
            },
            "requests": {},
            "active_research_session_id": "long-current",
        }

        with self.assertRaisesRegex(RuntimeError, "not from the current research session"):
            panel._approve(state, old_short, 0, "short")
        self.assertEqual(state["active_research_session_id"], "long-current")
        self.assertNotIn("production_target", state)

    def test_latest_long_and_short_are_independently_eligible(self):
        active, panel = self._modules()
        continuity.install(active=active, panel=panel)
        short = self._session("short-a", "short", "2026-08-31T00:05:00+00:00")
        long = self._session("long-a", "long", "2026-08-31T00:06:00+00:00")
        state = {
            "sessions": {"short-a": short, "long-a": long},
            "requests": {},
            "active_research_session_id": "long-a",
        }

        panel._approve(state, short, 0, "short")
        self.assertEqual(state["active_research_session_id"], "short-a")
        panel._approve(state, long, 0, "long")
        self.assertEqual(state["active_research_session_id"], "long-a")

    def test_install_is_idempotent(self):
        active, panel = self._modules()
        continuity.install(active=active, panel=panel)
        first_clear = active._clear_current_selection
        first_current = active._current_target
        first_approve = panel._approve

        continuity.install(active=active, panel=panel)

        self.assertIs(active._clear_current_selection, first_clear)
        self.assertIs(active._current_target, first_current)
        self.assertIs(panel._approve, first_approve)


if __name__ == "__main__":
    unittest.main()
