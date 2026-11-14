import io
from datetime import timedelta

import pytest
from PIL import Image

from agrisentinel.auth import key_hash
from agrisentinel.models import CameraFrame, Robot, db, utcnow

PATH = "/api/v1/robots/rv-cam/camera"
HEADERS = {"Authorization": "Bearer camera-test-key", "Content-Type": "image/jpeg"}


@pytest.fixture
def camera_rover(app):
    with app.app_context():
        db.session.add(Robot(id="rv-cam", name="Test camera", source="hardware", api_key_hash=key_hash("camera-test-key")))
        db.session.commit()


def jpeg(size=(320, 240), color="green"):
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, "JPEG")
    return output.getvalue()


def test_camera_upload_and_view_are_separately_authenticated(client, camera_rover):
    assert client.post(PATH + "/frame", data=jpeg(), content_type="image/jpeg").status_code == 401
    assert client.get(PATH).status_code == 401
    assert client.get(PATH + "/latest.jpg", headers=HEADERS).status_code == 401
    with client.session_transaction() as session:
        session["operator"] = "operator"
        session["csrf"] = "test-csrf"
    # Operator login does not grant device upload access.
    assert client.post(PATH + "/frame", data=jpeg(), content_type="image/jpeg").status_code == 401


def test_camera_waiting_upload_replace_and_no_telemetry(operator, app, camera_rover):
    assert operator.get(PATH).json["available"] is False
    assert operator.get(PATH + "/latest.jpg").status_code == 404
    image = jpeg()
    assert operator.post(PATH + "/frame", data=image, headers=HEADERS).status_code == 200
    status = operator.get(PATH).json
    assert status["live"] and status["available"] and status["width"] == 320 and status["height"] == 240
    response = operator.get(PATH + "/latest.jpg")
    assert response.data == image and response.mimetype == "image/jpeg"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Camera-Version"] == status["version"]
    replacement = jpeg(color="blue")
    assert operator.post(PATH + "/frame", data=replacement, headers=HEADERS).status_code == 200
    assert operator.get(PATH + "/latest.jpg").data == replacement
    assert operator.get(PATH).json["version"] != status["version"]
    with app.app_context():
        assert db.session.scalar(db.select(db.func.count(CameraFrame.robot_id))) == 1
        assert db.session.get(Robot, "rv-cam").last_seen is None
        assert db.session.get(Robot, "rv-cam").latest_recorded_at is None


def test_camera_offline_keeps_last_image(operator, app, camera_rover, monkeypatch):
    operator.post(PATH + "/frame", data=jpeg(), headers=HEADERS)
    later = utcnow() + timedelta(seconds=10)
    monkeypatch.setattr("agrisentinel.camera.utcnow", lambda: later)
    status = operator.get(PATH).json
    assert status["available"] and not status["live"] and status["age_seconds"] >= 10
    assert operator.get(PATH + "/latest.jpg").status_code == 200


@pytest.mark.parametrize("data", [b"", b"not a JPEG", b"\xff\xd8bad\xff\xd9", jpeg()[:700], jpeg((641, 480))])
def test_invalid_jpeg_rejected(operator, camera_rover, data):
    assert operator.post(PATH + "/frame", data=data, headers=HEADERS).status_code == 422
    assert operator.get(PATH).json["available"] is False


def test_wrong_mime_and_simulator_and_other_rover_key(operator, camera_rover, device_headers):
    assert operator.post(PATH + "/frame", data=jpeg(), headers={**HEADERS, "Content-Type": "application/octet-stream"}).status_code == 415
    assert operator.post(PATH + "/frame", data=jpeg(), headers={**HEADERS, **device_headers}).status_code == 401
    assert operator.post("/api/v1/robots/rv-001/camera/frame", data=jpeg(), headers={**HEADERS, **device_headers}).status_code == 403
    assert operator.get("/api/v1/robots/missing/camera").status_code == 404
    assert operator.get("/api/v1/robots/missing/camera/latest.jpg").status_code == 404


def test_camera_limit_does_not_raise_telemetry_limit(operator, camera_rover):
    output = io.BytesIO()
    Image.effect_noise((320, 240), 100).convert("RGB").save(output, "JPEG", quality=95)
    image = output.getvalue()
    assert len(image) > 16384
    assert operator.post(PATH + "/frame", data=image, headers=HEADERS).status_code == 200
    assert operator.post(PATH + "/frame", data=b"x" * (256 * 1024 + 1), headers=HEADERS).status_code == 413
    assert operator.post("/api/v1/telemetry", data=b" " * 16385, content_type="application/json").status_code == 413


def test_failed_upload_preserves_previous_image(operator, camera_rover):
    image = jpeg()
    operator.post(PATH + "/frame", data=image, headers=HEADERS)
    assert operator.post(PATH + "/frame", data=b"broken", headers=HEADERS).status_code == 422
    assert operator.get(PATH + "/latest.jpg").data == image


def test_latest_frame_survives_new_app_instance(tmp_path):
    from agrisentinel import create_app

    config = {"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///" + (tmp_path / "camera.db").as_posix(),
              "ADMIN_PASSWORD": "test-camera-persistence", "SECRET_KEY": "camera-persistence-test"}
    first = create_app(config)
    with first.app_context():
        db.session.add(Robot(id="rv-cam", name="Persistent camera", source="hardware", api_key_hash=key_hash("camera-test-key")))
        db.session.commit()
    image = jpeg()
    assert first.test_client().post(PATH + "/frame", data=image, headers=HEADERS).status_code == 200
    second = create_app(config)
    client = second.test_client()
    with client.session_transaction() as session:
        session["operator"] = "operator"
    assert client.get(PATH + "/latest.jpg").data == image
    assert client.get(PATH).json["available"] is True


def test_raw_color_upload_is_encoded_on_server(operator, camera_rover):
    # Three broad stripes avoid JPEG color bleed at the tested center pixels.
    raw = (bytes([0xe0]) * 32 + bytes([0x1c]) * 32 + bytes([0x03]) * 32) * 32
    headers = {**HEADERS, "Content-Type": "application/x-rgb332", "X-Camera-Width": "96", "X-Camera-Height": "32"}
    assert operator.post(PATH + "/frame", data=raw, headers=headers).status_code == 200
    image = Image.open(io.BytesIO(operator.get(PATH + "/latest.jpg").data))
    assert image.size == (96, 32)
    for location, channel in [((16, 16), 0), ((48, 16), 1), ((80, 16), 2)]:
        color = image.getpixel(location)
        assert color[channel] > 230 and all(color[i] < 25 for i in range(3) if i != channel)


@pytest.mark.parametrize("width,height,body", [("0", "1", b"x"), ("321", "1", b"x" * 321),
    ("1", "241", b"x" * 241), ("no", "1", b"x"), ("160", "120", b"short"), ("1", "1", b"xx")])
def test_invalid_raw_frame_is_rejected(operator, camera_rover, width, height, body):
    headers = {**HEADERS, "Content-Type": "application/x-rgb332", "X-Camera-Width": width, "X-Camera-Height": height}
    assert operator.post(PATH + "/frame", data=body, headers=headers).status_code == 422
    assert operator.get(PATH).json["available"] is False


def test_conditional_frame_has_metadata_without_retransmitting_image(operator, camera_rover):
    operator.post(PATH + "/frame", data=jpeg(), headers=HEADERS)
    first = operator.get(PATH + "/latest.jpg")
    version = first.headers["X-Camera-Version"]
    same = operator.get(PATH + "/latest.jpg?after=" + version)
    assert same.status_code == 204 and same.data == b""
    assert same.headers["X-Camera-Version"] == version
    assert same.headers["X-Camera-Width"] == "320" and same.headers["X-Camera-Height"] == "240"
    assert float(same.headers["X-Camera-Age"]) >= 0
    operator.post(PATH + "/frame", data=jpeg(color="blue"), headers=HEADERS)
    assert operator.get(PATH + "/latest.jpg?after=" + version).status_code == 200


def test_latest_frame_is_live_before_next_disk_checkpoint(operator, app, camera_rover):
    import time
    operator.post(PATH + "/frame", data=jpeg(), headers=HEADERS)
    with app.app_context():
        first_version = db.session.get(CameraFrame, "rv-cam").version
    replacement = jpeg(color="blue")
    operator.post(PATH + "/frame", data=replacement, headers=HEADERS)
    assert operator.get(PATH + "/latest.jpg").data == replacement
    with app.app_context():
        assert db.session.get(CameraFrame, "rv-cam").version == first_version
    time.sleep(2.01)
    operator.post(PATH + "/frame", data=replacement, headers=HEADERS)
    with app.app_context():
        assert db.session.get(CameraFrame, "rv-cam").version != first_version


def test_rgb565_quality_frame_preserves_color_order_and_resolution(operator, camera_rover):
    line = b"\xf8\x00" * 80 + b"\x07\xe0" * 80 + b"\x00\x1f" * 80 + b"\xff\xff" * 80
    headers = {**HEADERS, "Content-Type": "application/x-rgb565", "X-Camera-Width": "320", "X-Camera-Height": "240"}
    raw = line * 240
    assert len(raw) == 153600
    assert operator.post(PATH + "/frame", data=raw, headers=headers).status_code == 200
    image = Image.open(io.BytesIO(operator.get(PATH + "/latest.jpg").data))
    assert image.size == (320, 240)
    for x, expected in [(40, (255, 0, 0)), (120, (0, 255, 0)), (200, (0, 0, 255)), (280, (255, 255, 255))]:
        actual = image.getpixel((x, 120))
        assert all(abs(a - b) <= 3 for a, b in zip(actual, expected))


def test_rgb565_keeps_fine_color_steps_lost_in_speed_mode(operator, camera_rover):
    # RGB565 has 32 red levels; the old RGB332 transport retained only eight.
    line = b"".join(((x // 10) << 11).to_bytes(2, "big") for x in range(320))
    headers = {**HEADERS, "Content-Type": "application/x-rgb565", "X-Camera-Width": "320", "X-Camera-Height": "240"}
    assert operator.post(PATH + "/frame", data=line * 240, headers=headers).status_code == 200
    image = Image.open(io.BytesIO(operator.get(PATH + "/latest.jpg").data))
    assert len({image.getpixel((index * 10 + 5, 120))[0] for index in range(32)}) >= 28


@pytest.mark.parametrize("width,height,body", [("0", "1", b"xx"), ("321", "1", b"x" * 642),
    ("1", "241", b"x" * 482), ("no", "1", b"xx"), ("320", "240", b"short"), ("1", "1", b"x"), ("1", "1", b"xxx")])
def test_invalid_rgb565_frame_is_rejected(operator, camera_rover, width, height, body):
    headers = {**HEADERS, "Content-Type": "application/x-rgb565", "X-Camera-Width": width, "X-Camera-Height": height}
    assert operator.post(PATH + "/frame", data=body, headers=headers).status_code == 422
    assert operator.get(PATH).json["available"] is False
