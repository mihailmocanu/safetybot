"""Integration tests: TEST_DATABASE_URL must name a disposable empty PostgreSQL DB."""
import os
import unittest
from datetime import datetime, timezone

from safety_bot.db import Store
from safety_bot.reports import report_windows


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "No disposable PostgreSQL test database configured")
class PostgresTests(unittest.TestCase):
    def test_persistence_transactions_retry_and_singleton_lock(self):
        url = os.environ["TEST_DATABASE_URL"]
        store = Store(database_url=url)
        episode_id = ""
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
            # Exercise report/count SQL against PostgreSQL, not just SQLite.
            with store.db:
                store.put("report_history_complete", "test")
            episode_id = store.db.execute("SELECT id FROM speed_episodes WHERE asset_id=?", ("postgres-test-truck",)).fetchone()["id"]
            self.assertEqual(store.alert_speeding_count(episode_id, as_of=1001), 1)
            self.assertEqual(store.db.execute("SELECT COUNT(*) AS n FROM speeding_occurrences").fetchone()["n"], 1)
            windows = report_windows(datetime(1970, 1, 4, 22, tzinfo=timezone.utc), "UTC")
            store.schedule_reports(windows, 1001)
            job = store.due_reports()[0]
            self.assertEqual(store.report_stats(job)["total"], 1)
            store.freeze_report(job["key"], ["test report"])
            store.mark_report_chunk(job["key"], 0, 9000000003)
            self.assertTrue(store.report_chunk_sent(job["key"], 0))
            store.retry_report(job["key"], -1, "TemporaryError")
            self.assertEqual(store.due_reports()[0]["failures"], 1)
            store.finish_report(job["key"])
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
                store.db.execute("DELETE FROM speeding_aliases WHERE occurrence_id IN (SELECT id FROM speeding_occurrences WHERE asset_id=?)", ("postgres-test-truck",))
                store.db.execute("DELETE FROM alert_speed_counts WHERE event_id=?", (episode_id,))
                store.db.execute("DELETE FROM speeding_occurrences WHERE asset_id=?", ("postgres-test-truck",))
                store.db.execute("DELETE FROM report_chunks")
                store.db.execute("DELETE FROM report_jobs")
                store.db.execute("DELETE FROM meta WHERE key IN ('report_history_complete','report_schedule_until')")
            store.close()
