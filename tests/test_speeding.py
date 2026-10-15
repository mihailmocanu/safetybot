from datetime import datetime, timedelta, timezone
import unittest

from safety_bot.speeding import speed_samples, speeding_only


NOW = datetime(2026, 10, 15, 22, 0, tzinfo=timezone.utc)
MPH_TO_MPS = 0.44704


def reading(name, value, *, seconds_ago=0, asset="truck-1", timestamp=None):
    return {
        "entityId": asset,
        "readingId": name,
        "value": value,
        "happenedAtTime": timestamp or (NOW - timedelta(seconds=seconds_ago)).isoformat(),
    }


def pair(speed=61, limit=60, **kwargs):
    return [
        reading("samsaraSpeed", speed * MPH_TO_MPS, **kwargs),
        reading("samsaraSpeedLimit", limit * MPH_TO_MPS, **kwargs),
    ]


class SpeedSampleTests(unittest.TestCase):
    def test_strict_posted_limit_comparison_in_mps(self):
        for speed, expected in ((61, True), (60, False), (59, False), (0, False)):
            with self.subTest(speed=speed):
                sample, = speed_samples(pair(speed), now=NOW)
                self.assertIs(sample["over"], expected)
                self.assertAlmostEqual(sample["speed_kph"], speed * 1.609344)
                self.assertAlmostEqual(sample["limit_kph"], 60 * 1.609344)
                self.assertEqual(sample["asset_id"], "truck-1")
                self.assertEqual(sample["time"], "2026-10-15T22:00:00Z")
                self.assertEqual(sample["timestamp"], NOW.timestamp())

    def test_no_threshold_or_rounded_comparison(self):
        for speed, expected in ((60.00001, True), (59.99999, False)):
            sample, = speed_samples(pair(speed), now=NOW)
            self.assertEqual(round(sample["speed_kph"], 1), round(sample["limit_kph"], 1))
            self.assertIs(sample["over"], expected)

    def test_missing_limit_or_speed_is_not_a_non_speeding_sample(self):
        self.assertEqual(speed_samples([reading("samsaraSpeed", 30)], now=NOW), [])
        self.assertEqual(speed_samples([reading("samsaraSpeedLimit", 25)], now=NOW), [])
        self.assertEqual(speed_samples([], now=NOW), [])

    def test_stale_values_future_and_naive_timestamps_are_omitted(self):
        cases = (
            pair(seconds_ago=121),
            [reading("samsaraSpeed", 30, seconds_ago=121), reading("samsaraSpeedLimit", 25)],
            [reading("samsaraSpeed", 30), reading("samsaraSpeedLimit", 25, seconds_ago=121)],
            pair(seconds_ago=-31),
            pair(timestamp="2026-10-15T22:00:00"),
            pair(timestamp="invalid"),
        )
        for values in cases:
            with self.subTest(values=values):
                self.assertEqual(speed_samples(values, now=NOW), [])
        self.assertEqual(len(speed_samples(pair(seconds_ago=120), now=NOW)), 1)
        self.assertEqual(len(speed_samples(pair(seconds_ago=-30), now=NOW)), 1)

    def test_limit_age_remains_capped_if_speed_age_is_relaxed(self):
        values = [reading("samsaraSpeed", 30), reading("samsaraSpeedLimit", 25, seconds_ago=121)]
        self.assertEqual(speed_samples(values, now=NOW, max_age_seconds=600), [])

    def test_newest_reading_wins_out_of_order_without_invalid_fallback(self):
        values = [
            reading("samsaraSpeed", 30),
            reading("samsaraSpeed", 20, seconds_ago=30),
            reading("samsaraSpeedLimit", 35, seconds_ago=30),
            reading("samsaraSpeedLimit", 25),
        ]
        sample, = speed_samples(values, now=NOW)
        self.assertTrue(sample["over"])
        values.append(reading("samsaraSpeedLimit", None, seconds_ago=-1))
        self.assertEqual(speed_samples(values, now=NOW), [])

    def test_invalid_numbers_are_omitted(self):
        invalid = (None, True, False, "30", float("nan"), float("inf"), -1, {}, [])
        for name in ("samsaraSpeed", "samsaraSpeedLimit"):
            for value in invalid:
                values = pair()
                values[0 if name == "samsaraSpeed" else 1]["value"] = value
                with self.subTest(reading=name, value=value):
                    self.assertEqual(speed_samples(values, now=NOW), [])
        self.assertEqual(speed_samples(pair(limit=0), now=NOW), [])

    def test_multiple_assets_remain_separate(self):
        values = pair(asset="truck-1") + pair(speed=50, asset="truck-2")
        result = {sample["asset_id"]: sample["over"] for sample in speed_samples(values, now=NOW)}
        self.assertEqual(result, {"truck-1": True, "truck-2": False})

    def test_location_only_if_fresh_string(self):
        for value, age, expected in ((" Chicago, IL ", 0, "Chicago, IL"), ("Chicago, IL", 121, None), ({"city": "Chicago"}, 0, None), ("", 0, None)):
            with self.subTest(value=value, age=age):
                sample, = speed_samples(pair() + [reading("location", value, seconds_ago=age)], now=NOW)
                self.assertEqual(sample.get("location"), expected)

    def test_timestamp_offsets_are_normalized_and_epoch_now_supported(self):
        sample, = speed_samples(pair(timestamp="2026-10-15T18:00:00-04:00"), now=NOW.timestamp())
        self.assertEqual(sample["time"], "2026-10-15T22:00:00Z")


class SpeedingOnlyTests(unittest.TestCase):
    def test_speeding_with_context_is_duplicate_but_mixed_safety_event_is_not(self):
        def event(*labels):
            return {"behaviorLabels": [{"label": label} for label in labels]}

        self.assertTrue(speeding_only(event("ModerateSpeeding")))
        self.assertTrue(speeding_only(event("Speeding", "DefensiveDriving", "Invalid")))
        self.assertTrue(speeding_only({"behaviorLabels": [{"label": "Speeding"}, None, {"label": {}}]}))
        self.assertFalse(speeding_only(event("Speeding", "Braking")))
        self.assertFalse(speeding_only(event("Crash")))
        self.assertFalse(speeding_only(event("DefensiveDriving")))
        self.assertFalse(speeding_only(None))
        self.assertFalse(speeding_only({"behaviorLabels": None}))


if __name__ == "__main__":
    unittest.main()
