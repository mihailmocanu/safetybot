"""Reporting integration: history isolation, scheduling and resumable delivery."""

import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from safety_bot.app import Application
from safety_bot.config import Config
from safety_bot.db import Store
from safety_bot.events import CATEGORIES
from safety_bot.report_service import Reporting, missed_windows
from safety_bot.reports import report_windows
from safety_bot.samsara import Samsara


UTC = timezone.utc
NEW_YORK = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 16, 18, tzinfo=UTC)
DAY = 86400


def event(identifier, stamp=None, *, asset="truck-1", name="2022", label="ModerateSpeeding"):
    stamp = stamp if stamp is not None else NOW.timestamp()
    return {
        "id": identifier, "asset": {"id": asset, "name": name},
        "startMs": datetime.fromtimestamp(stamp, UTC).isoformat(),
        "behaviorLabels": [{"label": label}], "media": [],
    }


class ReportScheduleTests(unittest.TestCase):
    def test_all_boundaries_after_long_restart_including_dst(self):
        start = datetime(2026, 7, 15, 12, tzinfo=NEW_YORK)
        end = datetime(2026, 11, 15, 20, tzinfo=NEW_YORK)
        jobs = missed_windows(start.timestamp(), end.timestamp(), "America/New_York")
        expected = []
        day = start.replace(hour=0, minute=0, second=0, microsecond=0)
        while day <= end:
            candidates = []
            if day.weekday() == 6:
                candidates.append(("weekly", day.replace(hour=20)))
            if day.day == 1:
                candidates.append(("monthly", day.replace(hour=8)))
            for kind, boundary in candidates:
                if start < boundary <= end:
                    expected.append((boundary.timestamp(), kind))
            day += timedelta(days=1)
        self.assertEqual([(job["end"], job["kind"]) for job in jobs], sorted(expected))
        self.assertEqual(len({job["key"] for job in jobs}), len(jobs))
        self.assertEqual(sum(job["kind"] == "monthly" for job in jobs), 4)
        # The checkpoint boundary itself has already been scheduled.
        since = datetime(2026, 11, 1, 20, tzinfo=NEW_YORK).timestamp()
        self.assertTrue(all(job["end"] > since for job in missed_windows(since, end.timestamp(), "America/New_York")))


class ReportServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "report-service.sqlite3")
        self.store = Store(self.path)
        self.cfg = Config("unused", "unused", -1001, reports_enabled=True)
        self.bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=10)))
        self.api = SimpleNamespace(safety_history=AsyncMock(), event=AsyncMock(return_value=None))
        self.stop = asyncio.Event()
        self.reporting = self.make_reporting()

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    def make_reporting(self):
        result = Reporting(self.cfg, self.store, self.api, self.bot, self.stop)
        result.pause = AsyncMock()
        return result

    def initialize(self, when=NOW):
        with patch("safety_bot.report_service.datetime", wraps=datetime) as clock:
            clock.now.return_value = when
            self.reporting.initialize()

    def reopen(self):
        self.store.close()
        self.store = Store(self.path)
        self.reporting = self.make_reporting()

    def latest_weekly_job(self):
        window = next(job for job in report_windows(NOW, "America/New_York") if job["kind"] == "weekly")
        self.store.schedule_reports([window], NOW.timestamp())
        return self.store.due_reports()[0]

    async def test_first_activation_does_not_send_previous_reports_and_restart_keeps_start(self):
        self.initialize()
        original_start = self.store.get("reports_started_at")
        with self.store.db:
            self.store.put("report_history_complete", NOW.isoformat())
        self.reporting.pause.side_effect = lambda _: self.stop.set()
        with patch("safety_bot.report_service.time.time", return_value=NOW.timestamp() + 10):
            await self.reporting.worker()
        self.assertEqual(self.store.due_reports(), [])
        self.bot.send_message.assert_not_awaited()
        self.assertEqual(float(self.store.get("report_schedule_until")), NOW.timestamp() + 10)
        self.reopen()
        self.initialize(NOW + timedelta(days=90))
        self.assertEqual(self.store.get("reports_started_at"), original_start)
        self.assertEqual(float(self.store.get("report_schedule_until")), NOW.timestamp() + 10)

    async def test_paginated_history_resumes_after_restart_without_alerts_or_live_cursor_change(self):
        self.store.save_page([event("existing-brake", label="Braking")], "live-cursor")
        self.store.sent_text("existing-brake", 77)
        self.initialize()
        history_start, history_end = self.store.get("report_history_start"), self.store.get("report_history_end")
        first = event("history-1", NOW.timestamp() - DAY)
        self.api.safety_history.side_effect = [
            {"data": [first], "pagination": {"hasNextPage": True, "endCursor": "history-page-1"}},
            asyncio.CancelledError(),
        ]
        with self.assertRaises(asyncio.CancelledError):
            await self.reporting.backfill()
        self.assertEqual(self.store.get("report_history_cursor"), "history-page-1")
        self.assertIsNone(self.store.get("report_history_complete"))
        self.reopen()
        self.initialize(NOW + timedelta(hours=2))
        self.api.safety_history.reset_mock(side_effect=True)
        self.api.safety_history.return_value = {
            "data": [first, event("history-2", NOW.timestamp() - 2 * DAY), event("not-speeding", label="Braking")],
            "pagination": {"hasNextPage": False, "endCursor": "history-page-2"},
        }
        await self.reporting.backfill()
        self.api.safety_history.assert_awaited_once_with(history_start, history_end, "history-page-1")
        self.assertEqual(self.store.get("report_history_complete"), history_end)
        self.assertEqual(self.store.get("cursor"), "live-cursor")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) AS n FROM speeding_occurrences").fetchone()["n"], 2)
        queued = self.store.db.execute("SELECT id,message_id FROM events").fetchall()
        self.assertEqual([(row["id"], row["message_id"]) for row in queued], [("existing-brake", 77)])
        self.bot.send_message.assert_not_awaited()
        self.api.safety_history.reset_mock()
        await self.reporting.backfill()
        self.api.safety_history.assert_not_awaited()

    async def test_cancel_mid_page_yields_and_replays_without_advancing_cursor(self):
        self.initialize()
        history_start, history_end = self.store.get("report_history_start"), self.store.get("report_history_end")
        records = [event(f"history-{i}", NOW.timestamp() - i * 60) for i in range(25)]
        self.api.safety_history.return_value = {
            "data": records, "pagination": {"hasNextPage": False, "endCursor": "whole-page"},
        }
        task = asyncio.create_task(self.reporting.backfill())
        # The importer runs one batch, commits, and yields to the event loop.
        # A concurrent task can then cancel it before any page cursor is saved.
        await asyncio.sleep(0)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) AS n FROM speeding_occurrences").fetchone()["n"], 10)
        self.assertFalse(self.store.db.in_transaction)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIsNone(self.store.get("report_history_cursor"))
        self.assertIsNone(self.store.get("report_history_complete"))
        self.reopen()
        self.api.safety_history.reset_mock()
        await self.reporting.backfill()
        self.api.safety_history.assert_awaited_once_with(history_start, history_end, None)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) AS n FROM speeding_occurrences").fetchone()["n"], 25)
        self.assertEqual(self.store.get("report_history_cursor"), "whole-page")
        self.assertEqual(self.store.get("report_history_complete"), history_end)
        self.assertEqual(self.store.due(), [])
        self.bot.send_message.assert_not_awaited()

    async def test_samsara_history_filters_speeding_and_keeps_snapshot_boundaries(self):
        api = Samsara(None, "unused", "https://api.samsara.com")
        page = {"data": [], "pagination": {"hasNextPage": False}}
        api.get = AsyncMock(return_value=page)
        start, end = "2026-08-17T18:00:00+00:00", NOW.isoformat()
        self.assertIs(await api.safety_history(start, end, "next-page"), page)
        path, params = api.get.await_args.args
        self.assertEqual(path, "/safety-events/stream")
        self.assertEqual(params["startTime"], start)
        self.assertEqual(params["endTime"], end)
        self.assertEqual(params["after"], "next-page")
        self.assertEqual(params["queryByTimeField"], "createdAtTime")
        self.assertEqual(set(params["behaviorLabels"].split(",")), CATEGORIES["Speeding"])
        self.assertEqual(params["includeVgOnlyEvents"], "true")
        self.assertEqual(params["includeAsset"], "true")
        await api.safety_history(start, end)
        self.assertNotIn("after", api.get.await_args.args[1])

    async def test_report_waits_for_live_stream_checkpoint_before_freezing(self):
        job = self.latest_weekly_job()
        with self.store.db:
            self.store.put("stream_caught_up_at", str(job["end_time"] - 1))
        await self.reporting.deliver(job)
        self.bot.send_message.assert_not_awaited()
        self.assertIsNone(self.store.due_reports()[0]["payload"])
        with self.store.db:
            self.store.put("stream_caught_up_at", str(job["end_time"]))
        await self.reporting.deliver(self.store.due_reports()[0])
        self.bot.send_message.assert_awaited_once()
        self.assertEqual(self.store.due_reports(), [])

    async def test_frozen_chunks_resume_after_failure_and_restart_without_duplicate(self):
        job = self.latest_weekly_job()
        records = [event(f"record-{i}", job["end_time"] - 60, asset=f"truck-{i}", name=f"{i}:" + "<&>" * 50) for i in range(10)]
        self.store.import_speeding_history(records)
        with self.store.db:
            self.store.put("stream_caught_up_at", str(job["end_time"]))
        self.bot.send_message.side_effect = [SimpleNamespace(message_id=101), OSError("temporary send failure")]
        with self.assertRaises(OSError):
            await self.reporting.deliver(job)
        saved = self.store.due_reports()[0]
        frozen = json.loads(saved["payload"])
        self.assertGreater(len(frozen), 1)
        self.assertTrue(self.store.report_chunk_sent(job["key"], 0))
        self.assertFalse(self.store.report_chunk_sent(job["key"], 1))
        # An event arriving after the first send cannot alter the report retry.
        self.store.import_speeding_history([event("late-arrival", job["end_time"] - 10)])
        self.reopen()
        self.bot.send_message.reset_mock(side_effect=True)
        self.bot.send_message.return_value = SimpleNamespace(message_id=102)
        await self.reporting.deliver(self.store.due_reports()[0])
        self.assertEqual([call.args[1] for call in self.bot.send_message.await_args_list], frozen[1:])
        self.assertTrue(self.store.report_chunk_sent(job["key"], 1))
        self.assertEqual(self.store.due_reports(), [])
        saved = self.store.db.execute("SELECT payload FROM report_jobs WHERE key=?", (job["key"],)).fetchone()
        self.assertEqual(json.loads(saved["payload"]), frozen)

    async def test_worker_waits_for_history_before_scheduling_and_delivery(self):
        self.initialize()
        self.reporting.pause.side_effect = lambda _: self.stop.set()
        with patch("safety_bot.report_service.time.time", return_value=NOW.timestamp() + 10 * DAY):
            await self.reporting.worker()
        self.assertEqual(self.store.due_reports(), [])
        self.assertEqual(float(self.store.get("report_schedule_until")), NOW.timestamp())
        self.bot.send_message.assert_not_awaited()

    async def test_alert_uses_recorded_rolling_count_without_repeating_alert(self):
        self.store.import_speeding_history([
            event("first", NOW.timestamp() - 2 * DAY),
            event("second", NOW.timestamp() - DAY),
        ])
        current = event("current")
        self.store.save_page([current], "live-cursor")
        with self.store.db:
            self.store.put("report_history_complete", NOW.isoformat())
        app = Application(self.cfg, self.store, self.api, self.bot)
        with patch("safety_bot.db.time.time", return_value=NOW.timestamp()), patch("safety_bot.app.asyncio.sleep", new=AsyncMock()):
            await app.deliver(self.store.due()[0])
            text = self.bot.send_message.await_args.args[1]
            self.assertIn("Speeding incidents in past 30 days: 3", text)
            self.store.schedule("current", -1)
            await app.deliver(self.store.due()[0])
        self.bot.send_message.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
