"""Run against an already running server. Uses HTTP and the independent simulator.

Creates identifiable simulated telemetry and command history in the local instance.
It does not remove any stored data. Use --offline to also wait for timeout recovery.
"""
import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def main():
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=os.getenv("SIM_API_URL", "http://127.0.0.1:5000"))
    parser.add_argument("--offline", action="store_true", help="Wait for the actual offline timeout; stop any continuously running simulator first")
    args = parser.parse_args()
    http = requests.Session()
    url = args.url.rstrip("/")
    page = http.get(url + "/login", timeout=5)
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text)[1]
    password = os.getenv("ADMIN_PASSWORD")
    if not password:
        password = (ROOT / "instance/admin-credentials.txt").read_text(encoding="utf-8").split("Password: ", 1)[1].strip()
    response = http.post(url + "/login", data={"username": os.getenv("ADMIN_USERNAME", "operator"), "password": password, "csrf_token": token}, timeout=5)
    response.raise_for_status()
    csrf = re.search(r'name="csrf-token" content="([^"]+)"', response.text)[1]
    http.headers["X-CSRF-Token"] = csrf
    def get(path):
        response = http.get(url + "/api/v1" + path, timeout=15)
        response.raise_for_status()
        return response.json()
    def run_sim():
        # Keep command verification deterministic; normal simulator runs retain
        # configurable obstacle overrides, which can legitimately stop motion.
        env = dict(os.environ, SIM_API_URL=url, SIM_OBSTACLE_FREQUENCY="0")
        subprocess.run([sys.executable, "-m", "simulator", "--once"], cwd=ROOT, env=env, check=True, timeout=40)
    initial = get("/overview")
    run_sim()
    after = get("/overview")
    assert after["counts"]["telemetry"] > initial["counts"]["telemetry"]
    assert after["counts"]["online"] > 0
    assert all(r["source"] == "simulator" for r in after["activity"][:4])
    print("PASS independent simulator -> HTTP ingestion -> fleet/zone read APIs", flush=True)
    response = http.post(url + "/api/v1/robots/rv-001/commands", json={"action": "inspect"}, timeout=5)
    response.raise_for_status()
    command_id = response.json()["command"]["id"]
    run_sim()
    detail = get("/robots/rv-001")
    assert detail["robot"]["state"] == "inspecting"
    assert next(c for c in detail["commands"] if c["id"] == command_id)["status"] == "completed"
    assert detail["inspections"]
    print("PASS command queue -> device poll -> acknowledgement -> inspection", flush=True)
    report = get("/reports?period=daily")
    assert report["record_count"] >= 8 and report["statistics"]["temperature_c"]["count"] > 0
    csv = http.get(url + "/api/v1/reports/export.csv?period=daily", timeout=15)
    assert csv.ok and "Environmental trends" in csv.text
    assert get("/alerts?status=all")["items"]
    print("PASS stored history -> analytics, alerts and CSV reporting", flush=True)
    if args.offline:
        deadline = time.monotonic() + after["timeouts"]["offline"] + 15
        print("Waiting for device heartbeat and measurement expiry...", flush=True)
        while time.monotonic() < deadline:
            time.sleep(2)
            expired = get("/overview")
            if expired["counts"]["online"] == 0:
                assert all(r["stale"] for r in expired["robots"])
                if any(a["rule_id"] == "rover-offline" for a in get("/alerts?status=open")["items"]):
                    print("PASS simulator stopped -> stale telemetry -> offline fleet -> rule alerts", flush=True)
                    break
        else:
            raise AssertionError("Devices did not become offline. Is another simulator still running?")
    print("End-to-end smoke checks passed.", flush=True)


if __name__ == "__main__":
    main()
