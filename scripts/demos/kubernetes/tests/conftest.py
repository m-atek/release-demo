import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # scripts/demos/kubernetes/ first: its build, deploy, check
sys.path.append(str(HERE.parents[2]))  # scripts/

import oci  # noqa: E402


@pytest.fixture
def base_image(tmp_path):
    """A tiny base image, as oci.pull returns one, without a registry."""
    layer = oci._gzip(oci._tar([("bin", None, 0o755), ("bin/sh", b"#!", 0o755)]))
    path = tmp_path / "layer"
    path.write_bytes(layer)
    config = json.dumps({"architecture": "amd64", "os": "linux", "config": {"Env": ["PATH=/bin"]},
                         "rootfs": {"type": "layers", "diff_ids": ["sha256:" + "0" * 64]}, "history": []}).encode()
    descriptor = {"mediaType": oci.LAYER, "digest": oci.digest(layer), "size": len(layer)}
    return oci.Image("docker.io/library/base:1@sha256:" + "1" * 64, "sha256:" + "2" * 64,
                     {"layers": [descriptor]}, config, [(descriptor, path)])


class FakeRegistry(BaseHTTPRequestHandler):
    """Just enough of the distribution API for a push: blobs, uploads, manifests."""
    blobs, manifests, uploads = {}, {}, {}

    def log_message(self, *args):
        pass

    def reply(self, status, headers=None, body=b""):
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        self.reply(200 if self.path.rsplit("/", 1)[1] in self.blobs else 404)

    def do_POST(self):
        self.reply(202, {"Location": "/v2/upload/1?state=x"})

    def do_PUT(self):
        data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path.startswith("/v2/upload/"):
            wanted = self.path.split("digest=")[1].replace("%3A", ":")
            if oci.digest(data) != wanted:
                return self.reply(400)
            self.blobs[wanted] = data
            return self.reply(201)
        manifest = json.loads(data)
        if any(d["digest"] not in self.blobs for d in [manifest["config"], *manifest["layers"]]):
            return self.reply(400, body=b"blob unknown")
        self.manifests[self.path] = data
        self.reply(201, {"Docker-Content-Digest": oci.digest(data)})


@pytest.fixture
def registry():
    FakeRegistry.blobs, FakeRegistry.manifests = {}, {}
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeRegistry)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield oci.Registry(f"http://127.0.0.1:{server.server_address[1]}", bearer="token"), FakeRegistry
    server.shutdown()
