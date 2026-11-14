from concurrent.futures import ThreadPoolExecutor

from agrisentinel import create_app
from agrisentinel.auth import key_hash
from agrisentinel.models import Alert, Robot, Telemetry, db


def settings(path):
    return {"TESTING":True,"SQLALCHEMY_DATABASE_URI":"sqlite:///"+path.as_posix(),"ADMIN_PASSWORD":"test-persistence-password","SECRET_KEY":"test-persistence-secret","SIM_ROVER_COUNT":4}


def test_history_survives_new_app_instance(tmp_path,payload):
    config=settings(tmp_path/"persistent.db")
    first=create_app(config)
    with first.app_context():
        db.session.get(Robot,"rv-001").api_key_hash=key_hash("persistent-test-key")
        db.session.commit()
    assert first.test_client().post("/api/v1/telemetry",json=payload,headers={"Authorization":"Bearer persistent-test-key"}).status_code==201
    second=create_app(config)
    with second.app_context():
        assert db.session.scalar(db.select(db.func.count(Telemetry.id)))==1
        assert db.session.get(Robot,"rv-001").last_seen


def test_concurrent_retries_are_idempotent(tmp_path,payload):
    app=create_app(settings(tmp_path/"concurrency.db"))
    with app.app_context():
        db.session.get(Robot,"rv-001").api_key_hash=key_hash("concurrent-test-key")
        db.session.commit()
    payload["telemetry"]["soil_moisture_pct"]=20
    def send(_):
        with app.test_client() as client:
            return client.post("/api/v1/telemetry",json=payload,headers={"Authorization":"Bearer concurrent-test-key"}).status_code
    with ThreadPoolExecutor(max_workers=4) as pool:
        statuses=list(pool.map(send,range(4)))
    assert sorted(statuses)==[200,200,200,201]
    with app.app_context():
        assert db.session.scalar(db.select(db.func.count(Telemetry.id)))==1
        assert db.session.scalar(db.select(db.func.count(Alert.id)))==1
