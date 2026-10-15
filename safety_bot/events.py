from datetime import datetime, timezone
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
    return DISPLAY.get(label, re.sub(r"(?<=[a-z])(?=[A-Z])", " ", label))


def clean(value, limit=200):
    return " ".join(str(value).split())[:limit]


def notification(event, company, tz):
    lines = ["🚨 SAMSARA SAFETY ALERT", "", f"🏢 Companie: {clean(company)}"]
    for obj, title in (("asset", "🚛 Truck"), ("driver", "👤 Driver")):
        value = event.get(obj) or {}
        if value.get("name"):
            lines.append(f"{title}: {clean(value['name'])}")
        elif value.get("id"):
            lines.append(f"{title} ID: {clean(value['id'])}")
    lines.append("")
    names = ", ".join(display_label(x) for x in labels(event))
    if names:
        lines.append(f"⚠️ Event: {clean(names, 600)}")
    if event.get("startMs") is not None:
        try:
            when = parse_time(event["startMs"]).astimezone(ZoneInfo(tz))
            lines.extend([f"📅 Date: {when:%m/%d/%Y}", f"🕒 Time: {when:%H:%M %Z}"])
        except (ValueError, TypeError, OverflowError):
            lines.append(f"📅 Timestamp Samsara: {clean(event['startMs'])}")
    location = event.get("location") or {}
    address = location.get("address") or {}
    place = ", ".join(str(address[x]) for x in ("city", "state") if address.get(x))
    if not place:
        place = " ".join(str(address[x]) for x in ("streetNumber", "street") if address.get(x))
    if not place and location.get("latitude") is not None and location.get("longitude") is not None:
        place = f"{location['latitude']}, {location['longitude']}"
    if place:
        lines.append(f"📍 Location: {clean(place)}")
    link = event.get("inboxEventUrl") or event.get("incidentReportUrl")
    if link and link.startswith("https://"):
        lines.append(f"🔗 Samsara: {clean(link, 1000)}")
    lines.extend([f"🆔 Event ID: {clean(event['id'])}", "", "🎥 Video: se atașează separat când este disponibil prin API."])
    return "\n".join(lines)


def media_items(event):
    # One official event clip per input. Signed URLs can rotate: never use URL as ID.
    found = {}
    for item in event.get("media") or []:
        if item.get("input") and item.get("url"):
            found[item["input"]] = item
    return list(found.values())
