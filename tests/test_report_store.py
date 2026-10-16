"""Persistence and counting rules for speeding reports, independent of Telegram."""
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from safety_bot.db import Store


DAY = 86400
NOW = datetime(2026, 10, 16, 18, tzinfo=timezone.utc).timestamp()


def event(event_id, stamp=NOW, *, asset_id="truck-1", name="1720", labels=None, **extra):
    result = {
        "id": event_id,
        "asset": {"id": asset_id, "name": name},
        "startMs": datetime.fromtimestamp(stamp, timezone.utc).isoformat(),
        "behaviorLabels": [{"label": value} for value in (labels or ["ModerateSpeeding"])],
        "media": [],
    }
    result.update(extra)
    return result


def window(key="weekly:2026-10-16", *, start=NOW - 7 * DAY, end=NOW):
    return {
        "key": key, "kind": "weekly", "start": start, "end": end,
        "previous_start": start - 7 * DAY,
        "rolling_start": end - 30 * DAY, "timezone": "America/New_York",
    }


class ReportStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "reports.sqlite3")
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def complete_history(self):
        with self.store.db:
            self.store.put("report_history_complete", "2026-10-16T18:00:00Z")

    def occurrences(self):
        return [dict(row) for row in self.store.db.execute(
            "SELECT * FROM speeding_occurrences ORDER BY occurred_at,id"
        ).fetchall()]

    def test_same_event_media_refresh_and_mixed_labels_count_once(self):
        original = event("speed-1", labels=["ModerateSpeeding", "Braking"])
        self.store.save_page([original, original], "a")
        refreshed = dict(original, media=[{"input": "MEDIA_INPUT_PRIMARY", "url": "https://example.invalid/video.mp4"}])
        self.store.save_page([refreshed], "b")
        self.store.seed_speeding_history()
        self.store.import_speeding_history([original, refreshed], "history")
        self.assertEqual(len(self.occurrences()), 1)
        self.complete_history()
        self.assertEqual(self.store.alert_speeding_count("speed-1", NOW), 1)

    def test_non_speeding_and_incomplete_records_do_not_create_occurrences(self):
        malformed = [
            event("brake", labels=["Braking"]),
            event("invalid-time", startMs="not-a-date"),
            event("no-asset", asset={}),
            event("no-id", id=""),
        ]
        self.store.import_speeding_history(malformed)
        self.assertEqual(self.occurrences(), [])

    def test_updates_keep_original_occurrence_time_but_refresh_truck_name(self):
        original_time = NOW - 31 * DAY
        self.store.save_page([event("speed", original_time)], "a")
        self.store.save_page([event("speed", NOW, name="Renamed 1720")], "b")
        row, = self.occurrences()
        self.assertEqual(row["occurred_at"], original_time)
        self.assertEqual(row["truck_name"], "Renamed 1720")
        self.complete_history()
        self.assertIsNone(self.store.alert_speeding_count("speed", NOW))

    def test_rolling_30_day_count_includes_current_and_lower_boundary_only_for_same_asset(self):
        self.store.import_speeding_history([
            event("expired", NOW - 30 * DAY - 0.001),
            event("boundary", NOW - 30 * DAY),
            event("recent", NOW - 2 * DAY),
            event("current", NOW),
            event("future", NOW + 1),
            event("other-truck", NOW - DAY, asset_id="truck-2", name="1720"),
        ])
        self.assertIsNone(self.store.alert_speeding_count("current", NOW))
        self.complete_history()
        self.assertEqual(self.store.alert_speeding_count("current", NOW), 3)
        self.assertIsNone(self.store.alert_speeding_count("expired", NOW))
        self.assertIsNone(self.store.alert_speeding_count("future", NOW))
        self.assertEqual(self.store.alert_speeding_count("other-truck", NOW), 1)

    def test_alert_count_is_frozen_across_retries_late_imports_and_restart(self):
        self.store.import_speeding_history([event("first", NOW - DAY), event("current", NOW)])
        self.complete_history()
        self.assertEqual(self.store.alert_speeding_count("current", NOW), 2)
        self.store.import_speeding_history([event("late-history", NOW - 2 * DAY)])
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.alert_speeding_count("current", NOW + 31 * DAY), 2)
        self.assertEqual(self.store.alert_speeding_count("late-history", NOW), 3)

    def test_history_import_is_ledger_only_and_has_an_independent_cursor(self):
        self.store.save_page([event("already-queued")], "live-cursor")
        self.store.sent_text("already-queued", 12345)
        self.store.import_speeding_history([event("historical", NOW - DAY)], "history-cursor")
        self.assertEqual(self.store.get("cursor"), "live-cursor")
        self.assertEqual(self.store.get("report_history_cursor"), "history-cursor")
        queue = self.store.db.execute("SELECT id,message_id FROM events").fetchall()
        self.assertEqual([(row["id"], row["message_id"]) for row in queue], [("already-queued", 12345)])
        self.assertEqual(len(self.occurrences()), 2)

    def test_history_import_matches_live_episode_without_queue_or_media_changes(self):
        stamp = NOW - 60
        sample = {
            "asset_id": "truck-1", "timestamp": stamp,
            "time": datetime.fromtimestamp(stamp, timezone.utc).isoformat(),
            "speed_kph": 98, "limit_kph": 96, "over": True,
        }
        self.assertEqual(self.store.record_speed_samples([sample], {"truck-1": {"id": "truck-1", "name": "1720"}}), 1)
        live_id = self.store.db.execute("SELECT id FROM speed_episodes").fetchone()["id"]
        queue_before = [dict(row) for row in self.store.db.execute("SELECT * FROM events").fetchall()]
        official = event("official", stamp - 10, endMs=datetime.fromtimestamp(NOW, timezone.utc).isoformat(),
                         media=[{"input": "MEDIA_INPUT_PRIMARY", "url": "https://example.invalid/video.mp4"}])
        self.store.import_speeding_history([official])
        self.assertEqual(len(self.occurrences()), 1)
        self.assertEqual([dict(row) for row in self.store.db.execute("SELECT * FROM events").fetchall()], queue_before)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) AS n FROM speed_media").fetchone()["n"], 0)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) AS n FROM videos").fetchone()["n"], 0)
        self.store.save_page([official], "live-cursor", live_speeding=True)
        self.store.import_speeding_history([official])
        row, = self.occurrences()
        self.assertEqual(row["id"], live_id)
        self.assertEqual(row["occurred_at"], stamp)
        self.complete_history()
        self.assertEqual(self.store.alert_speeding_count(live_id, NOW), 1)
        self.assertEqual(self.store.alert_speeding_count("official", NOW), 1)
        self.assertEqual(len(self.store.db.execute("SELECT id FROM events").fetchall()), 1)

    def test_history_does_not_merge_mixed_events_other_assets_or_nonoverlapping_episodes(self):
        asset = {"truck-1": {"id": "truck-1", "name": "1720"}}
        def sample(stamp, over):
            return {"asset_id": "truck-1", "timestamp": stamp,
                    "time": datetime.fromtimestamp(stamp, timezone.utc).isoformat(),
                    "speed_kph": 98, "limit_kph": 96, "over": over}
        self.store.record_speed_samples([sample(NOW - 60, True), sample(NOW, False)], asset)
        self.store.import_speeding_history([
            event("mixed", NOW - 50, labels=["Speeding", "Braking"]),
            event("different-truck", NOW - 50, asset_id="truck-2"),
            event("too-early", NOW - 181),
            event("too-late", NOW + 121),
        ])
        self.assertEqual(len(self.occurrences()), 5)
        self.assertEqual(len(self.store.db.execute("SELECT id FROM events").fetchall()), 1)

    def test_report_stats_use_nonoverlapping_periods_and_asset_identity(self):
        start = NOW - 7 * DAY
        self.store.import_speeding_history([
            event("prior-start", start - 7 * DAY),
            event("prior-end", start - 1),
            event("current-start", start),
            event("current-late", NOW - 1),
            event("next-period", NOW),
            event("same-name-different-truck", NOW - 2, asset_id="truck-2", name="1720"),
            event("rolling-boundary", NOW - 30 * DAY),
            event("outside-rolling", NOW - 30 * DAY - 1),
            event("unnamed", NOW - 3, asset_id="truck-3", name=""),
        ])
        self.store.schedule_reports([window()], NOW)
        job, = self.store.due_reports()
        stats = self.store.report_stats(job)
        self.assertEqual(stats["total"], 4)
        self.assertEqual(stats["trucks"], 3)
        self.assertEqual(stats["previous_total"], 2)
        self.assertEqual(stats["rolling_total"], 7)
        top, second, third = stats["ranking"]
        self.assertEqual((top["asset_id"], top["count"], top["past30"], top["previous_count"]), ("truck-1", 2, 5, 2))
        self.assertEqual(second["asset_id"], "truck-2")
        self.assertEqual(third["truck_name"], "Asset truck-3")

    def test_report_ranking_is_limited_to_ten_but_fleet_totals_include_every_truck(self):
        records = [event(f"event-{i}", NOW - 10, asset_id=f"truck-{i:02}", name=f"{i:02}") for i in range(12)]
        self.store.import_speeding_history(records)
        self.store.schedule_reports([window()], NOW)
        stats = self.store.report_stats(self.store.due_reports()[0])
        self.assertEqual((stats["total"], stats["trucks"]), (12, 12))
        self.assertEqual(len(stats["ranking"]), 10)
        self.assertEqual([row["truck_name"] for row in stats["ranking"]], [f"{i:02}" for i in range(10)])

    def test_report_outbox_payload_chunks_retry_and_completion_survive_restart(self):
        expected = window()
        self.store.schedule_reports([expected, expected], NOW)
        self.store.freeze_report(expected["key"], ["First chunk", "Second chunk"])
        self.store.freeze_report(expected["key"], ["Must not replace frozen report"])
        self.store.mark_report_chunk(expected["key"], 0, 9000000001)
        with patch("safety_bot.db.time.time", return_value=NOW):
            self.store.retry_report(expected["key"], 60, "TemporaryError")
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(float(self.store.get("report_schedule_until")), NOW)
        with patch("safety_bot.db.time.time", return_value=NOW + 59):
            self.assertEqual(self.store.due_reports(), [])
        with patch("safety_bot.db.time.time", return_value=NOW + 60):
            job, = self.store.due_reports()
        self.assertEqual(json.loads(job["payload"]), ["First chunk", "Second chunk"])
        self.assertEqual((job["failures"], job["last_error"]), (1, "TemporaryError"))
        self.assertTrue(self.store.report_chunk_sent(expected["key"], 0))
        self.assertFalse(self.store.report_chunk_sent(expected["key"], 1))
        self.store.mark_report_chunk(expected["key"], 1, 9000000002)
        self.store.finish_report(expected["key"])
        self.store.schedule_reports([expected], NOW + DAY)
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.due_reports(), [])
        self.assertTrue(self.store.report_chunk_sent(expected["key"], 1))
        row = self.store.db.execute("SELECT sent_at,last_error FROM report_jobs WHERE key=?", (expected["key"],)).fetchone()
        self.assertIsNotNone(row["sent_at"])
        self.assertIsNone(row["last_error"])


if __name__ == "__main__":
    unittest.main()
