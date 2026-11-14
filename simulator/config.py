import json
import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    api_url: str
    rovers: list
    interval: float = 10
    seed: int = 42
    anomaly_frequency: float = .08
    obstacle_frequency: float = .06
    network_failure_rate: float = 0
    timeout: float = 5
    ranges: dict = field(default_factory=dict)


def load_config(path=None):
    file = Path(path or os.getenv("SIM_CONFIG") or Path(__file__).resolve().parent.parent / "instance" / "simulator.json")
    if not file.exists():
        raise ValueError(f"Simulator configuration not found: {file}. Start python app.py once to provision devices, or set SIM_CONFIG.")
    data = json.loads(file.read_text(encoding="utf-8"))
    rovers = data.get("rovers", [])
    count = int(os.getenv("SIM_ROVER_COUNT", str(len(rovers))))
    if not 1 <= count <= len(rovers):
        raise ValueError(f"SIM_ROVER_COUNT must be 1..{len(rovers)} (provision additional simulator rovers through the dashboard first)")
    rovers = [dict(r) for r in rovers[:count]]
    assignments = dict(part.split(":", 1) for part in os.getenv("SIM_ZONE_ASSIGNMENTS", "").split(",") if part)
    for rover in rovers:
        if "SIM_API_KEY" in os.environ:
            rover["api_key"] = os.environ["SIM_API_KEY"]
        if rover["robot_id"] in assignments:
            rover["zone_id"] = assignments[rover["robot_id"]]
        if not all(rover.get(key) for key in ("robot_id", "zone_id", "api_key")):
            raise ValueError("Each rover needs robot_id, zone_id and its own api_key")
    if len({r["robot_id"] for r in rovers}) != len(rovers):
        raise ValueError("Rover IDs must be unique")
    config = Config(
        api_url=os.getenv("SIM_API_URL", data.get("api_url", "http://127.0.0.1:5000")).rstrip("/"),
        rovers=rovers,
        interval=float(os.getenv("SIM_INTERVAL_SECONDS", str(data.get("interval_seconds", 10)))),
        seed=int(os.getenv("SIM_SEED", str(data.get("seed", 42)))),
        anomaly_frequency=float(os.getenv("SIM_ANOMALY_FREQUENCY", str(data.get("anomaly_frequency", .08)))),
        obstacle_frequency=float(os.getenv("SIM_OBSTACLE_FREQUENCY", str(data.get("obstacle_frequency", .06)))),
        network_failure_rate=float(os.getenv("SIM_NETWORK_FAILURE_RATE", str(data.get("network_failure_rate", 0)))),
        timeout=float(os.getenv("SIM_TIMEOUT_SECONDS", str(data.get("timeout_seconds", 5)))),
        ranges=data.get("sensor_ranges", {}),
    )
    if not config.api_url.startswith(("http://", "https://")):
        raise ValueError("SIM_API_URL must use HTTP or HTTPS")
    if not .1 <= config.interval <= 3600 or not .1 <= config.timeout <= 60:
        raise ValueError("Interval must be .1..3600 seconds and timeout .1..60 seconds")
    if not all(0 <= value <= 1 for value in (config.anomaly_frequency, config.obstacle_frequency, config.network_failure_rate)):
        raise ValueError("Frequencies must be 0..1")
    valid_ranges = {"temperature_c": (-40, 85), "humidity_pct": (0, 100), "soil_moisture_pct": (0, 100), "mq2_raw": (0, 4095), "mq3_raw": (0, 4095), "heading_deg": (0, 359.999), "tilt_deg": (-180, 180), "sound_level_raw": (0, 4095), "obstacle_distance_cm": (0, 1000)}
    for key, bounds in config.ranges.items():
        if key not in valid_ranges or not isinstance(bounds, list) or len(bounds) != 2 or not valid_ranges[key][0] <= bounds[0] < bounds[1] <= valid_ranges[key][1]:
            raise ValueError(f"Invalid sensor range: {key}")
    return config
