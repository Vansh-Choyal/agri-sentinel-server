import csv
import hashlib
import io
import json
import secrets
import threading
import time
from datetime import timedelta

from flask import Blueprint, Response, current_app, jsonify, request, session, stream_with_context
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from werkzeug.exceptions import BadRequest, UnsupportedMediaType

from .auth import device_from_request, key_hash, operator_required
from .models import ActiveCondition, Alert, AlertRule, AlertTransition, ChangeEvent, Command, Field, Inspection, METRICS, Robot, Telemetry, Zone, changed, db, iso, utcnow
from .services.alerts import evaluate_presence, evaluate_sensors, retire_zone_conditions
from .services.analytics import aggregate, period_bounds
from .services.serialization import alert_dict, command_dict, robot_dict, telemetry_dict, zone_dict
from .validation import Invalid, identifier, number, object_keys, validate_telemetry

api = Blueprint("api", __name__)
_streams = threading.BoundedSemaphore(12)


def fail(code, message, status):
    return jsonify(error=code, message=message), status


def body_json():
    try:
        return request.get_json()
    except (BadRequest, UnsupportedMediaType):
        raise Invalid("Send a valid JSON object with Content-Type: application/json") from None


def sqlite_write_lock():
    if db.engine.dialect.name == "sqlite":
        db.session.execute(text("BEGIN IMMEDIATE"))


@api.errorhandler(Invalid)
def invalid(error):
    db.session.rollback()
    return fail("validation_error", str(error), 422)


@api.get("/health")
def health():
    db.session.execute(text("SELECT 1"))
    return jsonify(status="ok", schema_version=1, server_time=iso(utcnow()))


@api.post("/telemetry")
def ingest():
    if not request.is_json:
        return fail("unsupported_media_type", "Content-Type must be application/json", 415)
    body = body_json()
    at = validate_telemetry(body)
    sqlite_write_lock()
    robot = device_from_request(body["robot_id"])
    if not robot:
        db.session.rollback()
        return fail("invalid_device_key", "Valid per-rover Bearer API key required", 401)
    robot = db.session.scalar(select(Robot).where(Robot.id == robot.id).with_for_update())
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    existing = db.session.scalar(select(Telemetry).where(Telemetry.event_id == body["event_id"]))
    if existing:
        if existing.robot_id != robot.id or existing.payload_hash != digest:
            db.session.rollback()
            return fail("event_conflict", "event_id already exists with different content", 409)
        result = jsonify(status="duplicate", telemetry_id=existing.id, event_id=existing.event_id)
        db.session.rollback()
        return result, 200
    if robot.zone_id != body["zone_id"]:
        db.session.rollback()
        return fail("zone_not_assigned", "Rover may report only to its currently assigned zone", 403)
    if robot.source == "demo":
        db.session.rollback()
        return fail("demo_device", "Built-in demo rovers do not accept device telemetry", 403)
    zone = db.session.get(Zone, body["zone_id"])
    checkpoint = body["robot"].get("checkpoint_id")
    if checkpoint is not None and checkpoint not in {f"cp-{i:02d}" for i in range(1, zone.checkpoint_count + 1)}:
        raise Invalid(f"checkpoint_id must be cp-01 through cp-{zone.checkpoint_count:02d} for this zone")
    row = Telemetry(event_id=body["event_id"], payload_hash=digest, robot_id=robot.id, zone_id=robot.zone_id, source=robot.source, recorded_at=at, uptime_ms=body["uptime_ms"], state=body["robot"]["status"], battery_pct=body["robot"].get("battery_pct"), checkpoint_id=checkpoint, **body["telemetry"])
    db.session.add(row)
    try:
        db.session.flush()
        newer = robot.latest_recorded_at is None or at > robot.latest_recorded_at
        robot.last_seen = utcnow()
        if newer:
            robot.latest_recorded_at, robot.state = at, row.state
            robot.uptime_ms, robot.battery_pct, robot.checkpoint_id = row.uptime_ms, row.battery_pct, checkpoint
            evaluate_sensors(robot, row)
        if row.state == "inspecting" and checkpoint:
            previous = db.session.scalar(select(Inspection).where(Inspection.robot_id == robot.id, Inspection.zone_id == row.zone_id).order_by(Inspection.inspected_at.desc()).limit(1))
            if not previous or previous.checkpoint_id != checkpoint or (at - previous.inspected_at).total_seconds() >= 300:
                db.session.add(Inspection(telemetry_id=row.id, robot_id=robot.id, zone_id=row.zone_id, checkpoint_id=checkpoint, inspected_at=at))
        evaluate_presence(robot)
        changed("telemetry")
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        existing = db.session.scalar(select(Telemetry).where(Telemetry.event_id == body["event_id"]))
        if existing and existing.robot_id == robot.id and existing.payload_hash == digest:
            return jsonify(status="duplicate", telemetry_id=existing.id, event_id=existing.event_id), 200
        if existing:
            return fail("event_conflict", "event_id already exists with different content", 409)
        raise
    current_app.logger.info("Telemetry accepted robot=%s source=%s event=%s", robot.id, robot.source, row.event_id)
    return jsonify(status="accepted", telemetry_id=row.id, event_id=row.event_id, source=row.source), 201


@api.get("/overview")
@operator_required
def overview():
    from .services.demo import profile
    robots = [robot_dict(r) for r in db.session.scalars(select(Robot).order_by(Robot.id).limit(200))]
    zones = [zone_dict(z) for z in db.session.scalars(select(Zone).order_by(Zone.map_row, Zone.map_col).limit(200))]
    latest = db.session.scalar(select(Telemetry).order_by(Telemetry.recorded_at.desc(), Telemetry.id.desc()).limit(1))
    alerts = list(db.session.scalars(select(Alert).where(Alert.status != "resolved").order_by(Alert.opened_at.desc()).limit(8)))
    return jsonify(demo=profile(), server_time=iso(utcnow()), fields=[{"id": f.id, "name": f.name, "area_ha": f.area_ha, "description": f.description} for f in db.session.scalars(select(Field))], counts={"total": len(robots), "online": sum(r["online"] for r in robots), "offline": sum(not r["online"] for r in robots), "idle": sum(r["online"] and r["state"] == "idle" for r in robots), "zones": len(zones), "active_alerts": db.session.scalar(select(db.func.count(Alert.id)).where(Alert.status != "resolved")), "telemetry": db.session.scalar(select(db.func.count(Telemetry.id)))}, robots=robots, zones=zones, latest=telemetry_dict(latest), latest_stale=latest is None or (utcnow() - latest.recorded_at).total_seconds() > current_app.config["STALE_TIMEOUT_SECONDS"], alerts=[alert_dict(a) for a in alerts], activity=[telemetry_dict(t) for t in db.session.scalars(select(Telemetry).order_by(Telemetry.received_at.desc(), Telemetry.id.desc()).limit(8))], timeouts={"offline": current_app.config["OFFLINE_TIMEOUT_SECONDS"], "stale": current_app.config["STALE_TIMEOUT_SECONDS"]})


@api.get("/robots")
@operator_required
def robots():
    return jsonify(items=[robot_dict(r) for r in db.session.scalars(select(Robot).order_by(Robot.id).limit(200))])


@api.post("/robots")
@operator_required
def register_robot():
    body = body_json()
    object_keys(body, {"id", "name", "zone_id", "source", "model", "firmware"}, {"id", "name", "source"})
    identifier(body["id"], "id")
    if not isinstance(body["name"], str) or not 1 <= len(body["name"].strip()) <= 120:
        raise Invalid("name must contain 1–120 characters")
    if body["source"] not in ("hardware", "simulator"):
        raise Invalid("source must be hardware or simulator")
    for key, maximum in (("model", 120), ("firmware", 80)):
        if key in body and (not isinstance(body[key], str) or len(body[key]) > maximum):
            raise Invalid(f"{key} must be a string of at most {maximum} characters")
    sqlite_write_lock()
    if db.session.get(Robot, body["id"]):
        return fail("robot_exists", "Rover ID is already registered", 409)
    if body.get("zone_id") is not None:
        identifier(body["zone_id"], "zone_id")
        if not db.session.get(Zone, body["zone_id"]):
            raise Invalid("Unknown zone_id")
    if db.session.scalar(select(db.func.count(Robot.id))) >= 200:
        raise Invalid("This installation supports up to 200 registered rovers")
    key = secrets.token_urlsafe(32)
    robot = Robot(**body, api_key_hash=key_hash(key))
    db.session.add(robot)
    changed("fleet")
    db.session.commit()
    return jsonify(robot=robot_dict(robot), api_key=key, message="Save this key now. It will not be returned again."), 201


@api.get("/robots/<robot_id>")
@operator_required
def robot_detail(robot_id):
    robot = db.session.get(Robot, robot_id)
    if not robot:
        return fail("not_found", "Rover not found", 404)
    return jsonify(robot=robot_dict(robot), commands=[command_dict(c) for c in db.session.scalars(select(Command).where(Command.robot_id == robot_id).order_by(Command.id.desc()).limit(30))], inspections=[{"zone_id": i.zone_id, "checkpoint_id": i.checkpoint_id, "at": iso(i.inspected_at)} for i in db.session.scalars(select(Inspection).where(Inspection.robot_id == robot_id).order_by(Inspection.inspected_at.desc()).limit(30))])


@api.patch("/robots/<robot_id>")
@operator_required
def assign_robot(robot_id):
    body = body_json()
    object_keys(body, {"zone_id"}, {"zone_id"})
    sqlite_write_lock()
    robot = db.session.scalar(select(Robot).where(Robot.id == robot_id).with_for_update())
    if not robot:
        return fail("not_found", "Rover not found", 404)
    if body["zone_id"] is not None:
        identifier(body["zone_id"], "zone_id")
        if not db.session.get(Zone, body["zone_id"]):
            raise Invalid("Unknown zone_id")
    if robot.zone_id != body["zone_id"]:
        retire_zone_conditions(robot, robot.zone_id)
        robot.zone_id, robot.checkpoint_id = body["zone_id"], None
        # Commands for the previous assignment must not execute in the new zone.
        for command in db.session.scalars(select(Command).where(Command.robot_id == robot_id, Command.status == "pending")):
            command.status = "cancelled"
        changed("fleet")
    db.session.commit()
    return jsonify(robot=robot_dict(robot))


@api.get("/zones")
@operator_required
def zones():
    return jsonify(items=[zone_dict(z) for z in db.session.scalars(select(Zone).order_by(Zone.id).limit(200))])


@api.get("/zones/<zone_id>")
@operator_required
def zone_detail(zone_id):
    zone = db.session.get(Zone, zone_id)
    if not zone:
        return fail("not_found", "Zone not found", 404)
    return jsonify(zone=zone_dict(zone))


def integer_arg(name, default, minimum, maximum):
    try:
        value = int(request.args.get(name, default))
    except (ValueError, TypeError):
        raise Invalid(f"{name} must be an integer") from None
    if not minimum <= value <= maximum:
        raise Invalid(f"{name} must be between {minimum} and {maximum}")
    return value


@api.get("/telemetry")
@operator_required
def history():
    limit = integer_arg("limit", 50, 1, 500)
    cursor = integer_arg("before_id", 2**63 - 1, 1, 2**63 - 1)
    start, end, _ = period_bounds(request.args)
    query = select(Telemetry).where(Telemetry.id < cursor, Telemetry.recorded_at >= start, Telemetry.recorded_at < end)
    if request.args.get("robot_id"):
        query = query.where(Telemetry.robot_id == request.args["robot_id"])
    if request.args.get("zone_id"):
        query = query.where(Telemetry.zone_id == request.args["zone_id"])
    if request.args.get("zones"):
        selected = request.args["zones"].split(",")
        if len(selected) > 12:
            raise Invalid("Select no more than 12 zones")
        query = query.where(Telemetry.zone_id.in_(selected))
    rows = list(db.session.scalars(query.order_by(Telemetry.id.desc()).limit(limit + 1)))
    return jsonify(items=[telemetry_dict(t) for t in rows[:limit]], next_cursor=rows[limit - 1].id if len(rows) > limit else None, **{"from": iso(start), "to": iso(end)})


@api.get("/analytics")
@operator_required
def analytics():
    return jsonify(aggregate(request.args))


@api.get("/alerts")
@operator_required
def alerts():
    limit = integer_arg("limit", 50, 1, 100)
    cursor = integer_arg("before_id", 2**63 - 1, 1, 2**63 - 1)
    query = select(Alert).where(Alert.id < cursor)
    status = request.args.get("status", "open")
    if status == "open":
        query = query.where(Alert.status != "resolved")
    elif status in ("active", "acknowledged", "resolved"):
        query = query.where(Alert.status == status)
    elif status != "all":
        raise Invalid("Invalid alert status")
    if request.args.get("zone_id"):
        query = query.where(Alert.zone_id == request.args["zone_id"])
    rows = list(db.session.scalars(query.order_by(Alert.id.desc()).limit(limit + 1)))
    return jsonify(items=[alert_dict(a, True) for a in rows[:limit]], next_cursor=rows[limit - 1].id if len(rows) > limit else None)


@api.post("/alerts/<int:alert_id>/acknowledge")
@operator_required
def acknowledge(alert_id):
    sqlite_write_lock()
    alert = db.session.get(Alert, alert_id)
    if not alert:
        return fail("not_found", "Alert not found", 404)
    if alert.status == "active":
        alert.status, alert.acknowledged_at = "acknowledged", utcnow()
        db.session.add(AlertTransition(alert_id=alert.id, action="acknowledged", actor=session["operator"], note="Operator acknowledged; waiting for measured recovery."))
        changed("alert")
        db.session.commit()
    return jsonify(alert=alert_dict(alert, True))


@api.get("/rules")
@operator_required
def rules():
    return jsonify(items=[{key: getattr(r, key) for key in ("id", "name", "metric", "direction", "trigger", "recovery", "severity", "enabled")} for r in db.session.scalars(select(AlertRule).order_by(AlertRule.id))])


@api.patch("/rules/<rule_id>")
@operator_required
def update_rule(rule_id):
    body = body_json()
    object_keys(body, {"trigger", "recovery", "enabled", "severity"})
    sqlite_write_lock()
    rule = db.session.get(AlertRule, rule_id)
    if not rule:
        return fail("not_found", "Rule not found", 404)
    meta = METRICS[rule.metric]
    for key in ("trigger", "recovery"):
        if key in body:
            number(body[key], key, meta["min"], meta["max"], nullable=False)
    trigger, recovery = body.get("trigger", rule.trigger), body.get("recovery", rule.recovery)
    if (rule.direction == "below" and recovery <= trigger) or (rule.direction == "above" and recovery >= trigger):
        raise Invalid("Recovery must be beyond the trigger in the safe direction")
    if "enabled" in body and type(body["enabled"]) is not bool:
        raise Invalid("enabled must be boolean")
    if "severity" in body and body["severity"] not in ("warning", "critical"):
        raise Invalid("severity must be warning or critical")
    for key, value in body.items():
        setattr(rule, key, value)
    if not rule.enabled:
        for alert in db.session.scalars(select(Alert).where(Alert.rule_id == rule_id, Alert.status != "resolved")):
            slot = db.session.get(ActiveCondition, alert.condition_key)
            if slot:
                db.session.delete(slot)
            alert.status, alert.resolved_at = "resolved", utcnow()
            db.session.add(AlertTransition(alert_id=alert.id, action="resolved", actor=session["operator"], note="Rule disabled by operator; no sensor recovery implied."))
    changed("rule")
    db.session.commit()
    return jsonify(status="updated")


@api.post("/robots/<robot_id>/commands")
@operator_required
def issue_command(robot_id):
    body = body_json()
    object_keys(body, {"action"}, {"action"})
    if body["action"] not in ("patrol", "inspect", "stop", "idle"):
        raise Invalid("Command action must be patrol, inspect, stop, or idle")
    sqlite_write_lock()
    if not db.session.get(Robot, robot_id):
        return fail("not_found", "Rover not found", 404)
    if db.session.scalar(select(db.func.count(Command.id)).where(Command.robot_id == robot_id, Command.status == "pending", Command.expires_at > utcnow())) >= 10:
        return fail("queue_full", "Rover has ten pending commands", 409)
    command = Command(robot_id=robot_id, action=body["action"], issued_by=session["operator"], expires_at=utcnow() + timedelta(minutes=5))
    db.session.add(command)
    changed("command")
    db.session.commit()
    return jsonify(command=command_dict(command)), 202


@api.get("/robots/<robot_id>/commands")
def poll_commands(robot_id):
    robot = device_from_request(robot_id)
    if not robot:
        return fail("invalid_device_key", "Valid per-rover Bearer API key required", 401)
    return jsonify(zone_id=robot.zone_id, commands=[command_dict(c) for c in db.session.scalars(select(Command).where(Command.robot_id == robot_id, Command.status == "pending", Command.expires_at > utcnow()).order_by(Command.id).limit(10))])


@api.post("/robots/<robot_id>/commands/<int:command_id>/ack")
def ack_command(robot_id, command_id):
    sqlite_write_lock()
    robot = device_from_request(robot_id)
    if not robot:
        return fail("invalid_device_key", "Valid per-rover Bearer API key required", 401)
    body = body_json()
    object_keys(body, {"status", "note"}, {"status"})
    if body["status"] not in ("completed", "failed") or not isinstance(body.get("note", ""), str) or len(body.get("note", "")) > 300:
        raise Invalid("status must be completed or failed; note at most 300 characters")
    command = db.session.get(Command, command_id)
    if not command or command.robot_id != robot_id:
        return fail("not_found", "Command not found", 404)
    if command.status == body["status"]:
        return jsonify(command=command_dict(command))
    if command.status != "pending" or command.expires_at < utcnow():
        return fail("command_expired", "Command is no longer pending", 409)
    command.status, command.completed_at, command.note = body["status"], utcnow(), body.get("note")
    changed("command")
    db.session.commit()
    return jsonify(command=command_dict(command))


@api.get("/reports")
@operator_required
def reports():
    return jsonify(aggregate(request.args, report=True))


def csv_cell(value):
    # Protect downloaded spreadsheets from formula injection in operator-set names.
    return "'" + value if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")) else value


@api.get("/reports/export.csv")
@operator_required
def export_report():
    report = aggregate(request.args, report=True)
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    def write(*cells):
        writer.writerow([csv_cell(cell) for cell in cells])
    write("AgriSentinel measurement report", report["period"], report["from"], report["to"])
    write("Data sources", ", ".join(report["sources"]))
    write("Farm scenario", report.get("demo", {}).get("scenario", "") if report.get("demo") else "")
    write("Basis", "Stored telemetry; missing values excluded; heading average is circular; activity counts are samples, not operating duration.")
    write("Partition", "Crop", "Area ha")
    for zone in report["zones"]:
        write(zone["name"], zone["crop"], zone["area_ha"])
    write("Scope", "Metric", "Unit", "Samples", "Minimum", "Maximum", "Average")
    for scope, stats in [("Fleet", report["statistics"])] + [(zone["name"], zone["statistics"]) for zone in report["zones"]]:
        for metric, stat in stats.items():
            write(scope, METRICS[metric]["label"], METRICS[metric]["unit"], stat["count"], stat["min"], stat["max"], stat["avg"])
    write("Rover", "Source", "State", "Samples")
    for robot in report["robots"]:
        for state, count in robot["state_samples"].items():
            write(robot["id"], ", ".join(robot["sources"]), state, count)
    write("Environmental trends", "Time-bucket averages of received measurements")
    write("Bucket UTC", "Zone", "Record count", "Temperature C", "Humidity %", "Soil moisture %")
    for point in report["points"]:
        if point["count"]:
            write(point["at"], point["zone_id"], point["count"], point["temperature_c"], point["humidity_pct"], point["soil_moisture_pct"])
    write("Alerts overlapping period", report["alert_count"], "Detail rows capped at 200")
    write("Alert ID", "Title", "Rover", "Zone", "Severity", "Status", "Opened UTC", "Resolved UTC")
    for alert in report["alerts"]:
        write(alert["id"], alert["title"], alert["robot_id"], alert["zone_id"], alert["severity"], alert["status"], alert["opened_at"], alert["resolved_at"])
    write("Inspections", report["inspection_count"], "Detail rows capped at 100")
    write("Rover", "Zone", "Checkpoint", "Inspected UTC")
    for inspection in report["inspections"]:
        write(inspection["robot_id"], inspection["zone_id"], inspection["checkpoint_id"], inspection["at"])
    return Response(output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename=agrisentinel-{report['period']}-{utcnow():%Y%m%d}.csv"})


@api.get("/events")
@operator_required
def events():
    if not _streams.acquire(blocking=False):
        return fail("stream_limit", "Live connection limit reached. Polling remains available.", 503)
    try:
        cursor = int(request.headers.get("Last-Event-ID", "0"))
    except ValueError:
        cursor = 0
    latest_id = db.session.scalar(select(db.func.max(ChangeEvent.id))) or 0
    cursor = min(max(0, cursor), latest_id)
    @stream_with_context
    def generate():
        nonlocal cursor
        started = time.monotonic()
        try:
            yield "retry: 3000\nevent: ready\ndata: {}\n\n"
            while time.monotonic() - started < 1800:
                rows = list(db.session.scalars(select(ChangeEvent).where(ChangeEvent.id > cursor).order_by(ChangeEvent.id).limit(100)))
                event_data = {"id": rows[-1].id, "kind": rows[-1].kind} if rows else None
                db.session.rollback()  # Never hold a transaction open while waiting.
                if event_data:
                    cursor = event_data["id"]
                    yield f"id: {cursor}\nevent: update\ndata: {json.dumps({'kind': event_data['kind']})}\n\n"
                else:
                    yield f"event: tick\ndata: {json.dumps({'server_time': iso(utcnow())})}\n\n"
                time.sleep(2)
        finally:
            _streams.release()
    return Response(generate(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
