import argparse
import logging
import os
import signal
import sys
import threading

# PyInstaller console=False sets sys.stdout/stderr to None which breaks uvicorn logging
if sys.stdout is None:
    sys.stdout = open(os.devnull, 'w')
if sys.stderr is None:
    sys.stderr = open(os.devnull, 'w')

import uvicorn

from bridge.config import DEFAULT_PORT, DEFAULT_HOST, BRIDGE_NAME, BRIDGE_VERSION
from bridge.server import set_shutdown_callback

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# Suppress harmless ConnectionResetError from asyncio (browser cancels video range requests on seek)
class _ConnectionResetFilter(logging.Filter):
    def filter(self, record):
        return "ConnectionResetError" not in record.getMessage()

logging.getLogger("asyncio").addFilter(_ConnectionResetFilter())


def main():
    parser = argparse.ArgumentParser(description=BRIDGE_NAME)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Port to listen on (default: {DEFAULT_PORT})")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"Host to bind to (default: {DEFAULT_HOST})")
    parser.add_argument("--no-tray", action="store_true", help="Disable system tray icon")
    args = parser.parse_args()

    logger.info("Starting %s v%s on %s:%d", BRIDGE_NAME, BRIDGE_VERSION, args.host, args.port)

    server_config = uvicorn.Config(
        "bridge.server:app",
        host=args.host,
        port=args.port,
        log_level="info",
        access_log=False,
    )
    server = uvicorn.Server(server_config)

    def shutdown():
        logger.info("Shutting down server...")
        server.should_exit = True

    set_shutdown_callback(shutdown)

    if args.no_tray:
        # Run server directly on main thread
        server.run()
    else:
        # Run server in background thread, tray on main thread
        server_thread = threading.Thread(target=server.run, daemon=True)
        server_thread.start()

        try:
            from bridge.tray import run_tray
            run_tray(port=args.port, shutdown_callback=shutdown)
        except KeyboardInterrupt:
            shutdown()
        except Exception as e:
            logger.error("Tray icon failed: %s. Running in console mode.", e)
            # Fall back to waiting for the server thread
            try:
                server_thread.join()
            except KeyboardInterrupt:
                shutdown()


if __name__ == "__main__":
    main()
