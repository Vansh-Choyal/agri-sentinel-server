import logging
import os
import secrets
from pathlib import Path

from flask import Flask, jsonify, request
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.exceptions import HTTPException

from .models import db


def create_app(config=None):
    root = Path(__file__).resolve().parent.parent
    app = Flask(__name__, instance_path=str(root / "instance"))
    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    secret_path = Path(app.instance_path) / "session-secret"
    if not secret_path.exists():
        secret_path.write_text(secrets.token_hex(32), encoding="utf-8")
    app.config.update(
        SECRET_KEY=os.getenv("SECRET_KEY") or secret_path.read_text(encoding="utf-8").strip(),
        SQLALCHEMY_DATABASE_URI=os.getenv("DATABASE_URL") or "sqlite:///agrisentinel.db",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        MAX_CONTENT_LENGTH=16384,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true",
        PERMANENT_SESSION_LIFETIME=28800,
        OFFLINE_TIMEOUT_SECONDS=int(os.getenv("OFFLINE_TIMEOUT_SECONDS", "45")),
        STALE_TIMEOUT_SECONDS=int(os.getenv("STALE_TIMEOUT_SECONDS", "30")),
        MONITOR_INTERVAL_SECONDS=int(os.getenv("MONITOR_INTERVAL_SECONDS", "5")),
        ADMIN_USERNAME=os.getenv("ADMIN_USERNAME", "operator"),
        ADMIN_PASSWORD=os.getenv("ADMIN_PASSWORD") or None,
        ADMIN_PASSWORD_MIN_LENGTH=max(8, int(os.getenv("ADMIN_PASSWORD_MIN_LENGTH", "12"))),
        SEED_DATA=True,
        SIM_ROVER_COUNT=int(os.getenv("SIM_ROVER_COUNT", "4")),
        MAX_SSE_CLIENTS=12,
        DEEPINFRA_API_KEY=os.getenv("DEEPINFRA_API_KEY", ""),
        DEEPINFRA_CHAT_MODEL=os.getenv("DEEPINFRA_CHAT_MODEL", "Qwen/Qwen3-235B-A22B-Instruct-2507"),
        DEEPINFRA_VOICE_CHAT_MODEL=os.getenv("DEEPINFRA_VOICE_CHAT_MODEL", "Qwen/Qwen3.5-35B-A3B"),
        DEEPINFRA_FAST_TTS_MODEL=os.getenv("DEEPINFRA_FAST_TTS_MODEL", "inworld-ai/realtime-tts-2"),
        DEEPINFRA_INDIC_TTS_MODEL=os.getenv("DEEPINFRA_INDIC_TTS_MODEL", "inworld-ai/realtime-tts-2"),
        DEEPINFRA_STT_MODEL=os.getenv("DEEPINFRA_STT_MODEL", "openai/whisper-large-v3-turbo"),
        DEEPINFRA_TTS_MODEL=os.getenv("DEEPINFRA_TTS_MODEL", "ResembleAI/chatterbox-multilingual"),
    )
    if config:
        app.config.update(config)
    if os.getenv("APP_ENV") == "production" and not config:
        if not os.getenv("SECRET_KEY") or not os.getenv("ADMIN_PASSWORD"):
            raise RuntimeError("Production requires explicit SECRET_KEY and ADMIN_PASSWORD")
    if app.config["STALE_TIMEOUT_SECONDS"] >= app.config["OFFLINE_TIMEOUT_SECONDS"]:
        raise ValueError("STALE_TIMEOUT_SECONDS must be less than OFFLINE_TIMEOUT_SECONDS")
    db.init_app(app)
    from .auth import auth, init_admin
    from .api import api
    from .views import views
    from .chat import chat
    from .camera import CameraStore, camera
    app.extensions["camera_store"] = CameraStore()
    app.register_blueprint(auth)
    app.register_blueprint(api, url_prefix="/api/v1")
    app.register_blueprint(views)
    app.register_blueprint(chat, url_prefix="/api/v1/chat")
    app.register_blueprint(camera, url_prefix="/api/v1")
    with app.app_context():
        db.create_all()
        init_admin(app)
        from .seed import initialize
        initialize(app)

    @app.after_request
    def security_headers(response):
        response.headers.update({
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "same-origin",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; font-src 'self'; img-src 'self' data: blob:; connect-src 'self'; media-src 'self' blob:; frame-ancestors 'none'; form-action 'self'; base-uri 'self'",
        })
        if request.path.startswith("/api/") or request.path in ("/login", "/", "/chatbot"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.errorhandler(413)
    def too_large(_):
        return jsonify(error="payload_too_large", message=f"Payload exceeds the {request.max_content_length // 1024} KiB limit"), 413

    @app.errorhandler(SQLAlchemyError)
    def database_error(error):
        db.session.rollback()
        logging.getLogger(__name__).exception("Database operation failed")
        return jsonify(error="database_unavailable", message="Database operation failed. Please retry."), 503

    @app.errorhandler(HTTPException)
    def http_error(error):
        if request.path.startswith("/api/"):
            return jsonify(error=error.name.lower().replace(" ", "_"), message=error.description), error.code
        return error

    @app.errorhandler(Exception)
    def unexpected_error(error):
        db.session.rollback()
        app.logger.exception("Unhandled request error")
        return jsonify(error="internal_error", message="The request could not be completed. Please try again."), 500

    return app
