from flask import current_app
from sqlalchemy import select

from ..models import Alert, AlertTransition, Command, Inspection, METRICS, Robot, Telemetry, Zone, db, iso, utcnow


def telemetry_dict(row):
    if not row:
        return None
    return {"id": row.id, "event_id": row.event_id, "robot_id": row.robot_id, "zone_id": row.zone_id, "source": row.source, "recorded_at": iso(row.recorded_at), "received_at": iso(row.received_at), "uptime_ms": row.uptime_ms, "state": row.state, "battery_pct": row.battery_pct, "checkpoint_id": row.checkpoint_id, "telemetry": {key: getattr(row, key) for key in METRICS}}


def robot_dict(robot):
    now = utcnow()
    age = (now - robot.last_seen).total_seconds() if robot.last_seen else None
    sample_age = (now - robot.latest_recorded_at).total_seconds() if robot.latest_recorded_at else None
    latest = db.session.scalar(select(Telemetry).where(Telemetry.robot_id == robot.id).order_by(Telemetry.recorded_at.desc(), Telemetry.id.desc()).limit(1))
    return {"id": robot.id, "name": robot.name, "zone_id": robot.zone_id, "source": robot.source, "model": robot.model, "firmware": robot.firmware, "registered_at": iso(robot.registered_at), "last_seen": iso(robot.last_seen), "latest_recorded_at": iso(robot.latest_recorded_at), "online": age is not None and age <= current_app.config["OFFLINE_TIMEOUT_SECONDS"], "stale": sample_age is None or sample_age > current_app.config["STALE_TIMEOUT_SECONDS"], "state": robot.state, "battery_pct": robot.battery_pct, "uptime_ms": robot.uptime_ms, "checkpoint_id": robot.checkpoint_id, "latest": telemetry_dict(latest)}


def zone_dict(zone):
    latest = db.session.scalar(select(Telemetry).where(Telemetry.zone_id == zone.id).order_by(Telemetry.recorded_at.desc(), Telemetry.id.desc()).limit(1))
    last = db.session.scalar(select(Inspection).where(Inspection.zone_id == zone.id).order_by(Inspection.inspected_at.desc()).limit(1))
    day = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    checkpoints = db.session.scalar(select(db.func.count(db.distinct(Inspection.checkpoint_id))).where(Inspection.zone_id == zone.id, Inspection.inspected_at >= day))
    return {"id": zone.id, "field_id": zone.field_id, "name": zone.name, "crop": zone.crop, "area_ha": zone.area_ha, "map_col": zone.map_col, "map_row": zone.map_row, "checkpoint_count": zone.checkpoint_count, "inspected_checkpoints_today": checkpoints, "last_inspection": iso(last.inspected_at) if last else None, "robot_ids": list(db.session.scalars(select(Robot.id).where(Robot.zone_id == zone.id))), "active_alerts": db.session.scalar(select(db.func.count(Alert.id)).where(Alert.zone_id == zone.id, Alert.status != "resolved")), "stale": latest is None or (utcnow() - latest.recorded_at).total_seconds() > current_app.config["STALE_TIMEOUT_SECONDS"], "latest": telemetry_dict(latest)}


def alert_dict(alert, history=False):
    result = {key: getattr(alert, key) for key in ("id", "rule_id", "robot_id", "zone_id", "title", "detail", "severity", "status")}
    result.update({key: iso(getattr(alert, key)) for key in ("opened_at", "resolved_at", "acknowledged_at")})
    if history:
        result["history"] = [{"at": iso(t.at), "action": t.action, "actor": t.actor, "note": t.note} for t in db.session.scalars(select(AlertTransition).where(AlertTransition.alert_id == alert.id).order_by(AlertTransition.id))]
    return result


def command_dict(command):
    result = {key: getattr(command, key) for key in ("id", "robot_id", "action", "status", "note", "issued_by")}
    result.update({key: iso(getattr(command, key)) for key in ("issued_at", "expires_at", "completed_at")})
    return result
