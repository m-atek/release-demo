"""The demo states: what each answers, that each passes production's check, and how many lines of
code the budget would count between them, through real git."""

import importlib.util
import shutil
import subprocess
import sys

import pytest

import budget
from conftest import HERE
from common import service  # noqa: E402

ROOT = HERE.parents[2]
STATES = ROOT / "demo-states" / "orders-service"
VERSIONS = ["1.26.0", "1.27.0", "1.27.1", "1.28.0", "1.28.1"]
SOCKS = "product=Sports%20socks%2C%203%20pairs"


def load(state, monkeypatch, environment="production"):
    for name in service("orders-service")["environments"]["production"]["settings"]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ENVIRONMENT", environment)
    monkeypatch.setenv("DEPLOYED_AT", "4 Nov 2026, 09:30 UTC")
    spec = importlib.util.spec_from_file_location(f"handler_{state.replace('.', '_')}", STATES / state / "handler.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def answers(module, path, query=""):
    """The status a function URL gives: 502 when the handler raises."""
    event = {"rawPath": path, "rawQueryString": query, "requestContext": {"http": {"method": "GET", "path": path}}}
    try:
        return module.handler(event, None)["statusCode"]
    except Exception:
        return 502


@pytest.mark.parametrize("version", VERSIONS)
def test_each_version_is_itself_and_passes_its_own_tests(version):
    assert (STATES / version / "VERSION").read_text().strip() == version
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"],
                            cwd=STATES / version, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout


@pytest.mark.parametrize("version", VERSIONS)
def test_each_version_passes_both_checks_and_shows_no_demo_label(version, monkeypatch):
    module = load(version, monkeypatch)
    assert answers(module, "/health") == 200
    for path in service("orders-service")["environments"]["production"]["check"]["paths"]:
        assert answers(module, path) == 200
    text = (STATES / version / "handler.py").read_text()
    assert "Demo system" not in text and 'class="demo"' not in text
    for forbidden in ("boto3.client(\"appconfig", "pg8000", "anthropic"):
        assert forbidden not in text


@pytest.mark.parametrize("version, status", [("1.26.0", 200), ("1.27.0", 502), ("1.27.1", 200), ("1.28.0", 502), ("1.28.1", 200)])
def test_the_failing_request_fails_only_where_it_should(version, status, monkeypatch):
    assert answers(load(version, monkeypatch), "/api/price", SOCKS) == status


@pytest.mark.parametrize("version", ["1.27.0", "1.27.1", "1.28.0", "1.28.1"])
def test_the_old_store_apps_name_answers_from_1_27_0_on(version, monkeypatch):
    module = load(version, monkeypatch)
    event = {"rawPath": "/api/price", "rawQueryString": "product=Rain%20jacket%2C%20blue",
             "requestContext": {"http": {"method": "GET", "path": "/api/price"}}}
    assert '"price_eur": 74.5' in module.handler(event, None)["body"]


def test_the_comma_gets_past_the_tests_of_the_bad_versions():
    for version in ("1.26.0", "1.27.0", "1.28.0"):
        assert "Sports socks" not in (STATES / version / "tests" / "test_handler.py").read_text()
    for version in ("1.27.1", "1.28.1"):
        assert "Sports socks, 3 pairs" in (STATES / version / "tests" / "test_handler.py").read_text()


@pytest.mark.parametrize("version", ["1.28.0", "1.28.1"])
def test_the_feature_is_on_the_page(version, monkeypatch):
    module = load(version, monkeypatch)
    event = {"rawPath": "/", "rawQueryString": "", "requestContext": {"http": {"method": "GET", "path": "/"}}}
    assert "Pickup in store: ready in 2 hours" in module.handler(event, None)["body"]


@pytest.fixture
def repository(tmp_path, monkeypatch):
    """A real git repository with the service folder, and evidence.py's git pointed at it."""
    import evidence

    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    monkeypatch.setattr(evidence, "ROOT", tmp_path)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    run = lambda *args: subprocess.run(["git", *args], cwd=tmp_path, capture_output=True, text=True, check=True).stdout.strip()
    run("init", "-q")

    def stage(version):
        target = tmp_path / "services" / "orders-service"
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(STATES / version, target, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
        (tmp_path / "scripts-unrelated.txt").write_text(version)  # another agent's commit changes nothing here
        run("add", "--all")
        run("commit", "-q", "-m", f"demo: stage orders-service {version} [skip ci]")
        return run("rev-parse", "HEAD")

    return stage


def test_the_fix_changes_one_line_of_code_since_production(repository):
    production = repository("1.27.0")
    repository("1.27.1")
    assert budget.changed_lines(production, "services/orders-service") == 1


def test_the_feature_is_over_the_limit_with_or_without_the_fix(repository):
    limit = service("orders-service")["error_budget"]["fix_max_changed_lines"]
    production = repository("1.27.0")
    repository("1.28.0")
    assert budget.changed_lines(production, "services/orders-service") > limit
    production = repository("1.27.1")
    repository("1.28.1")
    assert budget.changed_lines(production, "services/orders-service") > limit


def test_a_commit_git_doesnt_have_cant_be_read(repository):
    repository("1.27.0")
    assert budget.changed_lines("f" * 40, "services/orders-service") is None
    assert budget.changed_lines("local", "services/orders-service") is None


def test_1_28_1_is_1_27_1_plus_the_same_feature(monkeypatch):
    """1.28.1 and 1.28.0 differ by the one fixed line, as 1.27.1 and 1.27.0 do."""
    def differences(old, new):
        import difflib

        lines = lambda version: (STATES / version / "handler.py").read_text().splitlines()
        return [line for line in difflib.unified_diff(lines(old), lines(new), lineterm="", n=0)
                if line[:1] in "+-" and not line.startswith(("+++", "---"))]

    assert differences("1.27.0", "1.27.1") == differences("1.28.0", "1.28.1") == [
        '-    name = name.split(",")[0].strip()',
        '+    name = name if name in PRICES else name.split(",")[0].strip()']
