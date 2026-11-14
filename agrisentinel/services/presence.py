import logging
import threading

from sqlalchemy import select, text

from ..models import ChangeEvent, Command, Robot, db, utcnow
from .alerts import evaluate_presence


def monitor_once():
    if db.engine.dialect.name == "sqlite":
        db.session.execute(text("BEGIN IMMEDIATE"))
    now = utcnow()
    for robot in db.session.scalars(select(Robot).order_by(Robot.id).with_for_update()):
        evaluate_presence(robot, now)
    for command in db.session.scalars(select(Command).where(Command.status == "pending", Command.expires_at < now)):
        command.status = "expired"
    # The notification log is disposable; telemetry and alert history are retained.
    cutoff = db.session.scalar(select(db.func.max(ChangeEvent.id)))
    if cutoff and cutoff > 10000:
        db.session.execute(db.delete(ChangeEvent).where(ChangeEvent.id < cutoff - 10000))
    db.session.commit()


def start_monitor(app):
    stop = threading.Event()

    def run():
        while not stop.wait(app.config["MONITOR_INTERVAL_SECONDS"]):
            with app.app_context():
                try:
                    monitor_once()
                except Exception:
                    db.session.rollback()
                    logging.getLogger(__name__).exception("Presence monitor failed; retrying next cycle")
    threading.Thread(target=run, name="presence-monitor", daemon=True).start()
    return stop
