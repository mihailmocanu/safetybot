from datetime import datetime, timezone
from html import escape
import math
import re
from zoneinfo import ZoneInfo

# API labels, not dashboard category names. The grouping is an integration mapping;
# Samsara does not return a risk-factor category on each Safety Event.
CATEGORIES = {
    "Harsh Driving": {"Acceleration", "Braking", "HarshTurn", "AggressiveDriving"},
    "Speeding": {"Speeding", "LightSpeeding", "ModerateSpeeding", "HeavySpeeding", "SevereSpeeding", "MaxSpeed"},
    "Traffic Signs & Signals": {"RollingStop", "RanRedLight", "DidNotYield", "EdgeRailroadCrossingViolation"},
    "Collision Risk": {"FollowingDistance", "FollowingDistanceModerate", "FollowingDistanceSevere", "GenericTailgating", "ForwardCollisionWarning", "NearCollison", "NearPedestrianCollision", "LaneDeparture", "LateResponse", "UnsafeManeuver", "VulnerableRoadUserCollisionWarning", "ProximityWarning", "RearCollisionWarning", "VehicleInBlindSpotWarning", "RolloverProtection", "YawControl"},
    "Policy Violations": {"NoSeatbelt", "ObstructedCamera", "Passenger", "Smoking", "PolicyViolationMask", "ProtectiveEquipment", "BluetoothHeadset", "HosViolation", "OtherViolation", "UnsafeParking", "Reversing", "UTurn", "LeftTurn", "ImproperEgress"},
    "Crash": {"Crash", "HarshImpact", "HighSpeedSuddenDisconnect"},
    "Distracted Driving": {"EdgeDistractedDriving", "GenericDistraction", "MobileUsage", "Drowsy", "Eating", "Drinking", "EatingDrinking"},
}
MONITORED = set().union(*CATEGORIES.values())
DISPLAY = {"Acceleration": "Harsh Acceleration", "Braking": "Harsh Braking", "HarshTurn": "Harsh Turn", "NearCollison": "Near Collision", "EdgeDistractedDriving": "Distracted Driving"}


def labels(event):
    return [x["label"] for x in event.get("behaviorLabels", []) if isinstance(x, dict) and x.get("label")]


def selected(event):
    return bool(MONITORED.intersection(labels(event)))


def parse_time(value):
    # Current v2 schema uses RFC3339 even though the field is named startMs.
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, timezone.utc)
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Event timestamp has no timezone")
    return result


def display_label(label):
    if label in CATEGORIES["Speeding"]:
        return "Speeding Alert"
    return DISPLAY.get(label, re.sub(r"(?<=[a-z])(?=[A-Z])", " ", label))


def clean(value, limit=200):
    return " ".join(str(value).split())[:limit]


def speeding_details(event):
    if not CATEGORIES["Speeding"].intersection(labels(event)):
        return []
    metadata = event.get("speedingMetadata") or {}

    def speed(key):
        value = metadata.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
            return value / 1.609344
        return None

    maximum = speed("maxSpeedKilometersPerHour")
    limit = speed("postedSpeedLimitKilometersPerHour")
    lines = []
    if maximum is not None:
        title = "Speed" if event.get("_source") == "live-speeding" and not event.get("_samsaraEventId") else "Max speed"
        lines.append(f"{title}: {maximum:.1f} mph")
    if limit is not None and limit > 0:
        lines.append(f"Posted limit: {limit:.1f} mph")
        if maximum is not None and maximum >= limit:
            lines.append(f"Over posted limit: {maximum - limit:.1f} mph")
    try:
        duration = (parse_time(event["endMs"]) - parse_time(event["startMs"])).total_seconds()
        if duration >= 0:
            lines.append(f"Duration: {duration:g} sec")
    except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
        pass
    return lines


def notification(event, company, tz, *, video=False):
    # HTML keeps the event link short. External text is escaped before formatting.
    # company remains an argument for compatibility with existing configuration.
    names = ", ".join(dict.fromkeys(display_label(x) for x in labels(event))) or "Safety Event"
    lines = [f"<b>⚠️ {escape(clean(names, 160))}</b>", ""]
    truck = (event.get("asset") or {}).get("name")
    if truck:
        lines.append(f"Truck: {escape(clean(truck, 80))}")
    driver = (event.get("driver") or {}).get("name")
    if not video and driver and clean(driver) != "0":
        lines.append(f"Driver: {escape(clean(driver, 80))}")
    if not video:
        lines.extend(speeding_details(event))
        count = event.get("_speeding_count_30d")
        if isinstance(count, int) and count > 0:
            lines.append(f"Speeding incidents in past 30 days: {count}")
    location = event.get("location") or {}
    address = location.get("address") or {}
    place = ", ".join(str(address[x]) for x in ("city", "state") if address.get(x))
    if not place:
        place = event.get("_location")
    if place and not video:
        lines.append(f"Location: {escape(clean(place, 120))}")
    if event.get("startMs") is not None:
        try:
            when = parse_time(event["startMs"]).astimezone(ZoneInfo(tz))
            clock = when.strftime("%I:%M %p %Z").lstrip("0")
            lines.append(f"Time: {when:%b} {when.day}, {when.year}, {clock}")
        except (ValueError, TypeError, AttributeError, OverflowError):
            pass
    link = event.get("inboxEventUrl") or event.get("incidentReportUrl")
    if isinstance(link, str) and link.startswith("https://"):
        lines.append(f'🔗 <a href="{escape(link, quote=True)}">Open in Samsara</a>')
    return "\n".join(lines)


def media_items(event):
    # One official event clip per input. Signed URLs can rotate: never use URL as ID.
    found = {}
    for item in event.get("media") or []:
        if item.get("input") and item.get("url"):
            found[item["input"]] = item
    return list(found.values())
