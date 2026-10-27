"""Helpers the stage scripts share: paths, services.yaml, git and HTTP."""

import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def services():
    return yaml.safe_load((ROOT / "services.yaml").read_text())


def service(name):
    all_services = services()
    if name not in all_services:
        fail(f"Unknown service {name}. services.yaml has: {', '.join(all_services)}")
    config = all_services[name]
    config["name"] = name
    config["dir"] = ROOT / config["path"]
    return config


def version_of(directory):
    return (Path(directory) / "VERSION").read_text().strip()


def git(*args, check=True):
    result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    if check and result.returncode != 0:
        fail(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result


def http_get(url, timeout=5):
    """GET a URL. Returns (status, body); status 0 when nothing answered."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode(errors="replace")
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        return 0, ""


def write_json(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, indent=2) + "\n")


def fail(message):
    print(message, file=sys.stderr)
    sys.exit(1)
