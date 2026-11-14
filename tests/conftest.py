import pytest

from agrisentinel import create_app
from agrisentinel.auth import key_hash
from agrisentinel.models import Robot, db


@pytest.fixture
def app():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite://", "ADMIN_PASSWORD": "test-password-for-local", "SECRET_KEY": "test-secret", "SIM_ROVER_COUNT": 4})
    with app.app_context():
        for robot in db.session.scalars(db.select(Robot)):
            robot.api_key_hash = key_hash(f"test-key-{robot.id}")
        db.session.commit()
    yield app
    with app.app_context():
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def operator(client):
    with client.session_transaction() as session:
        session["operator"] = "operator"
        session["csrf"] = "test-csrf"
    return client


@pytest.fixture
def payload():
    from agrisentinel.models import iso, utcnow
    return {"schema_version": 1, "event_id": "rv-001-test-1", "robot_id": "rv-001", "zone_id": "zone-a", "recorded_at": iso(utcnow()), "uptime_ms": 360000, "telemetry": {"temperature_c": 29.5, "humidity_pct": 64.2, "soil_moisture_pct": 38.0, "mq2_raw": 210, "mq3_raw": 175, "heading_deg": 87.5, "tilt_deg": 2.1, "sound_level_raw": 110, "obstacle_distance_cm": 42.0}, "robot": {"status": "patrolling", "battery_pct": None, "checkpoint_id": "cp-03"}}


@pytest.fixture
def device_headers():
    return {"Authorization": "Bearer test-key-rv-001"}
