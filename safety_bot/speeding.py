"""Parse current vehicle speed readings without Samsara severity thresholds."""

from datetime import datetime, timezone
import math

from .events import CATEGORIES, MONITORED


def _timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def _number(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def speed_samples(readings, now=None, max_age_seconds=120):
    """Return fresh speed/posted-limit pairs from flat Readings latest data.

    Both official ``samsaraSpeed`` and ``samsaraSpeedLimit`` readings use m/s.
    Their raw numeric values determine ``over``; rounding is only for display.
    Unknown or stale data produces no sample, so it must not close an ongoing
    speeding episode. In particular, a posted limit is never trusted for more
    than 120 seconds, even if the caller allows an older speed sample. This
    conservative policy avoids comparing current speed with an old road's limit.

    ``now`` may be an aware datetime or epoch seconds. Readings must have aware
    RFC3339 timestamps. Up to 30 seconds of clock skew into the future is allowed.
    The optional ``location`` reading contributes only a fresh string value.
    """
    if not _number(max_age_seconds) or max_age_seconds < 0:
        raise ValueError("max_age_seconds must be a finite nonnegative number")
    if now is None:
        current = datetime.now(timezone.utc).timestamp()
    elif isinstance(now, datetime):
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must have a timezone")
        current = now.timestamp()
    elif _number(now):
        current = float(now)
    else:
        raise ValueError("now must be an aware datetime or finite epoch seconds")

    latest = {}
    for reading in readings or []:
        if not isinstance(reading, dict):
            continue
        asset_id = reading.get("entityId")
        reading_id = reading.get("readingId")
        if not isinstance(asset_id, str) or not asset_id:
            continue
        if reading_id not in ("samsaraSpeed", "samsaraSpeedLimit", "location"):
            continue
        when = _timestamp(reading.get("happenedAtTime"))
        if when is None:
            continue
        previous = latest.setdefault(asset_id, {}).get(reading_id)
        if previous is None or when > previous[0]:
            latest[asset_id][reading_id] = (when, reading.get("value"))

    def fresh(reading, age):
        return reading is not None and -30 <= current - reading[0].timestamp() <= age

    samples = []
    for asset_id, values in latest.items():
        speed = values.get("samsaraSpeed")
        limit = values.get("samsaraSpeedLimit")
        if not fresh(speed, max_age_seconds) or not fresh(limit, min(max_age_seconds, 120)):
            continue
        if not _number(speed[1]) or not _number(limit[1]) or speed[1] < 0 or limit[1] <= 0:
            continue
        speed_kph, limit_kph = speed[1] * 3.6, limit[1] * 3.6
        if not math.isfinite(speed_kph) or not math.isfinite(limit_kph):
            continue
        sample = {
            "asset_id": asset_id,
            "time": speed[0].isoformat().replace("+00:00", "Z"),
            "timestamp": speed[0].timestamp(),
            "speed_kph": speed_kph,
            "limit_kph": limit_kph,
            "over": speed[1] > limit[1],
        }
        location = values.get("location")
        if fresh(location, min(max_age_seconds, 120)) and isinstance(location[1], str) and location[1].strip():
            sample["location"] = location[1].strip()
        samples.append(sample)
    return samples


def speeding_only(event):
    """Identify Safety Events covered by live speeding, retaining mixed events."""
    if not isinstance(event, dict):
        return False
    behaviors = event.get("behaviorLabels")
    if not isinstance(behaviors, list):
        return False
    labels = {
        item["label"] for item in behaviors
        if isinstance(item, dict) and isinstance(item.get("label"), str)
    }
    speeding = CATEGORIES["Speeding"]
    return bool(labels.intersection(speeding)) and not labels.intersection(MONITORED - speeding)
