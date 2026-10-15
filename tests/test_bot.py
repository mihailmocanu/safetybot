import asyncio
import base64
import hashlib
import hmac
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from safety_bot.app import Application
from safety_bot.config import Config
from safety_bot.db import Store
from safety_bot.events import CATEGORIES, media_items, notification, selected
from safety_bot.webhook import valid_signature


def event(**changes):
    value = {
        "id": "event-1", "asset": {"id": "truck-1", "name": "1720"},
        "driver": {"name": "John Smith"}, "startMs": "2026-10-15T18:35:00Z",
        "behaviorLabels": [{"label": "Braking"}],
        "location": {"address": {"city": "Chicago", "state": "IL"}},
        "media": [],
    }
    return value | changes


class EventsTests(unittest.TestCase):
    def test_all_categories_and_zero_score(self):
        for category, values in CATEGORIES.items():
            for label in values:
                with self.subTest(category=category, label=label):
                    self.assertTrue(selected(event(behaviorLabels=[{"label": label}], score=0.0)))
        self.assertFalse(selected(event(behaviorLabels=[{"label": "DefensiveDriving"}])))

    def test_new_york_dst_and_missing_fields(self):
        text = notification(event(), "IDS Transportation", "America/New_York")
        self.assertIn("2:35 PM EDT", text)
        self.assertIn("Harsh Braking", text)
        text = notification(event(startMs="2026-01-15T18:35:00Z", driver=None, asset=None, location=None), "IDS", "America/New_York")
        self.assertIn("1:35 PM EST", text)
        self.assertNotIn("Driver:", text)
        self.assertNotIn("Truck:", text)
        self.assertNotIn("Location:", text)

    def test_compact_alert_hides_ids_and_escapes_external_text(self):
        text = notification(event(
            driver={"id": "0"}, asset={"name": "1720 <fleet>"},
            inboxEventUrl="https://cloud.samsara.com/event?id=1&x=2",
        ), "IDS Transportation", "America/New_York")
        self.assertIn("Truck: 1720 &lt;fleet&gt;", text)
        self.assertIn(">Open in Samsara</a>", text)
        self.assertIn("?id=1&amp;x=2", text)
        for unwanted in ("Driver ID", "Event ID", "Companie", "Video:", "SAMSARA SAFETY ALERT"):
            self.assertNotIn(unwanted, text)

    def test_speed_metrics_mph_and_actual_duration(self):
        speeding = event(
            behaviorLabels=[{"label": "ModerateSpeeding"}],
            endMs="2026-10-15T18:37:39Z",
            speedingMetadata={"maxSpeedKilometersPerHour": 116.0338944, "postedSpeedLimitKilometersPerHour": 97.0442112},
        )
        text = notification(speeding, "IDS", "America/New_York")
        self.assertIn("Max speed: 72.1 mph", text)
        self.assertIn("Posted limit: 60.3 mph", text)
        self.assertIn("Over posted limit: 11.8 mph", text)
        self.assertIn("Duration: 159 sec", text)
        self.assertNotIn("Max speed", notification(event(), "IDS", "America/New_York"))
        caption = notification(speeding, "IDS", "America/New_York", video=True)
        self.assertIn("Speeding Alert", caption)
        self.assertNotIn("Max speed", caption)

    def test_rotating_urls_do_not_create_camera_duplicates(self):
        self.assertEqual(len(media_items(event(media=[{"input": "dashcamRoadFacing", "url": "a"}, {"input": "dashcamRoadFacing", "url": "b"}]))), 1)

    def test_webhook_signature_and_replay_window(self):
        secret, body, timestamp = b"secret", b'{"eventType":"Ping"}', "1000"
        signature = "v1=" + hmac.new(secret, b"v1:1000:" + body, hashlib.sha256).hexdigest()
        self.assertTrue(valid_signature(secret, timestamp, signature, body, 1001))
        self.assertFalse(valid_signature(secret, timestamp, signature, body + b" ", 1001))
        self.assertFalse(valid_signature(secret, timestamp, signature, body, 1400))
        self.assertFalse(valid_signature(secret, "bad", signature, body, 1001))


class StoreTests(unittest.TestCase):
    def test_restart_dedup_and_atomic_page_rollback(self):
        with tempfile.TemporaryDirectory() as root:
            path = str(Path(root) / "events.sqlite3")
            store = Store(path)
            store.save_page([event()], "cursor-1")
            store.sent_text("event-1", 17)
            store.sent_video("event-1", "dashcamRoadFacing", 18)
            store.close()
            store = Store(path)
            self.assertEqual(store.get("cursor"), "cursor-1")
            store.save_page([event()], "cursor-2")
            self.assertEqual(store.due()[0]["message_id"], 17)
            self.assertTrue(store.video_sent("event-1", "dashcamRoadFacing"))
            with self.assertRaises(KeyError):
                store.save_page([event(id="event-2"), {}], "cursor-3")
            self.assertEqual(store.get("cursor"), "cursor-2")
            self.assertEqual(len(store.due()), 1)
            store.close()


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(str(Path(self.tmp.name) / "events.db"))
        self.store.save_page([event()], "c1")
        self.cfg = Config("unused", "unused", -1001)
        self.bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=5)), send_video=AsyncMock(return_value=SimpleNamespace(message_id=6)))
        self.api = SimpleNamespace(event=AsyncMock(return_value=event()), download=AsyncMock())
        self.app = Application(self.cfg, self.store, self.api, self.bot)

    async def asyncTearDown(self):
        self.store.close()
        self.tmp.cleanup()

    async def test_late_video_retry_without_duplicate_text_and_cleanup(self):
        with patch("safety_bot.app.asyncio.sleep", new=AsyncMock()):
            await self.app.deliver(self.store.due()[0])
            self.store.schedule("event-1", -1)
            self.api.event.return_value = event(media=[{"input": "dashcamRoadFacing", "url": "https://example.samsara.com/video"}])
            written = []

            async def download(url, path):
                path.write_bytes(b"fake-video")
                written.append(path)

            self.api.download.side_effect = download
            await self.app.deliver(self.store.due()[0])
            self.store.schedule("event-1", -1)
            await self.app.deliver(self.store.due()[0])
        self.bot.send_message.assert_awaited_once()
        self.bot.send_video.assert_awaited_once()
        self.assertEqual(self.bot.send_video.call_args.kwargs["parse_mode"], "HTML")
        self.assertNotIn("Event ID", self.bot.send_video.call_args.kwargs["caption"])
        self.assertEqual(self.bot.send_video.call_args.kwargs["reply_parameters"].message_id, 5)
        self.assertFalse(written[0].exists())

    async def test_upload_failure_preserves_text_and_cleans_temporary_file(self):
        self.api.event.return_value = event(media=[{"input": "dashcamRoadFacing", "url": "url"}])
        written = []

        async def download(url, path):
            path.write_bytes(b"fake-video")
            written.append(path)

        self.api.download.side_effect = download
        self.bot.send_video.side_effect = OSError("temporary network failure")
        with patch("safety_bot.app.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(OSError):
                await self.app.deliver(self.store.due()[0])
        self.assertEqual(self.store.due()[0]["message_id"], 5)
        self.assertFalse(self.store.video_sent("event-1", "dashcamRoadFacing"))
        self.assertFalse(written[0].exists())

    async def test_live_speeding_without_official_event_does_not_request_fake_samsara_id(self):
        live = event(_source="live-speeding", behaviorLabels=[{"label": "Speeding"}])
        self.store.save_page([live], None)
        with patch("safety_bot.app.asyncio.sleep", new=AsyncMock()):
            await self.app.deliver(self.store.due()[0])
        self.api.event.assert_not_awaited()
        self.bot.send_message.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
