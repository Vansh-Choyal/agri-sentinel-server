"""Authenticated JPEG ingress and latest-frame viewing, independent of telemetry."""
import io
import secrets
import threading
import time
from dataclasses import dataclass

from flask import Blueprint, Response, current_app, jsonify, request
from PIL import Image, UnidentifiedImageError
from sqlalchemy.exc import IntegrityError

from .auth import device_from_request, operator_required
from .models import CameraFrame, Robot, db, iso, utcnow

camera = Blueprint("camera", __name__)
MAX_JPEG_BYTES = 256 * 1024
OFFLINE_SECONDS = 8
CHECKPOINT_SECONDS = 2
RGB332_PALETTE = [channel for value in range(256) for channel in
                  (((value >> 5) * 255 // 7), (((value >> 2) & 7) * 255 // 7), ((value & 3) * 85))]


@dataclass(frozen=True)
class LiveFrame:
    jpeg: bytes
    width: int
    height: int
    received_at: object
    version: str


class CameraStore:
    """Latest frame only; per-app state for the single-process competition server."""
    def __init__(self):
        self.lock = threading.Lock()
        self.frames = {}
        self.checkpoints = {}

    def get(self, robot_id):
        with self.lock:
            return self.frames.get(robot_id)

    def put(self, robot_id, frame):
        with self.lock:
            # Registration already caps the fleet at 200. Also bound cache memory
            # when a deployment's database contains more manually added devices.
            if robot_id not in self.frames and len(self.frames) >= 200:
                oldest = next(iter(self.frames))
                self.frames.pop(oldest)
                self.checkpoints.pop(oldest, None)
            self.frames[robot_id] = frame
            now = time.monotonic()
            due = now - self.checkpoints.get(robot_id, -float("inf")) >= CHECKPOINT_SECONDS
            if due:
                self.checkpoints[robot_id] = now
            return due


def store():
    return current_app.extensions["camera_store"]


def get_frame(robot_id):
    live = store().get(robot_id)
    if live:
        return live
    return db.session.get(CameraFrame, robot_id)


@camera.before_request
def camera_upload_limit():
    # Keep the existing 16 KiB JSON/telemetry limit everywhere else.
    if request.endpoint == "camera.upload_frame":
        request.max_content_length = MAX_JPEG_BYTES


@camera.post("/robots/<robot_id>/camera/frame")
def upload_frame(robot_id):
    robot = device_from_request(robot_id)
    if not robot:
        return jsonify(error="invalid_device_key", message="Valid rover Bearer key required"), 401
    if robot.source != "hardware":
        return jsonify(error="hardware_required", message="Camera uploads require a hardware rover"), 403
    if request.mimetype not in ("image/jpeg", "application/x-rgb332", "application/x-rgb565"):
        return jsonify(error="unsupported_media_type", message="Send image/jpeg, application/x-rgb332 or application/x-rgb565"), 415
    body = request.get_data()
    try:
        if request.mimetype in ("application/x-rgb332", "application/x-rgb565"):
            width = int(request.headers.get("X-Camera-Width", "0"))
            height = int(request.headers.get("X-Camera-Height", "0"))
            full_color = request.mimetype == "application/x-rgb565"
            bytes_per_pixel = 2 if full_color else 1
            if not (1 <= width <= 320 and 1 <= height <= 240) or len(body) != width * height * bytes_per_pixel:
                raise ValueError("Raw color frame length/dimensions do not match")
            output = io.BytesIO()
            if full_color:
                # ESP32 camera RGB565 is high-byte first; Pillow's BGR;16
                # decoder takes little-endian 5/6/5 pixels. Swap bytes only,
                # retaining every sensor color bit rather than quantizing to RGB332.
                little_endian = bytearray(len(body))
                little_endian[0::2] = body[1::2]
                little_endian[1::2] = body[0::2]
                image = Image.frombytes("RGB", (width, height), little_endian, "raw", "BGR;16")
                image.save(output, "JPEG", quality=88, subsampling=0)
            else:
                image = Image.frombytes("P", (width, height), body)
                image.putpalette(RGB332_PALETTE)
                image.convert("RGB").save(output, "JPEG", quality=60)
            jpeg = output.getvalue()
        else:
            jpeg = body
            with Image.open(io.BytesIO(jpeg)) as image:
                if image.format != "JPEG" or not (1 <= image.width <= 640 and 1 <= image.height <= 480):
                    raise ValueError("JPEG must be at most 640 x 480 pixels")
                width, height = image.size
                image.load()  # Reject truncated/corrupt frames, not just invalid headers.
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        return jsonify(error="invalid_frame", message="Send a complete JPEG (max 640 x 480) or matching RGB332/RGB565 pixels (max 320 x 240)"), 422
    frame = LiveFrame(jpeg, width, height, utcnow(), secrets.token_hex(16))
    checkpoint = store().put(robot_id, frame)
    if checkpoint:
        saved = db.session.get(CameraFrame, robot_id)
        if saved is None:
            saved = CameraFrame(robot_id=robot_id)
            db.session.add(saved)
        saved.jpeg, saved.width, saved.height = jpeg, width, height
        saved.received_at, saved.version = frame.received_at, frame.version
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            # A concurrent first frame can already have created its checkpoint.
            # The live frame remains available and the next checkpoint retries.
    # Camera reception must not invent sensor telemetry or mark the rover online.
    return jsonify(ok=True, received_at=iso(frame.received_at)), 200


@camera.get("/robots/<robot_id>/camera")
@operator_required
def camera_status(robot_id):
    if not db.session.get(Robot, robot_id):
        return jsonify(error="not_found", message="Rover not found"), 404
    frame = get_frame(robot_id)
    age = max(0, (utcnow() - frame.received_at).total_seconds()) if frame else None
    return jsonify(
        available=frame is not None, live=age is not None and age < OFFLINE_SECONDS,
        received_at=iso(frame.received_at) if frame else None,
        age_seconds=round(age, 1) if age is not None else None,
        version=frame.version if frame else None,
        width=frame.width if frame else None, height=frame.height if frame else None,
        offline_after_seconds=OFFLINE_SECONDS,
    )


@camera.get("/robots/<robot_id>/camera/latest.jpg")
@operator_required
def latest_frame(robot_id):
    if not db.session.get(Robot, robot_id):
        return jsonify(error="not_found", message="Rover not found"), 404
    frame = get_frame(robot_id)
    if frame is None:
        return jsonify(error="no_camera_frame", message="Waiting for the first camera frame"), 404
    age = max(0, (utcnow() - frame.received_at).total_seconds())
    unchanged = request.args.get("after") == frame.version
    return Response(b"" if unchanged else frame.jpeg, status=204 if unchanged else 200, mimetype="image/jpeg", headers={
        "Cache-Control": "no-store", "X-Camera-Received-At": iso(frame.received_at),
        "X-Camera-Version": frame.version,
        "X-Camera-Age": str(round(age, 3)), "X-Camera-Width": str(frame.width),
        "X-Camera-Height": str(frame.height), "X-Camera-Offline-After": str(OFFLINE_SECONDS),
    })
