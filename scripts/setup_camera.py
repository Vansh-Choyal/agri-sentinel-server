"""Provision one camera rover and generate a private, ready-to-flash sketch.

Credentials are written under ignored instance/, never into the source template.
Existing rovers and their keys are never overwritten.
"""
import argparse
import getpass
import json
import secrets
import shutil
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

from agrisentinel import create_app
from agrisentinel.auth import key_hash
from agrisentinel.models import Robot, changed, db
from agrisentinel.validation import identifier


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-url", required=True, help="http://COMPUTER_WIFI_IP:5000")
    parser.add_argument("--ssid", required=True)
    parser.add_argument("--password", help="Omit to enter privately at the prompt")
    parser.add_argument("--robot-id", default="rv-camera-01")
    args = parser.parse_args()
    url = args.server_url.rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme != "http" or not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
        parser.error("Use a plain LAN HTTP origin, e.g. http://192.168.1.20:5000")
    identifier(args.robot_id, "robot_id")
    load_dotenv(ROOT / ".env")
    app = create_app()
    config_file = Path(app.instance_path) / f"{args.robot_id}-camera.json"
    saved = json.loads(config_file.read_text(encoding="utf-8")) if config_file.exists() else None
    with app.app_context():
        robot = db.session.get(Robot, args.robot_id)
        if robot:
            if not saved or robot.source != "hardware" or robot.api_key_hash != key_hash(saved["api_key"]):
                parser.error("Rover already exists without matching local camera credentials. Use a new --robot-id.")
            key = saved["api_key"]
        else:
            if db.session.scalar(db.select(db.func.count(Robot.id))) >= 200:
                parser.error("The 200-rover installation limit has been reached")
            key = secrets.token_urlsafe(32)
            robot = Robot(id=args.robot_id, name="ESP32 Camera", source="hardware", api_key_hash=key_hash(key),
                          model="ESP32-CAM · AI Thinker pinout", firmware="camera-1.0")
            db.session.add(robot)
            changed("fleet")
            db.session.commit()
    password = args.password if args.password is not None else getpass.getpass("Wi-Fi password: ")
    values = {"WIFI_SSID": args.ssid, "WIFI_PASSWORD": password, "SERVER_URL": url,
              "ROBOT_ID": args.robot_id, "DEVICE_KEY": key}
    template = ROOT / "firmware" / "AgriSentinelCamera"
    output = Path(app.instance_path) / "AgriSentinelCamera"
    output.mkdir(exist_ok=True)
    sketch = (template / "AgriSentinelCamera.ino").read_text(encoding="utf-8")
    for name, value in values.items():
        sketch = sketch.replace(f"@{name}@", json.dumps(value, ensure_ascii=True))
    target = output / "AgriSentinelCamera.ino"
    target.write_text(sketch, encoding="utf-8")
    shutil.copyfile(template / "partitions.csv", output / "partitions.csv")
    config_file.write_text(json.dumps({"robot_id": args.robot_id, "api_key": key, "server_url": url}, indent=2), encoding="utf-8")
    print(f"Ready-to-flash sketch: {target}\nRover: {args.robot_id}\nDashboard: {url}/#rovers")
    print("Keep instance/ private. Open the GENERATED sketch, not the source template.")


if __name__ == "__main__":
    main()
