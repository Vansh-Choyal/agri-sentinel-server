"""Local entry point. Waitress serves Flask; no debugger or reloader."""
import logging
import os

from dotenv import load_dotenv

from agrisentinel import create_app
from agrisentinel.services.presence import start_monitor

load_dotenv()
app = create_app()

if __name__ == "__main__":
    from waitress import serve

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    host, port = os.getenv("HOST", "0.0.0.0"), int(os.getenv("PORT", "5000"))
    stop_monitor = start_monitor(app)
    print(f"\n  AgriSentinel -> http://{host}:{port}\n  Operator credentials: {app.instance_path}/admin-credentials.txt\n  Built-in demo feed: enabled by default (DEMO_DATA=false for device-only operation)\n", flush=True)
    try:
        serve(app, host=host, port=port, threads=24, channel_timeout=120)
    finally:
        stop_monitor.set()
    