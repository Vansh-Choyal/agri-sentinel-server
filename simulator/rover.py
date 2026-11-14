import logging
import math
import random
import threading
import time
import uuid
from datetime import datetime, timezone

import requests

log = logging.getLogger("simulator")


def approach(value, target, limit):
    return value + max(-limit, min(limit, target - value))


class Rover:
    def __init__(self, device, config, index=0, stop=None, session=None):
        self.device = dict(device)
        self.config = config
        self.random = random.Random(config.seed + index * 1009)
        self.network_random = random.Random(config.seed + index * 173 + 99)
        self.stop = stop or threading.Event()
        self.http = session or requests.Session()
        self.http.headers.update({"Authorization": f"Bearer {device['api_key']}", "User-Agent": "AgriSentinel-Simulator/1.0"})
        self.start = time.monotonic()
        self.boot = uuid.uuid4().hex[:12]
        self.sequence = 0
        self.index = index
        self.state = "patrolling"
        self.commanded_state = None
        self.phase = 0
        self.checkpoint = 1
        self.anomaly = None
        self.anomaly_ticks = 0
        self.obstacle_ticks = 0
        self.values = {"temperature_c": 28.5 + index * .7, "humidity_pct": 65 - index * 2., "soil_moisture_pct": 24.0 if index == 1 else 39. + index * 2, "mq2_raw": 210. + index * 15, "mq3_raw": 175. + index * 9, "heading_deg": 85. + index * 20, "tilt_deg": 2., "sound_level_raw": 110., "obstacle_distance_cm": 180.}

    def payload(self, now=None):
        now = now or datetime.now(timezone.utc)
        self.sequence += 1
        self.phase += 1
        rng = self.random
        # A shared daily solar cycle correlates temperature and relative humidity.
        hour = (now.hour + now.minute / 60 + 5.5) % 24
        solar = math.sin((hour - 8) / 24 * 2 * math.pi)
        target_temp = 28.5 + solar * 3.5 + self.index * .7
        target_humidity = 66 - solar * 9 - self.index * 1.5
        if self.anomaly_ticks <= 0 and rng.random() < self.config.anomaly_frequency:
            self.anomaly = rng.choice(["heat", "humidity", "dry", "mq2"])
            self.anomaly_ticks = rng.randint(6, 12)
            log.info("%s simulated anomaly=%s", self.device["robot_id"], self.anomaly)
        if self.anomaly_ticks > 0:
            self.anomaly_ticks -= 1
            if self.anomaly == "heat":
                target_temp, target_humidity = 39.5, 46
            elif self.anomaly == "humidity":
                target_humidity, target_temp = 91, 26
        if self.obstacle_ticks <= 0 and rng.random() < self.config.obstacle_frequency:
            self.obstacle_ticks = rng.randint(2, 4)
        self.obstacle_ticks = max(0, self.obstacle_ticks - 1)
        if self.commanded_state:
            self.state = self.commanded_state
        else:
            phase = self.phase % 18
            self.state = "idle" if phase in (0, 1) else "inspecting" if phase in (6, 7, 12, 13) else "patrolling"
        if self.obstacle_ticks and self.state not in ("idle", "stopped"):
            self.state = "stopped"
        if self.state == "inspecting" and self.phase % 3 == 0:
            self.checkpoint = self.checkpoint % 8 + 1
        values = self.values
        values["temperature_c"] = approach(values["temperature_c"], target_temp + rng.uniform(-.3, .3), .65)
        values["humidity_pct"] = approach(values["humidity_pct"], target_humidity + rng.uniform(-.7, .7), 2.8)
        # Soil dries with warmth. Recovery after a demo drought is an explicit
        # simulated watering pattern, never an inferred real-world intervention.
        if self.anomaly == "dry" and self.anomaly_ticks:
            moisture_target = 19
            moisture_step = 1.6
        elif self.anomaly == "dry" and not self.anomaly_ticks:
            moisture_target, moisture_step = 38, .8
            if values["soil_moisture_pct"] >= 37:
                self.anomaly = None
        else:
            moisture_target = values["soil_moisture_pct"] - (.025 + max(0, values["temperature_c"] - 25) * .004)
            moisture_step = .1
        values["soil_moisture_pct"] = approach(values["soil_moisture_pct"], moisture_target, moisture_step)
        values["mq2_raw"] = approach(values["mq2_raw"], 3750 if self.anomaly == "mq2" and self.anomaly_ticks else 220 + self.index * 15 + rng.uniform(-20, 20), 500)
        values["mq3_raw"] = approach(values["mq3_raw"], 180 + self.index * 9 + rng.uniform(-14, 14), 12)
        if self.state == "patrolling":
            values["heading_deg"] = (values["heading_deg"] + rng.uniform(-8, 14)) % 360
        values["tilt_deg"] = approach(values["tilt_deg"], rng.uniform(-4, 4), .9)
        values["sound_level_raw"] = approach(values["sound_level_raw"], 150 if self.state == "patrolling" else 85, 18) + rng.uniform(-4, 4)
        # Distance can fall abruptly as an object enters the ultrasonic cone.
        values["obstacle_distance_cm"] = rng.uniform(9, 17) if self.obstacle_ticks else approach(values["obstacle_distance_cm"], rng.uniform(140, 230), 85)
        sensors = {}
        defaults = {"temperature_c": (-40, 85), "humidity_pct": (0, 100), "soil_moisture_pct": (0, 100), "mq2_raw": (0, 4095), "mq3_raw": (0, 4095), "heading_deg": (0, 359.999), "tilt_deg": (-180, 180), "sound_level_raw": (0, 4095), "obstacle_distance_cm": (0, 1000)}
        for key, value in values.items():
            lower, upper = self.config.ranges.get(key, defaults[key])
            value = max(lower, min(upper, value))
            sensors[key] = int(round(value)) if key.endswith("_raw") else round(value, 2)
        return {"schema_version": 1, "event_id": f"{self.device['robot_id']}-{self.boot}-{self.sequence:08d}", "robot_id": self.device["robot_id"], "zone_id": self.device["zone_id"], "recorded_at": now.isoformat().replace("+00:00", "Z"), "uptime_ms": int((time.monotonic() - self.start) * 1000), "telemetry": sensors, "robot": {"status": self.state, "battery_pct": None, "checkpoint_id": f"cp-{self.checkpoint:02d}"}}

    def poll_commands(self):
        response = self.http.get(f"{self.config.api_url}/api/v1/robots/{self.device['robot_id']}/commands", timeout=self.config.timeout)
        response.raise_for_status()
        data = response.json()
        if data.get("zone_id"):
            self.device["zone_id"] = data["zone_id"]
        else:
            log.warning("%s is unassigned; waiting for a zone", self.device["robot_id"])
            return False
        for command in data["commands"]:
            self.commanded_state = {"patrol": "patrolling", "inspect": "inspecting", "stop": "stopped", "idle": "idle"}[command["action"]]
            ack = self.http.post(f"{self.config.api_url}/api/v1/robots/{self.device['robot_id']}/commands/{command['id']}/ack", json={"status": "completed", "note": "Applied by independent simulator"}, timeout=self.config.timeout)
            ack.raise_for_status()
            log.info("%s executed command=%s", self.device["robot_id"], command["action"])
        return True

    def send(self, payload):
        attempts = 0
        while not self.stop.is_set():
            try:
                if self.network_random.random() < self.config.network_failure_rate:
                    raise requests.ConnectionError("Injected network interruption")
                response = self.http.post(f"{self.config.api_url}/api/v1/telemetry", json=payload, timeout=self.config.timeout)
                if response.status_code in (200, 201):
                    log.info("%s POST %s event=%s zone=%s state=%s", self.device["robot_id"], response.status_code, payload["event_id"], payload["zone_id"], payload["robot"]["status"])
                    return True
                if 400 <= response.status_code < 500 and response.status_code not in (408, 429):
                    log.error("%s rejected HTTP %s: %s", self.device["robot_id"], response.status_code, response.text[:300])
                    return False
                response.raise_for_status()
            except (requests.RequestException, ValueError) as error:
                attempts += 1
                delay = min(60, 2 ** min(attempts, 6)) + self.network_random.uniform(0, .5)
                log.warning("%s POST failed (%s); retrying same event in %.1fs", self.device["robot_id"], error, delay)
                if self.stop.wait(delay):
                    return False
        return False

    def run(self, once=False):
        try:
            while not self.stop.is_set():
                try:
                    assigned = self.poll_commands()
                except (requests.RequestException, ValueError) as error:
                    log.warning("%s command poll failed: %s", self.device["robot_id"], error)
                    assigned = True  # Telemetry retries independently using last assignment.
                success = self.send(self.payload()) if assigned else False
                if once:
                    return success
                if self.stop.wait(self.config.interval):
                    break
        finally:
            self.http.close()
        return True
