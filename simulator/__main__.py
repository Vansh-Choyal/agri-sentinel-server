import argparse
import logging
import signal
import threading
from concurrent.futures import ThreadPoolExecutor

from dotenv import load_dotenv

from .config import load_config
from .rover import Rover


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description="Independent AgriSentinel HTTP telemetry simulator")
    parser.add_argument("--config", help="JSON provisioning/configuration file")
    parser.add_argument("--once", action="store_true", help="Send one event per rover and exit (retries network failures)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = load_config(args.config)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    stop = threading.Event()
    def shutdown(*_):
        logging.info("Stopping simulator; interrupting retry waits")
        stop.set()
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    logging.info("Starting %s independent virtual rovers -> %s (interval %.1fs, seed %s)", len(config.rovers), config.api_url, config.interval, config.seed)
    with ThreadPoolExecutor(max_workers=len(config.rovers), thread_name_prefix="rover") as pool:
        futures = [pool.submit(Rover(device, config, index, stop).run, args.once) for index, device in enumerate(config.rovers)]
        results = [future.result() for future in futures]
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
