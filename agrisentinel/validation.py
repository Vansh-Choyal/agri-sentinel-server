import math
import re
from datetime import datetime, timedelta, timezone

from .models import METRICS, utcnow

STATES = {"idle", "patrolling", "inspecting", "stopped", "error"}
ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,63}$")


class Invalid(ValueError):
    pass


def object_keys(value, allowed, required=(), path="payload"):
    if not isinstance(value, dict):
        raise Invalid(f"{path} must be an object")
    unknown = set(value) - set(allowed)
    missing = set(required) - set(value)
    if unknown:
        raise Invalid(f"Unknown {path} fields: {', '.join(sorted(unknown))}")
    if missing:
        raise Invalid(f"Missing {path} fields: {', '.join(sorted(missing))}")


def identifier(value, name):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise Invalid(f"{name} must be 1–64 letters, digits, dots, dashes, colons or underscores")
    return value


def number(value, name, lower, upper, integer=False, nullable=True):
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or (isinstance(value, float) and not math.isfinite(value)):
        raise Invalid(f"{name} must be a finite {'integer' if integer else 'number'}")
    if integer and not isinstance(value, int):
        raise Invalid(f"{name} must be an integer")
    if not lower <= value <= upper:
        raise Invalid(f"{name} must be between {lower} and {upper}")
    return value


def timestamp(value, name="recorded_at"):
    if not isinstance(value, str):
        raise Invalid(f"{name} must be an ISO 8601 timestamp with timezone")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise Invalid(f"{name} must be an ISO 8601 timestamp with timezone") from None
    if result.tzinfo is None:
        raise Invalid(f"{name} must include a UTC offset or Z")
    return result.astimezone(timezone.utc)


def validate_telemetry(body):
    fields = {"schema_version", "event_id", "robot_id", "zone_id", "recorded_at", "uptime_ms", "telemetry", "robot"}
    object_keys(body, fields, fields)
    if type(body["schema_version"]) is not int or body["schema_version"] != 1:
        raise Invalid("schema_version must be integer 1")
    for key in ("robot_id", "zone_id"):
        identifier(body[key], key)
    if not isinstance(body["event_id"], str) or not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,128}", body["event_id"]):
        raise Invalid("event_id must be 1–128 identifier characters")
    at = timestamp(body["recorded_at"])
    if at > utcnow() + timedelta(seconds=120):
        raise Invalid("recorded_at is more than 120 seconds in the future")
    if at < utcnow() - timedelta(days=30):
        raise Invalid("recorded_at is older than the 30-day ingestion window")
    number(body["uptime_ms"], "uptime_ms", 0, 2**63 - 1, integer=True, nullable=False)
    sensors = body["telemetry"]
    object_keys(sensors, METRICS, path="telemetry")
    if not sensors:
        raise Invalid("telemetry must include at least one sensor key; use null for unavailable readings")
    for key, value in sensors.items():
        meta = METRICS[key]
        number(value, key, meta["min"], meta["max"], integer=key.endswith("_raw"))
    robot = body["robot"]
    object_keys(robot, {"status", "battery_pct", "checkpoint_id"}, {"status"}, "robot")
    if not isinstance(robot["status"], str) or robot["status"] not in STATES:
        raise Invalid(f"robot.status must be one of {', '.join(sorted(STATES))}")
    number(robot.get("battery_pct"), "battery_pct", 0, 100)
    if robot.get("checkpoint_id") is not None:
        identifier(robot["checkpoint_id"], "checkpoint_id")
    return at
