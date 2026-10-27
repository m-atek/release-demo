"""A stand-in for JFrog Container Registry, for tests and local runs only.

    python scripts/demos/clouds/tests/fake_jcr.py 18082     then JCR_URL=http://127.0.0.1:18082

It answers what the pipeline asks of JCR: generic uploads and downloads under
/artifactory/ with their SHA-256, and the Docker registry API under /v2/,
signing in the way JCR does, with a bearer token for a user name and token.
Everything is kept in memory. It is never used by the pipeline itself.
"""

import hashlib
import json
import re
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Store:
    def __init__(self):
        self.files, self.blobs, self.manifests, self.uploads = {}, {}, {}, {}


class Handler(BaseHTTPRequestHandler):
    store = Store()

    def log_message(self, *args):
        pass

    def reply(self, status, body=b"", headers=None):
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def body(self):
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def signed_in(self):
        """/v2/ wants the bearer token /v2/token gives for basic credentials."""
        if self.headers.get("Authorization", "").startswith("Bearer fake-"):
            return True
        host = self.headers.get("Host")
        self.reply(401, b"{}", {"WWW-Authenticate": f'Bearer realm="http://{host}/v2/token",service="fake-jcr"'})
        return False

    def route(self):
        path = self.path.split("?")[0]
        query = dict(p.split("=", 1) for p in self.path.partition("?")[2].split("&") if "=" in p)
        if path == "/v2/token":
            if not self.headers.get("Authorization", "").startswith("Basic "):
                return self.reply(401)
            return self.reply(200, json.dumps({"token": "fake-" + uuid.uuid4().hex}).encode())
        if path.startswith("/artifactory/api/storage/"):
            name = path.removeprefix("/artifactory/api/storage/")
            if name not in self.store.files:
                return self.reply(404)
            digest = hashlib.sha256(self.store.files[name]).hexdigest()
            return self.reply(200, json.dumps({"checksums": {"sha256": digest}}).encode())
        if path.startswith("/artifactory/"):
            name = path.removeprefix("/artifactory/")
            if self.command == "PUT":
                content = self.body()
                if name in self.store.files and self.store.files[name] != content:
                    return self.reply(403)
                self.store.files[name] = content
                return self.reply(201)
            if name in self.store.files:
                return self.reply(200, self.store.files[name])
            return self.reply(404)
        if not path.startswith("/v2/") or not self.signed_in():
            return None if path.startswith("/v2/") else self.reply(404)
        if match := re.fullmatch(r"/v2/(.+)/manifests/(.+)", path):
            repository, reference = match.groups()
            key = (repository, reference)
            if self.command == "PUT":
                content = self.body()
                digest = "sha256:" + hashlib.sha256(content).hexdigest()
                media_type = self.headers["Content-Type"]
                self.store.manifests[key] = self.store.manifests[(repository, digest)] = (media_type, content)
                return self.reply(201, b"", {"Docker-Content-Digest": digest})
            if key not in self.store.manifests:
                return self.reply(404)
            media_type, content = self.store.manifests[key]
            digest = "sha256:" + hashlib.sha256(content).hexdigest()
            return self.reply(200, content, {"Content-Type": media_type, "Docker-Content-Digest": digest})
        if match := re.fullmatch(r"/v2/(.+)/blobs/(sha256:[0-9a-f]+)", path):
            if match.group(2) not in self.store.blobs:
                return self.reply(404)
            return self.reply(200, self.store.blobs[match.group(2)])
        if re.fullmatch(r"/v2/(.+)/blobs/uploads/", path) and self.command == "POST":
            upload = uuid.uuid4().hex
            self.store.uploads[upload] = b""
            return self.reply(202, b"", {"Location": f"{path}{upload}"})
        if match := re.fullmatch(r"/v2/(.+)/blobs/uploads/([0-9a-f]+)", path):
            upload = match.group(2)
            if self.command == "PATCH":
                self.store.uploads[upload] += self.body()
                return self.reply(202, b"", {"Location": path})
            if self.command == "PUT":
                content = self.store.uploads.pop(upload) + self.body()
                digest = query["digest"].replace("%3A", ":")
                if "sha256:" + hashlib.sha256(content).hexdigest() != digest:
                    return self.reply(400)
                self.store.blobs[digest] = content
                return self.reply(201)
        return self.reply(404)

    do_GET = do_PUT = do_HEAD = do_POST = do_PATCH = route


def start(port=0):
    """A running stand-in on 127.0.0.1, in a thread: (server, base URL)."""
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


if __name__ == "__main__":
    server, url = start(int(sys.argv[1]) if len(sys.argv) > 1 else 18082)
    print(f"Fake JCR on {url}, for local runs only", flush=True)
    threading.Event().wait()

