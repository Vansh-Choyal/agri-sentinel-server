"""Idempotent schema-v1 initialization. No telemetry is seeded."""
import json
import secrets
from pathlib import Path

from .auth import key_hash
from .models import AlertRule, Field, Robot, Setting, Zone, db


def initialize(app):
    version = db.session.get(Setting, "schema_version")
    if version and version.value != 1:
        raise RuntimeError("Unsupported database schema. Back up the database before upgrading.")
    if not version:
        db.session.add(Setting(key="schema_version", value=1))
    defaults = [
        ("soil-low", "Low soil moisture", "soil_moisture_pct", "below", 25, 29, "warning"),
        ("temperature-high", "High temperature", "temperature_c", "above", 36, 34, "critical"),
        ("humidity-high", "High humidity", "humidity_pct", "above", 85, 80, "warning"),
        ("obstacle-near", "Nearby obstacle", "obstacle_distance_cm", "below", 20, 30, "critical"),
        ("tilt-high", "Excessive tilt", "tilt_deg", "above", 18, 12, "critical"),
        ("mq2-high", "Unusual MQ2 raw signal", "mq2_raw", "above", 3500, 3300, "warning"),
        ("mq3-high", "Unusual MQ3 raw signal", "mq3_raw", "above", 3500, 3300, "warning"),
    ]
    for rid, name, metric, direction, trigger, recovery, severity in defaults:
        if not db.session.get(AlertRule, rid):
            db.session.add(AlertRule(id=rid, name=name, metric=metric, direction=direction, trigger=trigger, recovery=recovery, severity=severity))
    if app.config["SEED_DATA"] and not db.session.get(Field, "field-01"):
        db.session.add(Field(id="field-01", name="North Research Farm", description="Demonstration field · partition layout, not geolocation", area_ha=12.8))
        db.session.flush()
        zones = [("a", "West Plot", "Wheat", 2.4), ("b", "Central Plot", "Maize", 2.2), ("c", "East Plot", "Soybean", 2.0), ("d", "Lower West", "Chickpea", 2.2), ("e", "Lower Central", "Wheat", 2.1), ("f", "Lower East", "Fallow", 1.9)]
        for index, (letter, name, crop, area) in enumerate(zones):
            db.session.add(Zone(id=f"zone-{letter}", field_id="field-01", name=name, crop=crop, area_ha=area, map_col=index % 3, map_row=index // 3, checkpoint_count=8))
        db.session.flush()
        rovers = []
        count = 3 if app.config.get("DEMO_DATA") else max(1, min(6, app.config["SIM_ROVER_COUNT"]))
        for index in range(count):
            rid, zid = f"rv-{index + 1:03d}", f"zone-{chr(97 + index)}"
            key = secrets.token_urlsafe(32)
            db.session.add(Robot(id=rid, name=f"Sentinel {index + 1:02d}", zone_id=zid, api_key_hash=key_hash(key), source="simulator", model="AgriSentinel S1 · virtual", firmware="sim-1.0"))
            rovers.append({"robot_id": rid, "zone_id": zid, "api_key": key})
        # This local provisioning file is the only shared configuration. The simulator
        # never imports the app, SQLAlchemy, or opens the database.
        if not app.testing:
            file = Path(app.instance_path) / "simulator.json"
            file.write_text(json.dumps({"api_url": "http://127.0.0.1:5000", "interval_seconds": 10, "seed": 42, "anomaly_frequency": 0.08, "obstacle_frequency": 0.06, "rovers": rovers}, indent=2), encoding="utf-8")
    db.session.commit()
