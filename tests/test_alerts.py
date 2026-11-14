from datetime import timedelta

from agrisentinel.models import Alert, AlertTransition, Robot, db, iso, utcnow
from agrisentinel.services.presence import monitor_once


def send(client,payload,headers,event,value):
    payload["event_id"] = event
    payload["recorded_at"] = iso(utcnow())
    payload["telemetry"]["soil_moisture_pct"] = value
    return client.post("/api/v1/telemetry",json=payload,headers=headers)


def test_no_duplicate_alert_and_hysteresis(client,app,payload,device_headers):
    for event,value in [("low-1",22),("low-2",23),("band",27)]:
        assert send(client,payload,device_headers,event,value).status_code == 201
    with app.app_context():
        alerts = db.session.scalars(db.select(Alert)).all()
        assert len(alerts)==1 and alerts[0].status=="active"
    send(client,payload,device_headers,"missing",None)
    with app.app_context():
        assert db.session.scalar(db.select(Alert)).status=="active"
    send(client,payload,device_headers,"recovered",30)
    with app.app_context():
        assert db.session.scalar(db.select(Alert)).status=="resolved"
        assert db.session.scalar(db.select(db.func.count(AlertTransition.id)))==2
    send(client,payload,device_headers,"recurrence",20)
    with app.app_context():
        assert db.session.scalar(db.select(db.func.count(Alert.id)))==2


def test_stale_backfill_does_not_generate_sensor_alert(client,app,payload,device_headers):
    payload["recorded_at"]=iso(utcnow()-timedelta(minutes=10))
    payload["telemetry"]["soil_moisture_pct"]=10
    client.post("/api/v1/telemetry",json=payload,headers=device_headers)
    with app.app_context():
        assert db.session.scalar(db.select(Alert).where(Alert.rule_id=="soil-low")) is None
        assert db.session.scalar(db.select(Alert).where(Alert.rule_id=="telemetry-stale"))


def test_offline_monitor_and_recovery(client,app,payload,device_headers):
    client.post("/api/v1/telemetry",json=payload,headers=device_headers)
    with app.app_context():
        robot=db.session.get(Robot,"rv-001")
        robot.last_seen=utcnow()-timedelta(seconds=60)
        robot.latest_recorded_at=utcnow()-timedelta(seconds=60)
        db.session.commit()
        monitor_once();monitor_once()
        alerts=db.session.scalars(db.select(Alert)).all()
        assert {a.rule_id for a in alerts}=={"telemetry-stale","rover-offline"}
        assert len(alerts)==2
    send(client,payload,device_headers,"back-online",38)
    with app.app_context():
        assert all(a.status=="resolved" for a in db.session.scalars(db.select(Alert)))


def test_acknowledgement_does_not_resolve_condition(operator,app,payload,device_headers):
    send(operator,payload,device_headers,"low",20)
    response=operator.post("/api/v1/alerts/1/acknowledge",json={},headers={"X-CSRF-Token":"test-csrf"})
    assert response.status_code==200 and response.json["alert"]["status"]=="acknowledged"
    send(operator,payload,device_headers,"still-low",21)
    with app.app_context():
        assert db.session.scalar(db.select(db.func.count(Alert.id)))==1


def test_rule_validation_and_disable(operator,app,payload,device_headers):
    send(operator,payload,device_headers,"low",20)
    headers={"X-CSRF-Token":"test-csrf"}
    assert operator.patch("/api/v1/rules/soil-low",json={"trigger":30,"recovery":25},headers=headers).status_code==422
    assert operator.patch("/api/v1/rules/soil-low",json={"enabled":False},headers=headers).status_code==200
    with app.app_context():
        assert db.session.scalar(db.select(Alert)).status=="resolved"
