import hashlib
import secrets
import threading
import time
from collections import defaultdict, deque
from functools import wraps
from pathlib import Path

from flask import Blueprint, current_app, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from .models import Robot, db

auth = Blueprint("auth", __name__)
_attempts = defaultdict(deque)
_lock = threading.Lock()


def key_hash(key):
    return hashlib.sha256(key.encode()).hexdigest()


def init_admin(app):
    credential_file = Path(app.instance_path) / "admin-credentials.txt"
    password = app.config["ADMIN_PASSWORD"]
    if not password:
        if credential_file.exists():
            password = credential_file.read_text(encoding="utf-8").split("Password: ", 1)[1].strip()
        else:
            password = secrets.token_urlsafe(18)
            credential_file.write_text(f"Username: {app.config['ADMIN_USERNAME']}\nPassword: {password}\n", encoding="utf-8")
    minimum_length = max(8, app.config.get("ADMIN_PASSWORD_MIN_LENGTH", 12))
    if len(password) < minimum_length:
        raise ValueError(f"ADMIN_PASSWORD must have at least {minimum_length} characters")
    app.config["ADMIN_PASSWORD_HASH"] = generate_password_hash(password)
    app.config["ADMIN_PASSWORD"] = None


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


def operator_required(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        if not session.get("operator"):
            if request.path.startswith("/api/"):
                return jsonify(error="authentication_required", message="Sign in to continue"), 401
            return redirect(url_for("auth.login"))
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            token = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token", "")
            if not secrets.compare_digest(str(token), session.get("csrf", "invalid")):
                return jsonify(error="csrf_failed", message="Refresh the page and try again"), 403
        return function(*args, **kwargs)
    return wrapped


def device_from_request(robot_id):
    raw = request.headers.get("Authorization", "")
    if not raw.startswith("Bearer "):
        return None
    robot = db.session.get(Robot, robot_id)
    if not robot or not secrets.compare_digest(robot.api_key_hash, key_hash(raw[7:])):
        return None
    return robot


@auth.route("/login", methods=["GET", "POST"])
def login():
    error = None
    token = csrf_token()
    if request.method == "POST":
        if not secrets.compare_digest(request.form.get("csrf_token", ""), token):
            return render_template("login.html", csrf_token=token, error="Session expired. Try again."), 403
        address = request.remote_addr or "local"
        with _lock:
            now = time.monotonic()
            # Bound memory even when many distinct clients attempt authentication.
            if len(_attempts) > 10000:
                _attempts.clear()
            bucket = _attempts[address]
            while bucket and bucket[0] < now - 300:
                bucket.popleft()
            if len(bucket) >= 10:
                return render_template("login.html", csrf_token=token, error="Too many attempts. Wait five minutes."), 429
            bucket.append(now)
        valid_password = check_password_hash(current_app.config["ADMIN_PASSWORD_HASH"], request.form.get("password", ""))
        if request.form.get("username") == current_app.config["ADMIN_USERNAME"] and valid_password:
            session.clear()
            session["operator"] = current_app.config["ADMIN_USERNAME"]
            session.permanent = True
            csrf_token()
            with _lock:
                _attempts.pop(address, None)
            return redirect(url_for("views.dashboard"))
        error = "The username or password is incorrect."
    return render_template("login.html", csrf_token=token, error=error), 401 if error else 200


@auth.post("/logout")
@operator_required
def logout():
    session.clear()
    return redirect(url_for("auth.login"))
