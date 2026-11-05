"""1.33.0 and 1.33.1: one line of text each, on a clean baseline, passing what the pipeline asks of a release."""

import difflib
import importlib.util
import subprocess
import sys

import pytest

import common
from conftest import ROOT

STATES = ROOT / "demo-states" / "orders-service"
VERSIONS = ["1.33.0", "1.33.1"]
LINES = {"1.33.0": "Free returns within 30 days", "1.33.1": "Order by 20:00, shipped today"}


def load(version, monkeypatch, environment="production"):
    for name in common.service("orders-service")["environments"]["production"]["settings"]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ENVIRONMENT", environment)
    monkeypatch.setenv("DEPLOYED_AT", "5 Nov 2026, 09:30 UTC")
    spec = importlib.util.spec_from_file_location(f"handler_{version.replace('.', '_')}", STATES / version / "handler.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def changed(before, after):
    old = (STATES / before / "handler.py").read_text().splitlines()
    new = (STATES / after / "handler.py").read_text().splitlines()
    return [line for line in difflib.unified_diff(old, new, lineterm="", n=0) if line[:1] in "+-" and not line.startswith(("+++", "---"))]


@pytest.mark.parametrize("version", VERSIONS)
def test_each_version_is_itself_and_passes_its_own_tests(version):
    assert (STATES / version / "VERSION").read_text().strip() == version
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"],
                            cwd=STATES / version, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout


@pytest.mark.parametrize("before, after", [("1.26.0", "1.33.0"), ("1.33.0", "1.33.1")])
def test_each_is_one_line_of_text_on_the_order_page(before, after):
    lines = changed(before, after)
    assert len(lines) == 1 and lines[0].startswith("+") and LINES[after] in lines[0] and 'class="note"' in lines[0]


@pytest.mark.parametrize("version", VERSIONS)
def test_each_passes_both_checks_shows_its_line_and_no_demo_label(version, monkeypatch):
    module = load(version, monkeypatch)
    event = {"rawPath": "/health", "requestContext": {"http": {"method": "GET", "path": "/health"}}}
    assert module.handler(event, None)["statusCode"] == 200
    for path in common.service("orders-service")["environments"]["production"]["check"]["paths"]:
        event = {"rawPath": path, "requestContext": {"http": {"method": "GET", "path": path}}}
        assert module.handler(event, None)["statusCode"] == 200
    body = module.handler({"rawPath": "/", "requestContext": {"http": {"method": "GET", "path": "/"}}}, None)["body"]
    assert LINES[version] in body and version in body
    text = (STATES / version / "handler.py").read_text()
    assert "Demo system" not in text and 'class="demo"' not in text


def test_the_second_keeps_the_first_line():
    assert LINES["1.33.0"] in (STATES / "1.33.1" / "handler.py").read_text()


@pytest.mark.parametrize("version", VERSIONS)
def test_neither_brings_a_package_so_the_scan_has_nothing_new(version):
    assert not (STATES / version / "requirements.txt").exists()
    assert sorted(path.name for path in (STATES / version).iterdir() if path.name != "__pycache__") == ["VERSION", "handler.py", "tests"]


def test_the_versions_are_free():
    """No other demo state claims 1.33.x, and only this video's reset archives them."""
    assert sorted(path.name for path in STATES.iterdir() if path.name.startswith("1.33.")) == VERSIONS
