"""Portable SQLAlchemy schema. Every stored timestamp is UTC."""
from datetime import datetime, timezone

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.types import DateTime, TypeDecorator

db = SQLAlchemy()


def utcnow():
    return datetime.now(timezone.utc)


def iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if value else None


class UTCDateTime(TypeDecorator):
    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Timestamp must include a timezone")
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        return value.replace(tzinfo=timezone.utc) if value else None


@event.listens_for(Engine, "connect")
def sqlite_settings(connection, _):
    if connection.__class__.__module__.startswith("sqlite3"):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()


class Setting(db.Model):
    key = db.Column(db.String(80), primary_key=True)
    value = db.Column(db.JSON, nullable=False)


class Field(db.Model):
    id = db.Column(db.String(64), primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    description = db.Column(db.String(300))
    area_ha = db.Column(db.Float)


class Zone(db.Model):
    id = db.Column(db.String(64), primary_key=True)
    field_id = db.Column(db.ForeignKey("field.id"), nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    crop = db.Column(db.String(80))
    area_ha = db.Column(db.Float)
    map_col = db.Column(db.Integer, nullable=False)
    map_row = db.Column(db.Integer, nullable=False)
    checkpoint_count = db.Column(db.Integer, nullable=False, default=8)


class Robot(db.Model):
    id = db.Column(db.String(64), primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    zone_id = db.Column(db.ForeignKey("zone.id"), index=True)
    api_key_hash = db.Column(db.String(64), nullable=False)
    source = db.Column(db.String(16), nullable=False)  # hardware or simulator; server-owned
    model = db.Column(db.String(120), default="ESP32 rover")
    firmware = db.Column(db.String(80), default="unreported")
    registered_at = db.Column(UTCDateTime, nullable=False, default=utcnow)
    last_seen = db.Column(UTCDateTime)
    latest_recorded_at = db.Column(UTCDateTime)
    state = db.Column(db.String(24), nullable=False, default="idle")
    uptime_ms = db.Column(db.BigInteger)
    battery_pct = db.Column(db.Float)
    checkpoint_id = db.Column(db.String(64))


class CameraFrame(db.Model):
    """One bounded latest JPEG per rover; no recording archive."""
    robot_id = db.Column(db.ForeignKey("robot.id"), primary_key=True)
    jpeg = db.Column(db.LargeBinary, nullable=False)
    version = db.Column(db.String(32), nullable=False)
    received_at = db.Column(UTCDateTime, nullable=False, default=utcnow)
    width = db.Column(db.Integer, nullable=False)
    height = db.Column(db.Integer, nullable=False)


METRICS = {
    "temperature_c": {"label": "Temperature", "unit": "°C", "min": -40, "max": 85},
    "humidity_pct": {"label": "Humidity", "unit": "%", "min": 0, "max": 100},
    "soil_moisture_pct": {"label": "Soil moisture", "unit": "%", "min": 0, "max": 100},
    "mq2_raw": {"label": "MQ2 raw", "unit": "ADC", "min": 0, "max": 4095},
    "mq3_raw": {"label": "MQ3 raw", "unit": "ADC", "min": 0, "max": 4095},
    "heading_deg": {"label": "Compass heading", "unit": "°", "min": 0, "max": 359.999},
    "tilt_deg": {"label": "Tilt", "unit": "°", "min": -180, "max": 180},
    "sound_level_raw": {"label": "Sound level raw", "unit": "ADC", "min": 0, "max": 4095},
    "obstacle_distance_cm": {"label": "Obstacle distance", "unit": "cm", "min": 0, "max": 1000},
}


class Telemetry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.String(128), unique=True, nullable=False)
    payload_hash = db.Column(db.String(64), nullable=False)
    robot_id = db.Column(db.ForeignKey("robot.id"), nullable=False)
    zone_id = db.Column(db.ForeignKey("zone.id"), nullable=False)
    source = db.Column(db.String(16), nullable=False)
    schema_version = db.Column(db.Integer, nullable=False, default=1)
    recorded_at = db.Column(UTCDateTime, nullable=False)
    received_at = db.Column(UTCDateTime, nullable=False, default=utcnow)
    uptime_ms = db.Column(db.BigInteger, nullable=False)
    state = db.Column(db.String(24), nullable=False)
    checkpoint_id = db.Column(db.String(64))
    battery_pct = db.Column(db.Float)
    temperature_c = db.Column(db.Float)
    humidity_pct = db.Column(db.Float)
    soil_moisture_pct = db.Column(db.Float)
    mq2_raw = db.Column(db.Integer)
    mq3_raw = db.Column(db.Integer)
    heading_deg = db.Column(db.Float)
    tilt_deg = db.Column(db.Float)
    sound_level_raw = db.Column(db.Integer)
    obstacle_distance_cm = db.Column(db.Float)
    __table_args__ = (
        db.Index("ix_telemetry_robot_time", "robot_id", "recorded_at"),
        db.Index("ix_telemetry_zone_time", "zone_id", "recorded_at"),
        db.Index("ix_telemetry_time", "recorded_at"),
    )


class AlertRule(db.Model):
    id = db.Column(db.String(64), primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    metric = db.Column(db.String(64), nullable=False)
    direction = db.Column(db.String(8), nullable=False)
    trigger = db.Column(db.Float, nullable=False)
    recovery = db.Column(db.Float, nullable=False)
    severity = db.Column(db.String(16), nullable=False)
    enabled = db.Column(db.Boolean, nullable=False, default=True)


class Alert(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    condition_key = db.Column(db.String(200), nullable=False, index=True)
    rule_id = db.Column(db.String(64), nullable=False)
    robot_id = db.Column(db.ForeignKey("robot.id"), nullable=False, index=True)
    zone_id = db.Column(db.ForeignKey("zone.id"), index=True)
    title = db.Column(db.String(160), nullable=False)
    detail = db.Column(db.String(400), nullable=False)
    severity = db.Column(db.String(16), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="active", index=True)
    opened_at = db.Column(UTCDateTime, nullable=False, default=utcnow, index=True)
    resolved_at = db.Column(UTCDateTime)
    acknowledged_at = db.Column(UTCDateTime)


class ActiveCondition(db.Model):
    # Portable uniqueness guarantee: only one open alert per condition, including PG.
    key = db.Column(db.String(200), primary_key=True)
    alert_id = db.Column(db.ForeignKey("alert.id"), nullable=False, unique=True)


class AlertTransition(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    alert_id = db.Column(db.ForeignKey("alert.id"), nullable=False, index=True)
    at = db.Column(UTCDateTime, nullable=False, default=utcnow)
    action = db.Column(db.String(24), nullable=False)
    actor = db.Column(db.String(80), nullable=False)
    note = db.Column(db.String(400), nullable=False)


class Command(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    robot_id = db.Column(db.ForeignKey("robot.id"), nullable=False, index=True)
    action = db.Column(db.String(32), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="pending")
    issued_at = db.Column(UTCDateTime, nullable=False, default=utcnow)
    expires_at = db.Column(UTCDateTime, nullable=False)
    completed_at = db.Column(UTCDateTime)
    issued_by = db.Column(db.String(80), nullable=False)
    note = db.Column(db.String(300))


class Inspection(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    telemetry_id = db.Column(db.ForeignKey("telemetry.id"), nullable=False, unique=True)
    robot_id = db.Column(db.ForeignKey("robot.id"), nullable=False)
    zone_id = db.Column(db.ForeignKey("zone.id"), nullable=False)
    checkpoint_id = db.Column(db.String(64), nullable=False)
    inspected_at = db.Column(UTCDateTime, nullable=False, index=True)
    __table_args__ = (db.Index("ix_inspection_zone_time", "zone_id", "inspected_at"),)


class ChangeEvent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(24), nullable=False)
    at = db.Column(UTCDateTime, nullable=False, default=utcnow)


def changed(kind):
    db.session.add(ChangeEvent(kind=kind))
