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
        finally:
            with store.db:
                store.db.execute("DELETE FROM videos WHERE event_id=?", ("postgres-integration-test",))
                store.db.execute("DELETE FROM events WHERE id=?", ("postgres-integration-test",))
                store.db.execute("DELETE FROM meta WHERE key='cursor' AND value='cursor-test'")
            store.close()
