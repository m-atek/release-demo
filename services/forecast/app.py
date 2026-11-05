"""forecast: the demand forecast the planning team reads, in a container.

A small HTTP service, standard library only. /api/forecast answers the
fictional forecast from a file in the image, by week and by product; /health is
for the liveness probe and the pipeline's checks, /ready for the readiness probe.
"""

import json
import os
import signal
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
SERVICE = "forecast"
QUIET = {"/health", "/ready"}  # the probes would fill the log


def read_package(directory):
    """The version, and the commit the build step wrote, or "local" before a build."""
    version = (directory / "VERSION").read_text().strip()
    info = directory / "build_info.json"
    commit = json.loads(info.read_text()).get("commit", "unknown") if info.exists() else "local"
    return version, commit


VERSION, COMMIT = read_package(HERE)


def health():
    return {"status": "ok", "service": SERVICE, "environment": os.environ.get("ENVIRONMENT", "local"),
            "version": VERSION, "commit": COMMIT, "pod": os.environ.get("POD_NAME") or socket.gethostname()}


def weeks():
    return json.loads((HERE / "data" / "forecast.json").read_text())["weeks"]


def ready():
    """Ready once the forecast loads."""
    try:
        count = len(weeks())
    except (OSError, ValueError, KeyError) as error:
        return False, f"the forecast doesn't load: {error}"
    return True, f"{count} weeks"


def forecast(week="", product=""):
    """The weeks asked for, each with the products asked for."""
    found = []
    for row in weeks():
        if week and row["week"] != week:
            continue
        units = {name: count for name, count in row["units"].items() if not product or name == product}
        found.append({"week": row["week"], "units": units})
    return {"week": week, "product": product, "count": len(found), "weeks": found}


def respond(target):
    """(status, body) for a GET of target, a path with its query."""
    url = urlparse(target)
    query = {key: values[0] for key, values in parse_qs(url.query).items()}
    if url.path == "/health":
        return 200, health()
    if url.path == "/ready":
        ok, reason = ready()
        return (200 if ok else 503), {"ready": ok, "reason": reason, "version": VERSION}
    if url.path == "/api/forecast":
        return 200, forecast(query.get("week", ""), query.get("product", ""))
    return 404, {"error": "not found"}


class Handler(BaseHTTPRequestHandler):
    server_version = SERVICE
    sys_version = ""

    def do_GET(self):
        status, body = respond(self.path)
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):
        if urlparse(self.path).path not in QUIET:
            sys.stderr.write(f"{self.address_string()} {format % args}\n")


def main():
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("", port), Handler)
    server.daemon_threads = True
    # Kubernetes sends SIGTERM once the pod is out of the Service: finish what is in flight, then stop.
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown).start())
    print(f"{SERVICE} {VERSION} on :{port}: {ready()[1]}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
