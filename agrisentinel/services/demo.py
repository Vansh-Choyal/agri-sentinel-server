"""Built-in, time-based demonstration farm. No device process is required.

These are illustrative readings, not calibrated agronomic predictions. Smooth
shared weather and scheduled irrigation replace independent random samples.
"""
import hashlib
import math
import secrets
import json
from pathlib import Path
from datetime import datetime, timedelta, timezone

from flask import current_app
from sqlalchemy import select, text

from ..models import ActiveCondition, Alert, AlertTransition, CameraFrame, Command, Field, Inspection, Robot, Setting, Telemetry, Zone, changed, db, iso, utcnow
from .alerts import evaluate_presence, evaluate_sensors

IST = timezone(timedelta(hours=5, minutes=30))
INTERVAL = 5
PROFILE_KEY = "demonstration_farm_v1"
CROPS = ["Wheat", "Wheat", "Chickpea", "Chickpea", "Wheat", "Fallow"]
DEMO_ROVERS = ("rv-001", "rv-002", "rv-003")


def route_slot(at):
    # Park overnight instead of changing plots while reporting idle.
    return int(at.timestamp() // 600) if 6 <= at.astimezone(IST).hour < 19 else 0


def migrate_patrol_history(setting):
    if setting.value.get("patrol_history_version") == 1:
        return
    for index, rid in enumerate(DEMO_ROVERS):
        moved = {}
        for row in db.session.scalars(select(Telemetry).where(Telemetry.robot_id == rid, Telemetry.source == "demo").execution_options(yield_per=1000)):
            plot_index = index + (3 if route_slot(row.recorded_at) % 2 else 0)
            zid = f"zone-{chr(97 + plot_index)}"
            if row.zone_id != zid:
                row.zone_id = zid
                values, _, _, _ = readings(row.recorded_at, plot_index, plot_index == 5)
                for key, value in values.items():
                    setattr(row, key, value)
                moved.setdefault(zid, []).append(row.id)
        for zid, ids in moved.items():
            for start in range(0, len(ids), 500):
                db.session.execute(db.update(Inspection).where(Inspection.telemetry_id.in_(ids[start:start + 500])).values(zone_id=zid))
    setting.value = {**setting.value, "patrol_history_version": 1}


def retire_extra_demo_rovers():
    """Archive the old generated fleet before removing its surplus devices.

    The server-owned demo IDs are the only eligible devices. Hardware, including
    the ESP32 camera, and user-registered rovers are preserved.
    """
    extra = list(db.session.scalars(select(Robot).where(
        Robot.id.in_(("rv-004", "rv-005", "rv-006")),
        Robot.source.in_(("demo", "simulator")), Robot.model.like("AgriSentinel S1%"))))
    if not extra:
        return
    ids = [robot.id for robot in extra]
    alert_ids = select(Alert.id).where(Alert.robot_id.in_(ids))
    queries = [(ActiveCondition, ActiveCondition.alert_id.in_(alert_ids)),
               (AlertTransition, AlertTransition.alert_id.in_(alert_ids)),
               *[(model, model.robot_id.in_(ids)) for model in (Inspection, Command, CameraFrame, Alert, Telemetry)],
               (Robot, Robot.id.in_(ids))]
    # A complete recoverable archive is written before changing the fleet.
    archive = {}
    for model, where in queries:
        archive[model.__tablename__] = [{column.name: getattr(row, column.name) for column in model.__table__.columns}
                                      for row in db.session.scalars(select(model).where(where))]
    encode = lambda value: iso(value) if isinstance(value, datetime) else {"hex": value.hex()} if isinstance(value, bytes) else str(value)
    if current_app.testing:
        db.session.add(Setting(key=f"retired_demo_fleet_archive_{utcnow():%Y%m%dT%H%M%S%f}", value=json.loads(json.dumps(archive, default=encode))))
    else:
        folder = Path(current_app.instance_path) / "backups"
        folder.mkdir(exist_ok=True)
        path = folder / f"retired-demo-fleet-{utcnow():%Y%m%dT%H%M%S%f}.json"
        path.write_text(json.dumps(archive, default=encode), encoding="utf-8")
    for model, where in queries:
        db.session.execute(db.delete(model).where(where).execution_options(synchronize_session=False))
    changed("fleet")


def profile():
    setting = db.session.get(Setting, PROFILE_KEY)
    return setting.value if setting and current_app.config.get("DEMO_DATA") else None


def crop_context(zone):
    farm = profile()
    if not farm or zone.id not in farm["plots"]:
        return None
    plot = farm["plots"][zone.id]
    days = (utcnow().astimezone(IST).date() - datetime.fromisoformat(plot["sown_on"]).date()).days
    return {**plot, "days_after_sowing": max(0, days) if zone.crop != "Fallow" else None,
            "growth_stage": ("Emergence / seedling" if days < 21 else "Vegetative growth") if zone.crop != "Fallow" else "Resting plot; no crop planted",
            "basis": "Fictional demo farm plan; growth stage is calendar-based, not detected by sensors."}


def initialize_demo():
    if not current_app.config.get("DEMO_DATA"):
        return
    if db.engine.dialect.name == "sqlite":
        db.session.execute(text("BEGIN IMMEDIATE"))
    farm = db.session.get(Field, "field-01")
    if not farm:
        db.session.rollback()
        return
    retire_extra_demo_rovers()
    setting = db.session.get(Setting, PROFILE_KEY)
    if not setting:
        plots = {}
        for index, crop in enumerate(CROPS):
            zone = db.session.get(Zone, f"zone-{chr(97 + index)}")
            if not zone or zone.field_id != farm.id:
                continue
            # Only adopt the originally provisioned virtual rovers. Hardware and
            # user-registered devices are never converted into demo devices.
            if index < len(DEMO_ROVERS):
                robot = db.session.get(Robot, DEMO_ROVERS[index])
                if robot and (robot.source != "simulator" or not robot.model.startswith("AgriSentinel S1")):
                    continue
                if not robot:
                    robot = Robot(id=DEMO_ROVERS[index], name=f"Sentinel {index + 1:02d}",
                                  api_key_hash=hashlib.sha256(secrets.token_bytes(32)).hexdigest(), zone_id=zone.id)
                    db.session.add(robot)
                robot.source, robot.model, robot.firmware = "demo", "AgriSentinel S1 · demo", "builtin-demo-1"
            zone.crop = crop
            plots[zone.id] = {"sown_on": (utcnow().astimezone(IST).date() - timedelta(days=18 + index)).isoformat(),
                              "soil": "Loam" if index % 2 == 0 else "Clay loam",
                              "irrigation": "Illustrative irrigation every second morning at 06:00 IST; gradual drying between cycles." if crop != "Fallow" else "No scheduled irrigation"}
        db.session.add(Setting(key=PROFILE_KEY, value={
            "name": "North Research Farm demonstration", "scenario": "Fictional central Indian rabi-season research farm",
            "timezone": "Asia/Kolkata", "season": "Rabi", "plots": plots,
            "data_basis": "Generated demonstration data, not measurements from physical sensors or local weather.",
            "update_interval_seconds": INTERVAL,
            "demo_rover_count": 3,
            "patrol_routes": {rid: [f"zone-{chr(97 + i)}", f"zone-{chr(100 + i)}"] for i, rid in enumerate(DEMO_ROVERS)},
        }))
    else:
        setting.value = {**setting.value, "demo_rover_count": 3,
                         "patrol_routes": {rid: [f"zone-{chr(97 + i)}", f"zone-{chr(100 + i)}"] for i, rid in enumerate(DEMO_ROVERS)}}
    db.session.flush()
    migrate_patrol_history(db.session.get(Setting, PROFILE_KEY))
    db.session.commit()
    update_demo(backfill=True)


def readings(at, index, fallow=False):
    local = at.astimezone(IST)
    hour = local.hour + local.minute / 60 + local.second / 3600
    days = at.timestamp() / 86400
    daylight = math.cos((hour - 14.5) * math.tau / 24)
    weather = 1.1 * math.sin(days * math.tau / 5)
    ripple = .12 * math.sin(at.timestamp() / 85 + index)
    temperature = 23 + 6.3 * daylight + weather + index * .12 + ripple
    humidity = 66 - 17 * daylight - weather * 1.8 - index * .3 - ripple
    # Different irrigation schedules per plot. Soil changes over hours, with
    # infiltration taking about 40 minutes rather than an instantaneous jump.
    since = (days - (6 - 5.5) / 24 - index % 2) % 2
    # Periodic exponential response is continuous across the irrigation cycle
    # boundary, including residual water from preceding irrigation events.
    water = 10 * (math.exp(-since * .48) / (1 - math.exp(-.96)) - math.exp(-since * 36) / (1 - math.exp(-72)))
    soil = (17 + index * 1.4 + water - .4 * daylight) if not fallow else 19.4 - .5 * daylight
    phase = int(at.timestamp()) % 120
    checkpoint = int(at.timestamp() // 120 + index) % 8 + 1
    active = 6 <= hour < 19
    state = "inspecting" if active and phase < 20 else "patrolling" if active else "idle"
    battery = 94 - (hour - 6) * 3.2 if active else min(94, 52.4 + ((hour - 19) % 24) * 4.8)
    return {"temperature_c": round(temperature, 2), "humidity_pct": round(humidity, 2),
            "soil_moisture_pct": round(soil, 2), "mq2_raw": round(205 + index * 7 + 9 * math.sin(days * 5)),
            "mq3_raw": round(168 + index * 6 + 7 * math.sin(days * 5 + .6)),
            "heading_deg": round(((checkpoint - 1) % 4) * 90 + 2 + math.sin(at.timestamp() / 20), 2),
            "tilt_deg": round(1.2 * math.sin(at.timestamp() / 32 + index), 2),
            "sound_level_raw": round((140 if active else 95) + 8 * math.sin(at.timestamp() / 45 + index)),
            "obstacle_distance_cm": round(180 + 22 * math.sin(at.timestamp() / 25 + index), 1)}, state, round(battery, 1), f"cp-{checkpoint:02d}"


def update_demo(backfill=False, now=None):
    if not current_app.config.get("DEMO_DATA"):
        return
    now = (now or utcnow()).replace(microsecond=0)
    now -= timedelta(seconds=now.second % INTERVAL)
    if db.engine.dialect.name == "sqlite":
        db.session.execute(text("BEGIN IMMEDIATE"))
    robots = list(db.session.scalars(select(Robot).where(Robot.source == "demo", Robot.id.in_(DEMO_ROVERS)).order_by(Robot.id).with_for_update()))
    wrote = False
    for index, robot in enumerate(robots):
        if not robot.zone_id or (robot.latest_recorded_at and robot.latest_recorded_at >= now):
            continue
        route = [f"zone-{chr(97 + index)}", f"zone-{chr(100 + index)}"]
        if robot.zone_id not in route or any(not db.session.get(Zone, zid) for zid in route):
            route = [robot.zone_id]
        times = []
        if backfill:
            latest = db.session.scalar(select(db.func.max(Telemetry.recorded_at)).where(Telemetry.robot_id == robot.id, Telemetry.source == "demo"))
            cursor = max(now - timedelta(days=30), latest + timedelta(minutes=30) if latest else now - timedelta(days=30))
            while cursor < now - timedelta(seconds=INTERVAL):
                times.append(cursor)
                cursor += timedelta(minutes=30)
        times.append(now)
        pending = list(db.session.scalars(select(Command).where(Command.robot_id == robot.id, Command.status == "pending", Command.expires_at > now).order_by(Command.id)))
        for at in times:
            zone = db.session.get(Zone, route[route_slot(at) % len(route)])
            plot_index = ord(zone.id[-1]) - ord("a") if zone.id.startswith("zone-") and len(zone.id) == 6 else index
            values, state, battery, checkpoint = readings(at, plot_index, zone.crop == "Fallow")
            if at == now:
                # A command changes the demo rover's state until the next command.
                previous = db.session.scalar(select(Command).where(Command.robot_id == robot.id, Command.status == "completed").order_by(Command.id.desc()).limit(1))
                command = pending[-1] if pending else previous
                if command:
                    state = {"patrol": "patrolling", "inspect": "inspecting", "stop": "stopped", "idle": "idle"}[command.action]
                    if state in ("stopped", "idle"):
                        zone = db.session.get(Zone, robot.zone_id)
                        plot_index = ord(zone.id[-1]) - ord("a") if zone.id.startswith("zone-") and len(zone.id) == 6 else index
                        values, _, battery, checkpoint = readings(at, plot_index, zone.crop == "Fallow")
                for item in pending:
                    item.status, item.completed_at, item.note = "completed", now, "Applied to built-in demo rover; no hardware action."
                    changed("command")
            event = f"demo-v1-{robot.id}-{int(at.timestamp())}"
            row = Telemetry(event_id=event, payload_hash=hashlib.sha256(event.encode()).hexdigest(),
                            robot_id=robot.id, zone_id=zone.id, source="demo", recorded_at=at, received_at=at,
                            uptime_ms=int((at.timestamp() % 86400) * 1000), state=state, battery_pct=battery,
                            checkpoint_id=checkpoint, **values)
            db.session.add(row)
            if state == "inspecting":
                db.session.flush()
                # Each checkpoint is recorded once per two-minute inspection stop.
                previous = db.session.scalar(select(Inspection).where(Inspection.robot_id == robot.id, Inspection.zone_id == zone.id).order_by(Inspection.inspected_at.desc()).limit(1))
                if not previous or previous.checkpoint_id != checkpoint or (at - previous.inspected_at).total_seconds() >= 120:
                    db.session.add(Inspection(telemetry_id=row.id, robot_id=robot.id, zone_id=zone.id, checkpoint_id=checkpoint, inspected_at=at))
            if at == now:
                if robot.zone_id != zone.id:
                    from .alerts import retire_zone_conditions
                    retire_zone_conditions(robot, robot.zone_id)
                    robot.zone_id = zone.id
                robot.last_seen = robot.latest_recorded_at = at
                robot.state, robot.battery_pct, robot.checkpoint_id, robot.uptime_ms = state, battery, checkpoint, row.uptime_ms
                evaluate_sensors(robot, row)
                evaluate_presence(robot, now)
            wrote = True
    if wrote:
        changed("telemetry")
    db.session.commit()
