from datetime import timedelta

from agrisentinel.models import Robot, Telemetry, db, iso, utcnow

CSRF = {"X-CSRF-Token":"test-csrf"}


def test_dashboard_auth_and_csrf(client,operator):
    operator.get("/")
    assert client.get("/api/v1/robots").status_code==200
    assert client.post("/api/v1/robots",json={}).status_code==403
    with client.session_transaction() as session:
        session.clear()
    assert client.get("/").status_code==302
    assert client.get("/api/v1/overview").status_code==401


def test_login(client,app):
    response=client.get("/login")
    assert response.status_code==200
    with client.session_transaction() as session:
        token=session["csrf"]
    assert client.post("/login",data={"username":"operator","password":"test-password-for-local","csrf_token":token}).status_code==302
    assert client.get("/").status_code==200
    assert client.get("/api/v1/overview").status_code==200


def test_registration_key_not_exposed_later(operator):
    response=operator.post("/api/v1/robots",json={"id":"rv-new","name":"New rover","source":"hardware","zone_id":"zone-f"},headers=CSRF)
    assert response.status_code==201
    assert len(response.json["api_key"])>32
    for url in ("/api/v1/overview","/api/v1/robots","/api/v1/robots/rv-new"):
        text=operator.get(url).text
        assert response.json["api_key"] not in text
        assert "api_key_hash" not in text


def test_assignment_and_retry_of_previous_event(operator,payload,device_headers):
    assert operator.post("/api/v1/telemetry",json=payload,headers=device_headers).status_code==201
    assert operator.patch("/api/v1/robots/rv-001",json={"zone_id":"zone-f"},headers=CSRF).status_code==200
    assert operator.post("/api/v1/telemetry",json=payload,headers=device_headers).status_code==200
    payload["event_id"]="new-event-wrong-zone"
    assert operator.post("/api/v1/telemetry",json=payload,headers=device_headers).status_code==403


def test_command_queue_device_ack_and_authorization(operator,device_headers):
    response=operator.post("/api/v1/robots/rv-001/commands",json={"action":"stop"},headers=CSRF)
    assert response.status_code==202
    cid=response.json["command"]["id"]
    assert operator.get("/api/v1/robots/rv-001/commands").status_code==401
    pending=operator.get("/api/v1/robots/rv-001/commands",headers=device_headers).json
    assert pending["commands"][0]["action"]=="stop"
    path=f"/api/v1/robots/rv-001/commands/{cid}/ack"
    assert operator.post(path,json={"status":"completed"},headers={"Authorization":"Bearer test-key-rv-002"}).status_code==401
    assert operator.post(path,json={"status":"completed"},headers=device_headers).status_code==200
    assert operator.post(path,json={"status":"completed"},headers=device_headers).status_code==200
    assert operator.get("/api/v1/robots/rv-001/commands",headers=device_headers).json["commands"]==[]


def test_reports_nulls_csv_and_circular_mean(operator,payload,device_headers):
    for index,heading in enumerate((359,1)):
        payload.update(event_id=f"heading-{index}",recorded_at=iso(utcnow()))
        payload["telemetry"]["heading_deg"]=heading
        payload["telemetry"]["humidity_pct"]=None if index else 60
        assert operator.post("/api/v1/telemetry",json=payload,headers=device_headers).status_code==201
    response=operator.get("/api/v1/reports?period=daily")
    assert response.status_code==200
    report=response.json
    assert report["record_count"]==2
    assert report["statistics"]["humidity_pct"]["count"]==1
    assert report["statistics"]["humidity_pct"]["avg"]==60
    assert min(abs(report["statistics"]["heading_deg"]["avg"]),abs(report["statistics"]["heading_deg"]["avg"]-360))<.01
    csv=operator.get("/api/v1/reports/export.csv?period=daily")
    assert csv.status_code==200 and "text/csv" in csv.content_type
    assert "Soil moisture" in csv.text and "simulator" in csv.text


def test_bounded_history_and_filters(operator,payload,device_headers):
    for i in range(5):
        payload.update(event_id=f"page-{i}",recorded_at=iso(utcnow()))
        operator.post("/api/v1/telemetry",json=payload,headers=device_headers)
    first=operator.get("/api/v1/telemetry?limit=2").json
    assert len(first["items"])==2 and first["next_cursor"]
    second=operator.get(f"/api/v1/telemetry?limit=2&before_id={first['next_cursor']}").json
    assert {r["id"] for r in first["items"]}.isdisjoint(r["id"] for r in second["items"])
    assert operator.get("/api/v1/telemetry?limit=100000").status_code==422
    assert operator.get("/api/v1/analytics?period=bad").status_code==422
    assert len(operator.get("/api/v1/analytics?period=24h").json["points"])<=90


def test_overview_values_come_from_records(operator,payload,device_headers):
    initial=operator.get("/api/v1/overview").json
    assert initial["counts"]["online"]==0 and initial["latest"] is None
    operator.post("/api/v1/telemetry",json=payload,headers=device_headers)
    data=operator.get("/api/v1/overview").json
    assert data["counts"]["online"]==1 and data["counts"]["offline"]==3
    assert data["latest"]["telemetry"]["temperature_c"]==29.5
    assert data["zones"][0]["latest"]["source"]=="simulator"


def test_sse_authenticated_and_streams_database_changes(operator,payload,device_headers):
    operator.post("/api/v1/telemetry",json=payload,headers=device_headers)
    response=operator.get("/api/v1/events",buffered=False)
    assert response.status_code==200
    iterator=iter(response.response)
    assert b"event: ready" in next(iterator)
    assert b"event: update" in next(iterator)
    response.close()
