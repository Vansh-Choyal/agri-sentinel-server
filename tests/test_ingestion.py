from copy import deepcopy
from datetime import timedelta

import pytest

from agrisentinel.auth import key_hash
from agrisentinel.models import Inspection, Robot, Telemetry, db, iso, utcnow


def test_ingestion_persists_and_updates_rover(client, app, payload, device_headers):
    response = client.post("/api/v1/telemetry", json=payload, headers=device_headers)
    assert response.status_code == 201
    assert response.json["source"] == "simulator"
    with app.app_context():
        row = db.session.scalar(db.select(Telemetry))
        robot = db.session.get(Robot, "rv-001")
        assert row.temperature_c == 29.5 and row.battery_pct is None
        assert row.recorded_at.tzinfo is not None
        assert robot.last_seen and robot.latest_recorded_at == row.recorded_at
        assert robot.state == "patrolling"


@pytest.mark.parametrize("header", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": "Bearer test-key-rv-002"}])
def test_device_auth(client, payload, header):
    assert client.post("/api/v1/telemetry", json=payload, headers=header).status_code == 401


def test_zone_ownership(client, payload, device_headers):
    payload["zone_id"] = "zone-b"
    assert client.post("/api/v1/telemetry", json=payload, headers=device_headers).status_code == 403


def test_duplicate_retry_and_conflict(client, app, payload, device_headers):
    first = client.post("/api/v1/telemetry", json=payload, headers=device_headers)
    retry = client.post("/api/v1/telemetry", json=payload, headers=device_headers)
    assert first.status_code == 201 and retry.status_code == 200
    assert retry.json["status"] == "duplicate"
    payload["telemetry"]["temperature_c"] = 30
    assert client.post("/api/v1/telemetry", json=payload, headers=device_headers).status_code == 409
    with app.app_context():
        assert db.session.scalar(db.select(db.func.count(Telemetry.id))) == 1


@pytest.mark.parametrize("mutate", [
    lambda p:p.update(schema_version=True), lambda p:p.update(schema_version=2),
    lambda p:p.update(recorded_at="2026-11-04T06:30:00"),
    lambda p:p.update(recorded_at=iso(utcnow()+timedelta(minutes=5))),
    lambda p:p.update(uptime_ms=-1), lambda p:p.update(extra="unknown"),
    lambda p:p.update(uptime_ms=10**1000),
    lambda p:p["telemetry"].update(humidity_pct=101),
    lambda p:p["telemetry"].update(temperature_c=True),
    lambda p:p["telemetry"].update(mq2_raw=10.5),
    lambda p:p["telemetry"].update(heading_deg=360),
    lambda p:p["telemetry"].update(gas_ppm=10),
    lambda p:p["robot"].update(status="healthy"),
    lambda p:p["robot"].update(status={}),
    lambda p:p["robot"].update(checkpoint_id="cp-99"),
    lambda p:p.update(source="hardware"),
])
def test_strict_validation(client, payload, device_headers, mutate):
    mutate(payload)
    assert client.post("/api/v1/telemetry", json=payload, headers=device_headers).status_code == 422


def test_malformed_json_and_size(client, device_headers):
    assert client.post("/api/v1/telemetry", data="{}", headers=device_headers).status_code == 415
    assert client.post("/api/v1/telemetry", data="{bad", content_type="application/json", headers=device_headers).status_code == 422
    assert client.post("/api/v1/telemetry", data="x"*17000, content_type="application/json", headers=device_headers).status_code == 413


def test_missing_values_and_source_cannot_be_spoofed(client, app, payload, device_headers):
    payload["telemetry"] = {"temperature_c": None}
    assert client.post("/api/v1/telemetry", json=payload, headers=device_headers).status_code == 201
    with app.app_context():
        row = db.session.scalar(db.select(Telemetry))
        assert row.humidity_pct is None and row.temperature_c is None
        assert row.source == "simulator"


def test_out_of_order_retains_newest_state(client, app, payload, device_headers):
    client.post("/api/v1/telemetry", json=payload, headers=device_headers)
    payload.update(event_id="older", recorded_at=iso(utcnow()-timedelta(minutes=10)))
    payload["robot"]["status"] = "error"
    assert client.post("/api/v1/telemetry", json=payload, headers=device_headers).status_code == 201
    with app.app_context():
        assert db.session.get(Robot,"rv-001").state == "patrolling"


def test_inspections_use_reported_checkpoint(client, app, payload, device_headers):
    payload["robot"]["status"] = "inspecting"
    for i in range(2):
        payload["event_id"] = f"inspection-{i}"
        payload["recorded_at"] = iso(utcnow())
        assert client.post("/api/v1/telemetry",json=payload,headers=device_headers).status_code == 201
    with app.app_context():
        assert db.session.scalar(db.select(db.func.count(Inspection.id))) == 1
