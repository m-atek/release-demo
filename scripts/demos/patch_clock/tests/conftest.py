"""Fakes for video 24's tests: JFrog as a dictionary, Lambda's reads, and Grype's saved answers.

The JSON under fixtures/ is written from Grype v0.120.1's and Syft v1.54.1's source (their JSON models, read at those
tags, cmd/grype/cli/commands/internal/dbsearch and grype/presenter/models) and GitHub's advisory, not saved from a run:
no scan ran while this was built. first_day.py cve prints the real facts on the first day; if they differ, the
fixtures follow them.
"""

import base64
import hashlib
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(1, str(HERE.parents[1]))

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SAM = "U0SAM000001"


def saved(name):
    return json.loads((FIXTURES / name).read_text())


def saved_bytes(name):
    return (FIXTURES / name).read_bytes()


class Store:
    """JFrog: path to bytes. A PUT refuses a path that exists, as release-pipeline's token does."""

    def __init__(self, files=None):
        self.files, self.puts = dict(files or {}), []

    def request(self, method, path, token=None, data=None, headers=None):
        if method == "GET" and path.startswith("api/storage/"):
            target = path.removeprefix("api/storage/")
            if target in self.files:
                return 200, json.dumps({"checksums": {"sha256": hashlib.sha256(self.files[target]).hexdigest()}}).encode()
            below = {name[len(target) + 1:].split("/")[0]: "/" in name[len(target) + 1:] for name in self.files if name.startswith(target + "/")}
            if below:
                return 200, json.dumps({"children": [{"uri": f"/{name}", "folder": folder} for name, folder in sorted(below.items())]}).encode()
            return 404, b"{}"
        if method == "GET":
            return (200, self.files[path]) if path in self.files else (404, b"")
        if path in self.files:
            return 403, b"not allowed to overwrite"
        self.files[path] = data
        self.puts.append(path)
        return 201, b""

    def get(self, path):
        return self.request("GET", path)


def hexdigest(data):
    return hashlib.sha256(data).hexdigest()


def b64digest(data):
    return base64.b64encode(hashlib.sha256(data).digest()).decode()


def released(store, root, version, package, sbom, image=None):
    """A release folder as the pipeline leaves it: the package, build-info.json, the SBOM and what ties them."""
    info = {"service": root.rsplit("/", 1)[-1], "version": version, "package": "package.zip", "sha256": hexdigest(package)}
    if image:
        info["image"] = {"digest": image}
    folder = f"{root}/{version}"
    store.files[f"{folder}/package.zip"] = package
    store.files[f"{folder}/build-info.json"] = json.dumps(info).encode()
    store.files[f"{folder}/sbom.cdx.json"] = sbom
    store.files[f"{folder}/sbom-info.json"] = json.dumps({"package_sha256": hexdigest(package), "sbom_sha256": hexdigest(sbom)}).encode()
    return folder


class Functions:
    """Lambda's two reads, as release-demo-sbom-reader may make them."""

    def __init__(self, configs):
        self.configs, self.calls = configs, []

    def get_alias(self, FunctionName, Name):
        self.calls.append(("GetAlias", FunctionName))
        assert Name == "live"
        config = self.configs[FunctionName]
        if isinstance(config, Exception):
            raise config
        return {"FunctionVersion": config.get("Version", "7"), "RoutingConfig": config.get("RoutingConfig")}

    def get_function_configuration(self, FunctionName, Qualifier):
        self.calls.append(("GetFunctionConfiguration", FunctionName))
        return self.configs[FunctionName]


ORDERS, PRICING, QUOTE = b"orders-service package", b"pricing-api package", b"quote-service package"
IMAGE = "ab" * 32


class World:
    """Three live services as the spec's first reply finds them: 1.12.0 holds it, the others are clear."""

    def __init__(self):
        self.store = Store()
        released(self.store, "orders-service-releases", "1.12.0", ORDERS, saved_bytes("sbom-brotli-1.2.0.cdx.json"))
        released(self.store, "demos-releases/pricing-api", "2.1.2", PRICING, saved_bytes("sbom-empty.cdx.json"))
        released(self.store, "demos-releases/quote-service", "1.2.0", QUOTE, saved_bytes("sbom-30-components.cdx.json"), image=f"sha256:{IMAGE}")
        self.functions = Functions({
            "orders-service-production": {"Version": "25", "Description": "1.12.0", "CodeSha256": b64digest(ORDERS), "PackageType": "Zip"},
            "pricing-api-production": {"Version": "7", "Description": "2.1.2", "CodeSha256": b64digest(PRICING), "PackageType": "Zip"},
            "quote-service-production": {"Version": "7", "Description": "1.2.0", "CodeSha256": IMAGE, "PackageType": "Image"},
        })
        self.scans = []

    def scan(self, sbom_file):
        """Grype's answer for what the SBOM names, saved. Every call is a scan that really ran."""
        sbom = json.loads(Path(sbom_file).read_text())
        self.scans.append([c["name"] + " " + c.get("version", "") for c in sbom["components"]][:1])
        holds = any(c["name"] == "brotli" and c["version"] == "1.2.0" for c in sbom["components"])
        return saved("grype-by-cve-brotli-1.2.0.json" if holds else "grype-by-cve-clear.json")

    def patched(self):
        """After the second release: production on 1.12.2, with its own release folder."""
        package = b"orders-service package of 1.12.2"
        released(self.store, "orders-service-releases", "1.12.2", package, saved_bytes("sbom-brotli-1.2.1.cdx.json"))
        self.functions.configs["orders-service-production"] = {"Version": "26", "Description": "1.12.2", "CodeSha256": b64digest(package), "PackageType": "Zip"}


@pytest.fixture
def world(tmp_path, monkeypatch):
    import clock
    import live as readers
    import run_state
    import search
    import tools

    fake = World()
    monkeypatch.setenv("SLACK_CHANNEL", "")
    monkeypatch.setenv("SLACK_TS", "")
    monkeypatch.setattr(clock, "FOLDER", tmp_path / "clocks")
    monkeypatch.setattr(tools, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(tools, "usable_database", lambda: {"valid": True, "path": "/cache/6/vulnerability.db"})
    monkeypatch.setattr(tools, "db_search", lambda *ids: saved("grype-db-search-brotli.json"))
    monkeypatch.setattr(tools, "db_records", lambda vulnerability: [])
    monkeypatch.setattr(tools, "scan", fake.scan)
    monkeypatch.setattr(search, "ghsa_ids", lambda cve, status: ["GHSA-9v8h-qqgm-4m35"])
    monkeypatch.setattr(readers, "reader_client", lambda session=None: fake.functions)
    monkeypatch.setattr(readers, "evidence_reader", lambda: fake.store)
    monkeypatch.setattr(run_state, "now", lambda: 1793000031.0)
    monkeypatch.setattr(search.SWITCH, "is_on", lambda ssm=None: True)
    return fake
