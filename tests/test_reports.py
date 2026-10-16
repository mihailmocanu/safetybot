from datetime import datetime, timedelta, timezone
from html import unescape
import re
import unittest
from zoneinfo import ZoneInfo

from safety_bot.reports import format_report, report_windows


UTC = timezone.utc
NEW_YORK = ZoneInfo("America/New_York")


def local(value):
    return datetime.fromisoformat(value).replace(tzinfo=NEW_YORK)


def by_kind(now):
    return {window["kind"]: window for window in report_windows(now, NEW_YORK)}


class ReportWindowsTests(unittest.TestCase):
    def test_sunday_before_and_at_schedule(self):
        before = by_kind(local("2026-10-18T19:59:59"))["weekly"]
        due = by_kind(local("2026-10-18T20:00:00"))["weekly"]
        self.assertEqual(datetime.fromtimestamp(before["end"], NEW_YORK), local("2026-10-11T20:00:00"))
        self.assertEqual(datetime.fromtimestamp(due["end"], NEW_YORK), local("2026-10-18T20:00:00"))
        self.assertEqual(datetime.fromtimestamp(due["start"], NEW_YORK), local("2026-10-11T20:00:00"))

    def test_monthly_before_and_at_eight(self):
        before = by_kind(local("2026-11-01T07:59:59"))["monthly"]
        due = by_kind(local("2026-11-01T08:00:00"))["monthly"]
        self.assertEqual(datetime.fromtimestamp(before["end"], NEW_YORK), local("2026-10-01T08:00:00"))
        self.assertEqual(datetime.fromtimestamp(due["end"], NEW_YORK), local("2026-11-01T08:00:00"))
        self.assertEqual(due["end"] - due["start"], 30 * 86400)
        self.assertEqual(due["start"] - due["previous_start"], 30 * 86400)

    def test_weekly_daylight_saving_uses_local_sunday_clock(self):
        for when, expected_hours in (("2026-03-08T20:00:00", 167), ("2026-11-01T20:00:00", 169)):
            with self.subTest(when=when):
                job = by_kind(local(when))["weekly"]
                self.assertEqual(job["end"] - job["start"], expected_hours * 3600)
                for key in ("end", "start", "previous_start"):
                    boundary = datetime.fromtimestamp(job[key], NEW_YORK)
                    self.assertEqual((boundary.weekday(), boundary.hour), (6, 20))
                self.assertEqual(job["end"] - job["rolling_start"], 30 * 86400)

    def test_year_and_leap_month_boundaries(self):
        january = by_kind(local("2027-01-01T07:00:00"))["monthly"]
        self.assertEqual(datetime.fromtimestamp(january["end"], NEW_YORK), local("2026-12-01T08:00:00"))
        march = by_kind(local("2028-03-01T08:00:00"))["monthly"]
        self.assertEqual(datetime.fromtimestamp(march["start"], NEW_YORK), local("2028-01-31T08:00:00"))

    def test_keys_do_not_depend_on_now_timezone(self):
        now = local("2026-11-01T20:00:00")
        self.assertEqual(report_windows(now, NEW_YORK), report_windows(now.astimezone(UTC), "America/New_York"))
        self.assertTrue(all(job["key"].endswith("+00:00") for job in report_windows(now)))
        with self.assertRaises(ValueError):
            report_windows(datetime(2026, 10, 18))


class ReportFormatTests(unittest.TestCase):
    def setUp(self):
        self.jobs = by_kind(local("2026-11-01T20:00:00"))
        self.stats = {
            "total": 4, "trucks": 1, "previous_total": 7, "rolling_total": 9,
            "ranking": [{"truck_name": "2022 <fleet>&", "count": 4, "past30": 9, "previous_count": 5}],
        }

    def test_weekly_counts_escape_names_and_display_timezone(self):
        text = "\n".join(format_report(self.jobs["weekly"], self.stats))
        self.assertIn("Weekly Speeding Report", text)
        self.assertIn("8:00 PM EDT", text)
        self.assertIn("8:00 PM EST", text)
        self.assertIn("Incidents: 4 | Trucks: 1", text)
        self.assertIn("Past 30 days: 9 incidents", text)
        self.assertIn("Previous week: 7 | Change: -3", text)
        self.assertIn("2022 &lt;fleet&gt;&amp; — 4 this week | 9 past 30 days", text)
        self.assertNotIn("points", text.lower())

    def test_monthly_previous_thirty_days(self):
        text = "\n".join(format_report(self.jobs["monthly"], self.stats))
        self.assertIn("Monthly Speeding Report", text)
        self.assertIn("4 incidents | previous 30d: 5", text)
        self.assertIn("Previous 30 days: 7 | Change: -3", text)

    def test_zero_and_missing_optional_values(self):
        stats = {"total": 0, "trucks": 0, "ranking": [], "fleet_count": None}
        text = "\n".join(format_report(self.jobs["weekly"], stats, coverage_complete=False))
        self.assertIn("No speeding incidents recorded", text)
        self.assertIn("history incomplete", text)
        self.assertNotIn("Active trucks", text)
        self.assertNotIn("Previous week", text)
        self.assertNotIn("Top 10", text)

    def test_large_external_names_fit_telegram_and_top_ten(self):
        self.stats["ranking"] = [
            {"truck_name": f"{i}:" + "<&>" * 150, "count": i + 1, "past30": i + 10}
            for i in range(143)
        ]
        chunks = format_report(self.jobs["weekly"], self.stats)
        self.assertGreater(len(chunks), 1)
        for text in chunks:
            self.assertLessEqual(len(text), 3900)
            self.assertLessEqual(len(unescape(re.sub(r"</?b>", "", text))), 3900)
            self.assertEqual(text.count("<b>"), text.count("</b>"))
        joined = "\n".join(chunks)
        self.assertIn("10. ", joined)
        self.assertNotIn("11. ", joined)


if __name__ == "__main__":
    unittest.main()
