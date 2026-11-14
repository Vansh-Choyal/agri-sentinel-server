import ast
import json
import threading
from datetime import datetime,timezone
from pathlib import Path

import pytest
import requests

from simulator.config import Config,load_config
from simulator.rover import Rover


def configuration(**overrides):
    return Config(api_url="http://localhost:5000",rovers=[{"robot_id":"rv-001","zone_id":"zone-a","api_key":"test-key-rv-001"}],**overrides)


def test_simulator_payload_accepted_by_public_api(client):
    config=configuration()
    rover=Rover(config.rovers[0],config)
    for i in range(20):
        payload=rover.payload()
        response=client.post("/api/v1/telemetry",json=payload,headers={"Authorization":"Bearer test-key-rv-001"})
        assert response.status_code==201,response.json
        assert response.json["source"]=="simulator"


def test_seeded_correlated_sensor_changes_and_unique_events():
    config=configuration(anomaly_frequency=0,obstacle_frequency=0)
    first,second=Rover(config.rovers[0],config),Rover(config.rovers[0],config)
    now=datetime(2026,11,4,6,30,tzinfo=timezone.utc)
    previous=None
    event_ids=set()
    for _ in range(50):
        a,b=first.payload(now),second.payload(now)
        assert a["telemetry"]==b["telemetry"]
        assert a["robot"]["battery_pct"] is None
        assert a["event_id"] not in event_ids
        event_ids.add(a["event_id"])
        if previous:
            assert abs(a["telemetry"]["temperature_c"]-previous["temperature_c"])<=.66
            assert a["telemetry"]["soil_moisture_pct"]<=previous["soil_moisture_pct"]
        previous=a["telemetry"]


def test_retry_preserves_event_and_backoff():
    config=configuration()
    class FakeResponse:
        status_code=201
    class FakeHTTP:
        headers={}
        def __init__(self):self.payloads=[]
        def post(self,url,json,timeout):
            self.payloads.append(json.copy())
            if len(self.payloads)==1:raise requests.Timeout("test")
            return FakeResponse()
    class Stop:
        def __init__(self):self.waits=[]
        def is_set(self):return False
        def wait(self,seconds):self.waits.append(seconds);return False
    http,stop=FakeHTTP(),Stop()
    rover=Rover(config.rovers[0],config,stop=stop,session=http)
    assert rover.send(rover.payload())
    assert http.payloads[0]==http.payloads[1]
    assert 2<=stop.waits[0]<=2.5


def test_simulator_imports_no_backend_or_database():
    for file in (Path(__file__).parents[1]/"simulator").glob("*.py"):
        tree=ast.parse(file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node,ast.Import):names=[a.name for a in node.names]
            elif isinstance(node,ast.ImportFrom):names=[node.module or ""]
            else:continue
            assert not any(n.startswith(("agrisentinel","sqlalchemy","sqlite3")) for n in names)


def test_configuration_validation(tmp_path,monkeypatch):
    file=tmp_path/"sim.json"
    file.write_text(json.dumps({"rovers":configuration().rovers}),encoding="utf-8")
    assert load_config(file).interval==10
    monkeypatch.setenv("SIM_INTERVAL_SECONDS","0")
    with pytest.raises(ValueError):load_config(file)
