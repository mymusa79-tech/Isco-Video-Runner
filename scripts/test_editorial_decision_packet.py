import tempfile
import unittest

from scripts.editorial_decision_packet import EditorialPacketStore, bind_to_production_request, render_telegram, verify_production_binding


FIELDS = dict(
    topic="Topic A", angle="Angle A", promise="Promise A", hook="Hook A", tone="Warm", length="8m",
    visual_direction="Minimal cinematic", short_long_strategy="Long + sibling shorts", publish_target="Thursday 20:00",
)


class EditorialDecisionPacketTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = EditorialPacketStore(self.tmp.name)
        self.v1 = self.store.create(packet_id="pkt1", **FIELDS)

    def tearDown(self):
        self.tmp.cleanup()

    def test_packet_contains_all_nine_required_fields(self):
        for field in FIELDS:
            self.assertEqual(getattr(self.v1, field), FIELDS[field])

    def test_change_hook_creates_new_version_and_invalidates_approval(self):
        approved = self.store.approve("pkt1", expected_version=1)
        self.assertTrue(approved.approved_by_user)
        v2 = self.store.edit("pkt1", expected_version=1, changes={"hook":"Hook B"})
        self.assertEqual(v2.version, 2)
        self.assertFalse(v2.approved_by_user)
        self.assertIn("hook: Hook A → Hook B", v2.change_summary)

    def test_keep_topic_new_angle_changes_angle_only(self):
        v2 = self.store.edit("pkt1", expected_version=1, changes={"angle":"Angle B"})
        self.assertEqual(v2.topic, self.v1.topic)
        self.assertEqual(v2.angle, "Angle B")

    def test_three_alternatives_are_non_mutating_until_selection(self):
        alternatives = self.store.alternatives("pkt1", expected_version=1, field="hook", values=["H1", "H2", "H3"])
        self.assertEqual(self.store.current("pkt1").version, 1)
        self.assertEqual(self.store.current("pkt1").hook, "Hook A")
        v2 = self.store.choose_alternative(alternatives, 1)
        self.assertEqual(v2.version, 2)
        self.assertEqual(v2.hook, "H2")

    def test_approval_is_bound_to_exact_version(self):
        approved_v1 = self.store.approve("pkt1", expected_version=1)
        request = bind_to_production_request({"request_id":"req1"}, approved_v1)
        self.assertTrue(verify_production_binding(request, approved_v1))
        v2 = self.store.edit("pkt1", expected_version=1, changes={"hook":"new"})
        self.assertFalse(verify_production_binding(request, v2))
        self.assertFalse(v2.approved_by_user)

    def test_unapproved_packet_cannot_bind_to_production(self):
        with self.assertRaises(ValueError):
            bind_to_production_request({"request_id":"req1"}, self.v1)

    def test_stale_edit_is_rejected(self):
        self.store.edit("pkt1", expected_version=1, changes={"hook":"new"})
        with self.assertRaises(RuntimeError):
            self.store.edit("pkt1", expected_version=1, changes={"angle":"stale"})

    def test_version_history_preserves_old_decision(self):
        self.store.edit("pkt1", expected_version=1, changes={"hook":"new"})
        history = self.store.history("pkt1")
        self.assertEqual([x.version for x in history], [1, 2])
        self.assertEqual(history[0].hook, "Hook A")
        self.assertEqual(history[1].hook, "new")

    def test_publish_target_is_metadata_not_publish_permission(self):
        approved = self.store.approve("pkt1", expected_version=1)
        bound = bind_to_production_request({"request_id":"r"}, approved)
        self.assertIn("publish_target", FIELDS)
        self.assertNotIn("publish_approved", bound)
        self.assertNotIn("effective_decision", bound)

    def test_telegram_packet_is_chat_only_with_required_actions(self):
        text, keyboard = render_telegram(self.v1)
        self.assertIn("Editorial Decision Packet", text)
        labels = [button["text"] for row in keyboard for button in row]
        self.assertIn("✏️ Change Hook", labels)
        self.assertIn("↪️ Keep topic, new angle", labels)
        self.assertIn("3️⃣ Give me 3 alternatives", labels)
        self.assertNotIn("web_app", str(keyboard))


if __name__ == "__main__":
    unittest.main()
