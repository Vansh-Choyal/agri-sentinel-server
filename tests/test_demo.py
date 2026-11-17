import json
from datetime import timedelta

import pytest

from agrisentinel import create_app
from agrisentinel.models import CameraFrame, Robot, Setting, Telemetry, db, utcnow
from agrisentinel.services.demo import initialize_demo, readings, update_demo
from agrisentinel.services import chat


@pytest.fixture
def demo_app():
    app = create_app({"TESTING": True, "DEMO_DATA": True, "SQLALCHEMY_DATABASE_URI": "sqlite://",
                      "ADMIN_PASSWORD": "test-password-for-demo", "SECRET_KEY": "demo-test"})
    with app.app_context():
        db.session.add(Robot(id="rv-camera-01", name="ESP32 Camera", source="hardware", api_key_hash="c" * 64))
        db.session.commit()
    yield app
    with app.app_context():
        db.session.remove()
        db.drop_all()


def test_builtin_demo_history_updates_idempotently_and_preserves_hardware(demo_app):
    with demo_app.app_context():
        robots = list(db.session.scalars(db.select(Robot)))
        assert len(robots) == 4
        assert sum(r.source == "demo" for r in robots) == 3
        assert db.session.get(Robot, "rv-camera-01").source == "hardware"
        hardware = Robot(id="real-device", name="Physical rover", source="hardware", api_key_hash="x" * 64)
        db.session.add(hardware)
        db.session.commit()
        count = db.session.scalar(db.select(db.func.count(Telemetry.id)))
        assert count >= 3 * 48 * 30
        at = max(r.latest_recorded_at for r in robots if r.source == "demo")
        update_demo(now=at)
        assert db.session.scalar(db.select(db.func.count(Telemetry.id))) == count
        old = db.session.scalar(db.select(Telemetry).where(Telemetry.robot_id == "rv-001").order_by(Telemetry.id.desc()))
        old_temp, old_soil, old_zone = old.temperature_c, old.soil_moisture_pct, old.zone_id
        update_demo(now=at + timedelta(seconds=10))
        latest = db.session.scalar(db.select(Telemetry).where(Telemetry.robot_id == "rv-001").order_by(Telemetry.id.desc()))
        assert latest.recorded_at > at
        assert abs(latest.temperature_c - old_temp) < .2
        if latest.zone_id == old_zone:
            assert abs(latest.soil_moisture_pct - old_soil) < .2
        db.session.rollback()
        initialize_demo()
        assert db.session.get(Robot, "real-device").source == "hardware"
        assert db.session.get(Robot, "real-device").last_seen is None


def test_weather_correlation_and_irrigation_are_continuous():
    at = utcnow().replace(hour=0, minute=30, second=0, microsecond=0)  # 06:00 IST
    before = readings(at - timedelta(seconds=5), 0)[0]
    after = readings(at + timedelta(seconds=5), 0)[0]
    assert abs(after["soil_moisture_pct"] - before["soil_moisture_pct"]) < .2
    morning = readings(at, 0)[0]
    afternoon = readings(at + timedelta(hours=8), 0)[0]
    assert afternoon["temperature_c"] > morning["temperature_c"]
    assert afternoon["humidity_pct"] < morning["humidity_pct"]


def test_judge_context_and_report_filters_share_persisted_demo(demo_app):
    client = demo_app.test_client()
    with client.session_transaction() as session:
        session["operator"], session["csrf"] = "operator", "test-csrf"
    with demo_app.app_context():
        payload, _ = chat.completion_payload([{"role": "user", "content": "Which crop is growing now?"}], "en")
        snapshot = json.loads(payload["messages"][1]["content"].split("\n", 1)[1])
        assert {z["crop"] for z in snapshot["zones"]} == {"Wheat", "Chickpea", "Fallow"}
        assert all(z["cultivation"]["basis"] for z in snapshot["zones"])
        assert snapshot["past_24_hours"]["record_count"] > 0
        assert "No automatic irrigation actuator" in snapshot["project_facts"]["limitations"]
        assert "api_key" not in json.dumps(snapshot)
    for period in ("daily", "weekly", "monthly"):
        report = client.get(f"/api/v1/reports?period={period}&zones=zone-c").json
        assert report["record_count"] > 0
        assert [z["id"] for z in report["zones"]] == ["zone-c"]
        assert report["zones"][0]["crop"] == "Chickpea"
        assert report["sources"].keys() == {"demo"}
        csv = client.get("/api/v1/reports/export.csv", query_string={"period": period, "zones": "zone-c", "from": report["from"], "to": report["to"]})
        assert csv.status_code == 200
        assert "Chickpea" in csv.text and "demo" in csv.text
        assert "Wheat" not in csv.text
    command = client.post("/api/v1/robots/rv-001/commands", json={"action": "stop"}, headers={"X-CSRF-Token": "test-csrf"})
    assert command.status_code == 202
    with demo_app.app_context():
        update_demo(now=utcnow() + timedelta(seconds=10))
        assert db.session.get(Robot, "rv-001").state == "stopped"
    detail = client.get("/api/v1/robots/rv-001").json
    assert detail["commands"][0]["status"] == "completed"
    assert "no hardware action" in detail["commands"][0]["note"]


def test_surplus_demo_fleet_is_archived_and_camera_untouched(demo_app):
    with demo_app.app_context():
        camera = db.session.get(Robot, "rv-camera-01")
        camera.firmware = "preserve-firmware"
        db.session.add(CameraFrame(robot_id=camera.id, jpeg=b"camera-image", version="keep", width=320, height=240))
        db.session.add(Robot(id="rv-005", name="Old demo", source="demo", api_key_hash="e" * 64,
                             model="AgriSentinel S1 demo", zone_id="zone-e"))
        db.session.flush()
        db.session.add(Telemetry(event_id="old-extra", payload_hash="a" * 64, robot_id="rv-005", zone_id="zone-e",
                                 source="demo", recorded_at=utcnow(), uptime_ms=0, state="idle"))
        db.session.commit()
        initialize_demo()
        assert db.session.scalar(db.select(db.func.count(Robot.id))) == 4
        assert db.session.get(Robot, "rv-005") is None
        assert db.session.get(Robot, camera.id).api_key_hash == "c" * 64
        assert db.session.get(Robot, camera.id).firmware == "preserve-firmware"
        assert db.session.get(CameraFrame, camera.id).jpeg == b"camera-image"
        archives = list(db.session.scalars(db.select(Setting).where(Setting.key.like("retired_demo_fleet_archive_%"))))
        assert any(any(row["event_id"] == "old-extra" for row in archive.value["telemetry"]) for archive in archives)
        from agrisentinel.services.analytics import aggregate
        daily = aggregate({"period": "24h"})
        assert len(daily["zones"]) == 6
        assert {r["id"] for r in daily["robots"]} == {"rv-001", "rv-002", "rv-003"}
