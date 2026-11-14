from flask import current_app
from sqlalchemy import select

from ..models import ActiveCondition, Alert, AlertRule, AlertTransition, METRICS, changed, db, utcnow


def condition(robot, rule_id, active, recovered, title, detail, severity="warning", zone_id=None):
    key = f"{robot.id}:{zone_id or '-'}:{rule_id}"
    slot = db.session.get(ActiveCondition, key)
    if active and not slot:
        alert = Alert(condition_key=key, rule_id=rule_id, robot_id=robot.id, zone_id=zone_id, title=title, detail=detail, severity=severity)
        db.session.add(alert)
        db.session.flush()
        db.session.add(ActiveCondition(key=key, alert_id=alert.id))
        db.session.add(AlertTransition(alert_id=alert.id, action="opened", actor="rule-engine", note=detail))
        changed("alert")
    elif recovered and slot:
        alert = db.session.get(Alert, slot.alert_id)
        alert.status = "resolved"
        alert.resolved_at = utcnow()
        db.session.add(AlertTransition(alert_id=alert.id, action="resolved", actor="rule-engine", note=detail))
        db.session.delete(slot)
        changed("alert")


def evaluate_sensors(robot, record):
    # Historical backfill must not open or resolve current environmental conditions.
    if (utcnow() - record.recorded_at).total_seconds() > current_app.config["STALE_TIMEOUT_SECONDS"]:
        return
    for rule in db.session.scalars(select(AlertRule).where(AlertRule.enabled.is_(True))):
        value = getattr(record, rule.metric)
        if value is None:
            continue  # Missing is unknown, never proof of recovery.
        tested = abs(value) if rule.metric == "tilt_deg" else value
        below = rule.direction == "below"
        active = tested < rule.trigger if below else tested > rule.trigger
        recovered = tested >= rule.recovery if below else tested <= rule.recovery
        unit = METRICS[rule.metric]["unit"]
        detail = f"{METRICS[rule.metric]['label']}: {value:g} {unit}. Trigger {'<' if below else '>'} {rule.trigger:g}; recovery {'≥' if below else '≤'} {rule.recovery:g} {unit}."
        condition(robot, rule.id, active, recovered, rule.name, detail, rule.severity, record.zone_id)
    condition(robot, "robot-error", record.state == "error", record.state != "error", "Rover reports an error", f"Device-reported operational state: {record.state}.", "critical", record.zone_id)


def evaluate_presence(robot, now=None):
    now = now or utcnow()
    if not robot.last_seen:
        return  # Registered, awaiting first contact. No invented heartbeat history.
    heartbeat_age = (now - robot.last_seen).total_seconds()
    sample_age = (now - robot.latest_recorded_at).total_seconds() if robot.latest_recorded_at else float("inf")
    offline = heartbeat_age > current_app.config["OFFLINE_TIMEOUT_SECONDS"]
    stale = sample_age > current_app.config["STALE_TIMEOUT_SECONDS"]
    condition(robot, "rover-offline", offline, not offline, "Rover offline", f"No device contact for {int(heartbeat_age)} seconds.", "critical")
    condition(robot, "telemetry-stale", stale, not stale, "Telemetry stale", f"Newest device measurement is {int(sample_age)} seconds old.", "warning")


def retire_zone_conditions(robot, old_zone):
    # Assignment closes ownership-specific conditions without claiming sensor recovery.
    alerts = db.session.scalars(select(Alert).where(Alert.robot_id == robot.id, Alert.zone_id == old_zone, Alert.status != "resolved")).all()
    for alert in alerts:
        slot = db.session.get(ActiveCondition, alert.condition_key)
        if slot:
            db.session.delete(slot)
        alert.status, alert.resolved_at = "resolved", utcnow()
        db.session.add(AlertTransition(alert_id=alert.id, action="resolved", actor="operator", note="Zone assignment changed; condition retired without a recovery measurement."))
