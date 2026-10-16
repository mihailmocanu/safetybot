"""Calendar report windows and concise Telegram report formatting.

Weekly windows follow the configured local clock, including daylight saving
changes. Monthly reports always count an exact rolling 30 days in UTC.
"""

from datetime import datetime, timedelta, timezone
from html import escape
from zoneinfo import ZoneInfo


UTC = timezone.utc
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
TELEGRAM_REPORT_LIMIT = 3900


def _previous_month(value):
    year, month = value.year, value.month - 1
    if month == 0:
        year, month = year - 1, 12
    return value.replace(year=year, month=month, day=1)


def report_windows(now, tz="America/New_York", lookback_days=35):
    """Return the latest completed weekly and monthly scheduled windows.

    ``now`` must be timezone aware. All returned boundaries are UTC epoch
    seconds, suitable for database comparisons using half-open [start, end)
    intervals. Keys use canonical UTC values, independent of the timezone in
    which ``now`` was supplied. The caller decides which windows are new and
    persists their keys; this helper does not send reports or backfill jobs.
    """
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Report scheduling requires a timezone-aware datetime")
    if lookback_days < 0:
        raise ValueError("lookback_days must be nonnegative")
    zone = ZoneInfo(tz) if isinstance(tz, str) else tz
    local = now.astimezone(zone)
    weekly_end = local.replace(hour=20, minute=0, second=0, microsecond=0)
    weekly_end -= timedelta(days=(local.weekday() + 1) % 7)
    if weekly_end > local:
        weekly_end -= timedelta(days=7)
    monthly_end = local.replace(day=1, hour=8, minute=0, second=0, microsecond=0)
    if monthly_end > local:
        monthly_end = _previous_month(monthly_end)

    windows = []
    oldest = now.astimezone(UTC) - timedelta(days=lookback_days)
    for kind, end_local in (("weekly", weekly_end), ("monthly", monthly_end)):
        end = end_local.astimezone(UTC)
        if end < oldest:
            continue
        rolling_start = end - timedelta(days=30)
        if kind == "weekly":
            # Arithmetic in the local ZoneInfo timezone preserves Sunday 20:00
            # when the UTC offset changes during a report week.
            start_local = end_local - timedelta(days=7)
            start = start_local.astimezone(UTC)
            previous_start = (start_local - timedelta(days=7)).astimezone(UTC)
        else:
            start = rolling_start
            previous_start = start - timedelta(days=30)
        windows.append({
            "kind": kind,
            "key": f"{kind}:{end.isoformat(timespec='seconds')}",
            "start": int(start.timestamp()),
            "end": int(end.timestamp()),
            "previous_start": int(previous_start.timestamp()),
            "rolling_start": int(rolling_start.timestamp()),
            "timezone": getattr(zone, "key", str(zone)),
        })
    return windows


def _date_time(epoch, zone):
    value = datetime.fromtimestamp(epoch, UTC).astimezone(zone)
    hour = value.hour % 12 or 12
    period = "AM" if value.hour < 12 else "PM"
    return f"{MONTHS[value.month - 1]} {value.day}, {value.year}, {hour}:{value.minute:02d} {period} {value:%Z}"


def _chunks(lines):
    chunks, current = [], ""
    for line in lines:
        # Every line is self-contained HTML. Truck names are bounded before
        # escaping, so splitting never cuts an HTML entity or formatting tag.
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > TELEGRAM_REPORT_LIMIT:
            if current:
                chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def format_report(job, stats, coverage_complete=True):
    """Return escaped HTML Telegram messages, each at most 3900 raw chars.

    Counts describe recorded incidents, never severity scores. Optional values
    are omitted when unavailable. Ranking is limited to the first ten entries;
    the data layer is responsible for ordering those entries by incident count.
    """
    kind = job["kind"]
    if kind not in ("weekly", "monthly"):
        raise ValueError(f"Unsupported report kind: {kind}")
    zone = ZoneInfo(job.get("timezone", "America/New_York"))
    title = "Weekly" if kind == "weekly" else "Monthly"
    lines = [
        f"<b>📋 {title} Speeding Report</b>",
        f"{_date_time(job['start'], zone)} → {_date_time(job['end'], zone)}",
        "",
        f"Incidents: {int(stats['total'])} | Trucks: {int(stats['trucks'])}",
    ]
    if stats.get("fleet_count") is not None:
        lines.append(f"Active trucks: {int(stats['fleet_count'])}")
    if stats.get("rolling_total") is not None:
        lines.append(f"Past 30 days: {int(stats['rolling_total'])} incidents")
    if stats.get("previous_total") is not None:
        previous = int(stats["previous_total"])
        change = int(stats["total"]) - previous
        previous_label = "Previous week" if kind == "weekly" else "Previous 30 days"
        lines.append(f"{previous_label}: {previous} | Change: {change:+d}")
    ranking = stats.get("ranking") or []
    if int(stats["total"]) == 0:
        lines.extend(["", "No speeding incidents recorded in this period."])
    elif ranking:
        lines.extend(["", "<b>Top 10 trucks</b>"])
        for number, truck in enumerate(ranking[:10], 1):
            name = escape(" ".join(str(truck["truck_name"]).split())[:120])
            count = int(truck["count"])
            if kind == "weekly":
                detail = f"{count} this week"
                if truck.get("past30") is not None:
                    detail += f" | {int(truck['past30'])} past 30 days"
            else:
                detail = f"{count} incidents"
                if truck.get("previous_count") is not None:
                    detail += f" | previous 30d: {int(truck['previous_count'])}"
            lines.append(f"{number}. {name} — {detail}")
    if not coverage_complete:
        lines.extend(["", "Coverage: available recorded incidents; history incomplete."])
    return _chunks(lines)
