"""Integration tests: TEST_DATABASE_URL must name a disposable empty PostgreSQL DB."""
import os
import unittest

from safety_bot.db import Store


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "No disposable PostgreSQL test database configured")
class PostgresTests(unittest.TestCase):
    def test_persistence_transactions_retry_and_singleton_lock(self):
        url = os.environ["TEST_DATABASE_URL"]
        store = Store(database_url=url)
        try:
            count = store.db.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
            self.assertEqual(count, 0, "Use an empty disposable database for integration tests")
            sample = {"id": "postgres-integration-test", "media": []}
            store.save_page([sample], "cursor-test")
            store.sent_text(sample["id"], 9000000001)
            store.sent_video(sample["id"], "dashcamRoadFacing", 9000000002)
            store.schedule(sample["id"], -1, "TemporaryError")
            self.assertEqual(store.due()[0]["failures"], 1)
            store.schedule(sample["id"], -1)
            self.assertEqual(store.due()[0]["failures"], 0)
            self.assertTrue(store.video_sent(sample["id"], "dashcamRoadFacing"))
            with self.assertRaises(KeyError):
                store.save_page([{"id": "must-rollback"}, {}], "bad-cursor")
            self.assertEqual(store.get("cursor"), "cursor-test")
            self.assertEqual(len(store.due()), 1)
            with self.assertRaisesRegex(RuntimeError, "Another bot instance"):
                Store(database_url=url)
            store.close()
            store = Store(database_url=url)
            self.assertEqual(store.get("cursor"), "cursor-test")
            self.assertEqual(store.due()[0]["message_id"], 9000000001)
            sample = {"asset_id": "postgres-test-truck", "timestamp": 1000,
                      "time": "1970-01-01T00:16:40Z", "speed_kph": 98,
                      "limit_kph": 96, "over": True}
            self.assertEqual(store.record_speed_samples([sample], {"postgres-test-truck": {"id": "postgres-test-truck", "name": "Test"}}), 1)
            store.save_page([{
                "id": "postgres-official-speed-test", "asset": {"id": "postgres-test-truck"},
                "behaviorLabels": [{"label": "ModerateSpeeding"}],
                "startMs": "1970-01-01T00:16:30Z", "endMs": "1970-01-01T00:18:00Z", "media": [],
            }], None, live_speeding=True)
            self.assertEqual(store.db.execute("SELECT COUNT(*) AS n FROM speed_media").fetchone()["n"], 1)
        finally:
            with store.db:
                episodes = store.db.execute("SELECT id FROM speed_episodes WHERE asset_id=?", ("postgres-test-truck",)).fetchall()
                for episode in episodes:
                    store.db.execute("DELETE FROM events WHERE id=?", (episode["id"],))
                store.db.execute("DELETE FROM speed_media WHERE id=?", ("postgres-official-speed-test",))
                store.db.execute("DELETE FROM speed_state WHERE asset_id=?", ("postgres-test-truck",))
                store.db.execute("DELETE FROM speed_episodes WHERE asset_id=?", ("postgres-test-truck",))
                store.db.execute("DELETE FROM videos WHERE event_id=?", ("postgres-integration-test",))
                store.db.execute("DELETE FROM events WHERE id=?", ("postgres-integration-test",))
                store.db.execute("DELETE FROM meta WHERE key='cursor' AND value='cursor-test'")
            store.close()
