import json
from pathlib import Path
import tempfile
import unittest

from safety_bot.db import Store


ASSETS = {"truck-1": {"id": "truck-1", "name": "2022"}}


def sample(stamp, over=True):
    from datetime import datetime, timezone
    return {"asset_id": "truck-1", "timestamp": stamp,
            "time": datetime.fromtimestamp(stamp, timezone.utc).isoformat(),
            "speed_kph": 98 if over else 96, "limit_kph": 96, "over": over}


class LiveSpeedStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "state.db")
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_one_alert_per_continuous_episode_across_restart(self):
        self.assertEqual(self.store.record_speed_samples([sample(1000)], ASSETS), 1)
        event_id = self.store.due()[0]["id"]
        self.store.sent_text(event_id, 77)
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.record_speed_samples([sample(1060)], ASSETS), 0)
        self.assertEqual(self.store.due()[0]["message_id"], 77)
        self.assertEqual(self.store.record_speed_samples([], ASSETS), 0)
        self.assertEqual(self.store.record_speed_samples([sample(1120, False)], ASSETS), 0)
        self.assertEqual(self.store.record_speed_samples([sample(1180)], ASSETS), 1)
        self.assertEqual(len(self.store.due()), 2)

    def test_older_or_duplicate_samples_do_not_reopen_episode(self):
        self.store.record_speed_samples([sample(1000)], ASSETS)
        self.store.record_speed_samples([sample(1060, False)], ASSETS)
        self.assertEqual(self.store.record_speed_samples([sample(1060), sample(1000)], ASSETS), 0)
        state = self.store.db.execute("SELECT * FROM speed_state").fetchone()
        self.assertIsNone(state["episode_id"])

    def test_official_media_updates_attach_to_live_alert_without_new_text(self):
        self.store.record_speed_samples([sample(1000)], ASSETS)
        episode_id = self.store.due()[0]["id"]
        self.store.sent_text(episode_id, 77)
        official = {"id": "official-event", "asset": ASSETS["truck-1"],
                    "behaviorLabels": [{"label": "ModerateSpeeding"}],
                    "startMs": "1970-01-01T00:16:30Z", "endMs": "1970-01-01T00:18:00Z",
                    "media": [{"input": "dashcamRoadFacing", "url": "https://example.samsara.com/a.mp4"}]}
        self.store.save_page([official], "official-cursor", live_speeding=True)
        rows = self.store.due()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["message_id"], 77)
        self.assertEqual(json.loads(rows[0]["payload"])["_samsaraEventId"], "official-event")
        self.assertEqual(self.store.get("cursor"), "official-cursor")
        self.store.save_page([dict(official, id="later-event", media=[])], None, live_speeding=True)
        pending = json.loads(self.store.due()[0]["payload"])
        self.assertEqual(pending["_samsaraEventId"], "official-event")
        self.assertEqual(len(pending["media"]), 1)

    def test_media_arriving_before_sample_is_matched_later(self):
        official = {"id": "official-event", "asset": ASSETS["truck-1"],
                    "behaviorLabels": [{"label": "LightSpeeding"}],
                    "startMs": "1970-01-01T00:16:30Z", "endMs": "1970-01-01T00:18:00Z", "media": []}
        self.store.save_page([official], "c1", live_speeding=True)
        self.assertEqual(len(self.store.due()), 1)
        self.store.sent_text("official-event", 77)
        self.store.record_speed_samples([sample(1000)], ASSETS)
        rows = self.store.due()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["message_id"], 77)
        self.assertEqual(json.loads(rows[0]["payload"])["_samsaraEventId"], "official-event")

    def test_unknown_truck_is_not_given_an_invented_number(self):
        self.assertEqual(self.store.record_speed_samples([sample(1000)], {}), 0)
