"""Start the trace viewer on localhost and open it in the browser."""

import argparse
import socket
import sys
import threading
import webbrowser

import uvicorn

from viewer.api import create_app

HOST = "127.0.0.1"  # Local only: the app is never reachable from the network.
DEFAULT_PORT = 8765


def port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        return probe.connect_ex((HOST, port)) != 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--no-browser", action="store_true", help="Do not open a browser tab."
    )
    args = parser.parse_args()

    url = f"http://{HOST}:{args.port}"
    if not port_is_free(args.port):
        print(
            f"Port {args.port} is already in use. If the viewer is already running, "
            f"open {url}. Otherwise start it with: --port <another port>"
        )
        return 1

    print(f"Trace viewer running at {url} (press Ctrl+C to stop)")
    if not args.no_browser:
        threading.Timer(0.8, webbrowser.open, args=(url,)).start()
    uvicorn.run(create_app(), host=HOST, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
