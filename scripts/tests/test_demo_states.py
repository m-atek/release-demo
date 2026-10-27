"""history.py's plan and the demo states agree: every version exists, and each
one passes or fails the pipeline's checks in each environment as planned."""

import importlib.util
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import ROOT, service, version_of  # noqa: E402
from history import BASELINE, LIVE, PLAN, SERVICE  # noqa: E402

STATES = ROOT / "demo-states" / SERVICE


def load(state, monkeypatch, environment):
    """The state's handler, loaded fresh with only that environment's settings, as Lambda runs it."""
    settings = service(SERVICE)["environments"][environment].get("settings") or {}
    for name in ["STORES", "BRAND_TITLE", *service(SERVICE)["environments"]["staging"].get("settings", {}),
                 *service(SERVICE)["environments"]["production"].get("settings", {})]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ENVIRONMENT", environment)
    monkeypatch.setenv("DEPLOYED_AT", "27 Oct 2026, 09:30 UTC")
    for name, value in settings.items():
        monkeypatch.setenv(name, str(value))
    spec = importlib.util.spec_from_file_location(f"handler_{state.name.replace('.', '_')}_{environment}", state / "handler.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def answers(module, path):
    """The status a function URL gives: 502 when the handler raises."""
    try:
        return module.handler({"rawPath": path, "requestContext": {"http": {"method": "GET", "path": path}}}, None)["statusCode"]
    except Exception:
        return 502


def test_every_planned_version_has_a_demo_state():
    for version, _, _ in PLAN:
        assert version_of(STATES / version) == version
    assert PLAN[-1][0] == BASELINE == "1.3.0"
    assert [key for _, key, _ in PLAN] == [f"ORD-{n}" for n in range(1, 12)]


@pytest.mark.parametrize("version,key,expect", PLAN)
def test_each_version_passes_staging_and_meets_its_plan_in_production(version, key, expect, monkeypatch):
    staging = load(STATES / version, monkeypatch, "staging")
    assert answers(staging, "/health") == 200  # all staging's check asks
    production = load(STATES / version, monkeypatch, "production")
    assert answers(production, "/health") == 200
    paths = service(SERVICE)["environments"]["production"]["check"]["paths"]
    passes = all(answers(production, path) == 200 for path in paths)
    assert passes == (expect == LIVE), f"{version} ({key}) should {'pass' if expect == LIVE else 'fail'} production's check"


def test_each_version_is_a_small_step_from_the_one_before():
    """Neighbouring states differ in a few lines of handler.py, never by a rewrite."""
    import difflib

    versions = ["1.1.0", *[version for version, _, _ in PLAN]]
    for before, after in zip(versions, versions[1:]):
        old = (STATES / before / "handler.py").read_text().splitlines()
        new = (STATES / after / "handler.py").read_text().splitlines()
        changed = [line for line in difflib.unified_diff(old, new, lineterm="", n=0)
                   if line[:1] in "+-" and not line.startswith(("+++", "---"))]
        assert 0 < len(changed) <= 20, f"{before} to {after} changes {len(changed)} lines"


def test_states_use_no_random_module():
    """SonarQube flags every use of random: the fictional orders come from a SHA-256 picker."""
    for state in [STATES / "1.1.0", *(STATES / version for version, _, _ in PLAN)]:
        assert "import random" not in (state / "handler.py").read_text()
