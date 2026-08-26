from __future__ import annotations

import unittest

from scripts import telegram_rich_integration as integration


class FakeReleases:
    repository = "owner/repo"

    def __init__(self, release=None, quality=None):
        self.release = release
        self.quality = quality or {}

    def latest(self, prefix=None):
        if not self.release:
            return None
        tag = str(self.release.get("tag_name") or "")
        if prefix is None or tag.startswith(prefix):
            return self.release
        return None

    def asset_json(self, release, name):
        return self.quality.get(name)


class TelegramRichIntegrationTests(unittest.TestCase):
    def test_status_payload_shows_approved_target_without_starting_production(self):
        state = {
            "requests": {"req-1": {"request_id": "req-1", "approved_topic": "موضوع", "status": "approved_waiting_production_activation"}},
            "production_target": {"request_id": "req-1"},
        }
        payload = integration._status_payload(state, FakeReleases())
        self.assertEqual(payload["stage"], "approved_waiting_production_activation")
        self.assertEqual(payload["request_id"], "req-1")
        self.assertEqual(payload["title"], "موضوع")
        self.assertIn("تأكيد الإنتاج", payload["note"])

    def test_status_payload_maps_pending_dispatch_without_fake_progress(self):
        state = {
            "requests": {"req-1": {"request_id": "req-1", "approved_topic": "موضوع"}},
            "production_queue": [
                {"request_id": "req-1", "status": "pending_dispatch", "requested_at": "2026-08-26T18:00:00+00:00", "attempt": 1}
            ],
        }
        payload = integration._status_payload(state, FakeReleases())
        self.assertEqual(payload["stage"], "pending_dispatch")
        self.assertNotIn("progress", payload)

    def test_quality_payload_reads_list_from_state(self):
        payload = integration._quality_payload({"quality_gates": [{"name": "A", "passed": True}]}, FakeReleases())
        self.assertEqual(payload, {"gates": [{"name": "A", "passed": True}]})

    def test_quality_payload_reads_real_release_quality_assets(self):
        release = {"tag_name": "video-telegram-req-1", "published_at": "2026-08-26T18:10:00Z"}
        releases = FakeReleases(
            release,
            {
                "quality-final.json": {"duration_ok": True, "render_ok": True},
                "final-master-qc.json": {"evidence": {"status": "pass"}},
            },
        )
        payload = integration._quality_payload({}, releases)
        self.assertIsNotNone(payload)
        names = {gate["name"] for gate in payload["gates"]}
        self.assertIn("quality-final.json: duration_ok", names)
        self.assertIn("final-master-qc.json: evidence.status", names)


if __name__ == "__main__":
    unittest.main()
