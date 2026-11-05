"""1.30.0 and the baseline it is released over: a small real change, and an order page that can be on film."""

import importlib.util
import subprocess
import sys

import pytest

import cheat
from common import service
from conftest import HERE, STATES, by_path


def load(state, monkeypatch, environment="production"):
    for name in service("orders-service")["environments"]["production"]["settings"]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ENVIRONMENT", environment)
    monkeypatch.setenv("DEPLOYED_AT", "2 Nov 2026, 17:08 UTC")
    spec = importlib.util.spec_from_file_location(f"handler_{state.replace('.', '_')}", STATES / state / "handler.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def answers(module, path):
    event = {"rawPath": path, "rawQueryString": "", "requestContext": {"http": {"method": "GET", "path": path}}}
    try:
        return module.handler(event, None)
    except Exception:
        return {"statusCode": 502, "body": ""}


def test_1_30_0_is_itself_and_passes_its_own_tests():
    assert (STATES / "1.30.0" / "VERSION").read_text().strip() == "1.30.0"
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"],
                            cwd=STATES / "1.30.0", capture_output=True, text=True)
    assert result.returncode == 0, result.stdout


def test_1_30_0_is_1_26_0_with_three_lines_of_code_and_a_test():
    old = (STATES / "1.26.0" / "handler.py").read_text().splitlines()
    new = (STATES / "1.30.0" / "handler.py").read_text().splitlines()
    assert [line for line in new if line not in old] == [
        "RETURN_WINDOW_DAYS = 30", "        return_days=RETURN_WINDOW_DAYS,", '  <p class="note">Free returns within {return_days} days</p>']
    assert [line for line in old if line not in new] == []
    tests = (STATES / "1.30.0" / "tests" / "test_handler.py").read_text()
    assert "def test_page_shows_the_return_window():" in tests


@pytest.mark.parametrize("version", ["1.26.0", "1.30.0"])
def test_each_passes_both_checks_and_its_page_has_no_demo_label(version, monkeypatch):
    for environment in ("staging", "production"):
        module = load(version, monkeypatch, environment)
        for path in ["/health", *service("orders-service")["environments"]["production"]["check"]["paths"]]:
            assert answers(module, path)["statusCode"] == 200
        page = answers(module, "/")["body"]
        assert "Demo system" not in page and "DORA" not in page
        assert version in page and "Delivery estimate: 2 days" in page


def test_only_1_30_0_shows_the_new_line(monkeypatch):
    assert "Free returns" not in answers(load("1.26.0", monkeypatch), "/")["body"]
    assert "Free returns within 30 days" in answers(load("1.30.0", monkeypatch), "/")["body"]


def test_why_the_baseline_isn_t_the_main_video_s_its_page_carries_the_label(monkeypatch):
    """The order page is on film, and "Demo system" stays off screen. 1.3.0 has it, so does 1.4.0."""
    for version in ("1.3.0", "1.4.0"):
        assert "Demo system" in answers(load(version, monkeypatch), "/")["body"]


def test_1_30_0_needs_no_other_stage_and_goes_through_the_deploy_script_as_it_is():
    """No migrations/ and no database/: no version stage, and the wrapper in services.yaml goes straight to scripts/deploy.py."""
    folder = STATES / "1.30.0"
    assert sorted(path.name for path in folder.iterdir() if path.name != "__pycache__") == ["VERSION", "handler.py", "tests"]
    assert service("orders-service")["scripts"]["deploy"] == "scripts/demos/rollback-trap/deploy.py"


def test_the_helpers_and_the_reset_name_the_same_version():
    reset = by_path("two_ways_reset_for_version", HERE / "reset.py")
    assert cheat.VERSION == reset.STAGED == "1.30.0"
    assert reset.BASELINE == "1.26.0"
