"""Serve a Lambda function on localhost the way a function URL does.

    python scripts/lambda_local.py --dir services/orders-service --port 8001

Each request becomes a function URL event (payload format 2.0) for
handler(event, context). An unhandled exception answers 502 Internal Server
Error, as a function URL does, so a broken release fails here as in AWS.
The module loads once at start, like a cold start. scripts/dev.py runs one of
these per environment with that environment's settings.
"""

import argparse
import base64
import importlib.util
import json
import sys
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


class Context:
    """The parts of the Lambda context object a handler usually reads."""

    def __init__(self, name):
        self.function_name = name
        self.function_version = "$LATEST"
        self.aws_request_id = str(uuid.uuid4())
        self.memory_limit_in_mb = 128
        self._deadline = time.monotonic() + 30

    def get_remaining_time_in_millis(self):
        return max(0, int((self._deadline - time.monotonic()) * 1000))


def load_handler(directory, spec):
    module_name, function_name = spec.rsplit(".", 1)
    sys.path.insert(0, str(directory))
    module_spec = importlib.util.spec_from_file_location(module_name, Path(directory) / f"{module_name}.py")
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return getattr(module, function_name)


def make_server(function, name, port):
    class Handler(BaseHTTPRequestHandler):
        def do_request(self):
            url = urlsplit(self.path)
            length = int(self.headers.get("content-length") or 0)
            body = self.rfile.read(length) if length else b""
            event = {
                "version": "2.0",
                "rawPath": url.path,
                "rawQueryString": url.query,
                "headers": {key.lower(): value for key, value in self.headers.items()},
                "requestContext": {
                    "http": {"method": self.command, "path": url.path, "sourceIp": self.client_address[0]},
                    "requestId": str(uuid.uuid4()),
                    "timeEpoch": int(time.time() * 1000),
                },
                "body": base64.b64encode(body).decode() if body else None,
                "isBase64Encoded": bool(body),
            }
            try:
                result = function(event, Context(name))
                status = result.get("statusCode", 200)
                headers = result.get("headers", {})
                payload = result.get("body") or ""
                payload = base64.b64decode(payload) if result.get("isBase64Encoded") else payload.encode()
            except Exception:
                traceback.print_exc()
                status, headers = 502, {"content-type": "application/json"}
                payload = json.dumps({"message": "Internal Server Error"}).encode()

            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_request

        def log_message(self, format, *args):
            print(f"[{name}] {self.command} {self.path} {args[1] if len(args) > 1 else ''}", flush=True)

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dir", required=True, type=Path)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--handler", default="handler.handler")
    parser.add_argument("--name", default="function")
    args = parser.parse_args()

    function = load_handler(args.dir.resolve(), args.handler)
    server = make_server(function, args.name, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
