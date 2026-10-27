"""Stages 5 and 12, Publish and Record: store the package in JFrog Container Registry.

    python scripts/publish.py orders-service             to <service>-candidates/<version>/<build>/
    python scripts/publish.py orders-service --release   copy it to <service>-releases/<version>/

A service with `jcr: {shared: true}` in services.yaml uses the shared pair
demos-candidates/<service>/ and demos-releases/<service>/ instead, because
JCR's free edition creates repositories only in its UI.

Uploads the package and build-info.json with their checksums, so JCR rejects a
corrupted upload. --release copies the exact candidate that reached production,
after checking its SHA-256 again. The token is release-pipeline's, which may
deploy but not delete, so nothing published can be overwritten.
"""

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

import run_state
from common import ROOT, fail, get_parameter, service, ssm_client

JCR = os.environ.get("JCR_URL", "http://localhost:8082")


def request(method, path, token, data=None, headers=None):
    req = urllib.request.Request(f"{JCR}/artifactory/{path}", data=data, method=method,
                                 headers={"Authorization": f"Bearer {token}", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def upload(token, path, content):
    headers = {"X-Checksum-Sha256": hashlib.sha256(content).hexdigest(),
               "X-Checksum-Sha1": hashlib.sha1(content).hexdigest(),
               "Content-Type": "application/octet-stream"}
    status, body = request("PUT", path, token, content, headers)
    if status in (200, 201):
        return
    if status in (403, 409):
        # Already stored and not ours to overwrite: fine if it is the same content.
        existing = stored_sha256(token, path)
        if existing == headers["X-Checksum-Sha256"]:
            print(f"  {path}: already stored with the same SHA-256")
            return
    fail(f"JCR refused {path} ({status}): {body[:200].decode(errors='replace')}")


def stored_sha256(token, path):
    status, body = request("GET", f"api/storage/{path}", token)
    return json.loads(body).get("checksums", {}).get("sha256") if status == 200 else None


def download(token, path):
    status, body = request("GET", path, token)
    if status != 200:
        fail(f"JCR has no {path} ({status})")
    return body


def candidates_root(name):
    """Where a service's candidates live: its own repository or its folder in the shared one."""
    return f"demos-candidates/{name}" if service(name).get("jcr", {}).get("shared") else f"{name}-candidates"


def releases_root(name):
    """Where a service's releases live: its own repository or its folder in the shared one."""
    return f"demos-releases/{name}" if service(name).get("jcr", {}).get("shared") else f"{name}-releases"


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("service")
    parser.add_argument("--release", action="store_true", help="copy the candidate to the releases repository")
    args = parser.parse_args()

    state = run_state.load()
    token = get_parameter(ssm_client(), "jcr/token")
    out = Path(os.environ.get("WORKSPACE", ROOT)) / "dist" / args.service
    info = json.loads((out / "build-info.json").read_text())

    if not args.release:
        info.update({"jira_key": state["jira_key"], "build_url": state["build"].get("url"),
                     "build": state["build"]["number"]})
        (out / "build-info.json").write_text(json.dumps(info, indent=2) + "\n")
        base = f"{candidates_root(args.service)}/{info['version']}/{state['build']['number']}"
        upload(token, f"{base}/{info['package']}", (out / info["package"]).read_bytes())
        upload(token, f"{base}/build-info.json", (out / "build-info.json").read_bytes())
        state["package"] = {"path": f"{base}/{info['package']}", "sha256": info["sha256"], "name": info["package"]}
        run_state.save(state)
        print(f"Published {base}/{info['package']}")
        print(f"sha256 {info['sha256']}")
        return

    package = download(token, state["package"]["path"])
    if hashlib.sha256(package).hexdigest() != state["package"]["sha256"]:
        fail(f"{state['package']['path']} no longer matches its SHA-256: not copying it to releases.")
    target = f"{releases_root(args.service)}/{info['version']}/{info['package']}"
    upload(token, target, package)
    upload(token, f"{releases_root(args.service)}/{info['version']}/build-info.json", (out / "build-info.json").read_bytes())
    state["package"]["release_path"] = target
    run_state.save(state)
    print(f"Copied to {target}")


if __name__ == "__main__":
    sys.exit(main())
