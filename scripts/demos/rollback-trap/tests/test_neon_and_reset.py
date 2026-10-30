"""Demo 20's database setup and reset, against fakes for Neon's API, GitHub and SSM."""

import importlib.util
import json

import pytest

import neon
import trap


def test_a_connection_string_for_another_role_keeps_host_and_options():
    uri = "postgresql://orders_owner:owner-pw@ep-cool-1-pooler.eu-west-2.aws.neon.tech/orders?sslmode=require&channel_binding=require"
    assert neon.with_user(uri, "orders_app", "a-b_c") == (
        "postgresql://orders_app:a-b_c@ep-cool-1-pooler.eu-west-2.aws.neon.tech/orders?sslmode=require&channel_binding=require")


class FakeNeon:
    def __init__(self):
        self.calls = []

    def call(self, method, path, body=None, **query):
        self.calls.append((method, path, body))
        return {"operations": [{"id": "op-1"}]}

    def wait(self, project_id, operations, timeout=300):
        self.calls.append(("wait", project_id, [op["id"] for op in operations]))


def test_restore_puts_staging_and_production_back_to_baseline(monkeypatch):
    fake = FakeNeon()
    monkeypatch.setattr(neon, "neon", lambda: fake)
    monkeypatch.setattr(neon, "project_ids", lambda: {"project_id": "p-1", "branches": {
        "baseline": "br-base", "production": "br-prod", "staging": "br-stage"}})
    said = []
    neon.restore(out=said.append)
    assert fake.calls == [
        ("POST", "/projects/p-1/branches/br-stage/restore", {"source_branch_id": "br-base"}), ("wait", "p-1", ["op-1"]),
        ("POST", "/projects/p-1/branches/br-prod/restore", {"source_branch_id": "br-base"}), ("wait", "p-1", ["op-1"])]
    assert said == ["  Neon branch staging: restored from baseline", "  Neon branch production: restored from baseline"]
    # No preserve_under_name: neither branch has children, so nothing moves and no backup branch is made.
    assert all("preserve_under_name" not in (body or {}) for _, _, body in fake.calls if isinstance(body, dict))


def test_a_personal_key_names_its_organization_to_find_and_make_the_project(monkeypatch, capsys):
    # Neon answers GET /projects with a personal API key and no org_id with 400
    # "org_id is required": setup asks for the key's organization first.
    calls = []

    def call(self, method, path, body=None, **query):
        calls.append((method, path, body, query))
        if path == "/users/me/organizations":
            return {"organizations": [{"id": "org-quiet-1", "name": "Mateusz"}]}
        if path == "/projects" and method == "GET":
            return {"projects": [{"name": "release-demo-orders-old"}]}
        return {"project": {"id": "p-1"}}

    monkeypatch.setattr(neon.Neon, "call", call)
    api = neon.Neon("key")
    org_id = api.organization()
    assert org_id == "org-quiet-1"
    assert api.project(org_id) is None
    assert calls[1] == ("GET", "/projects", None, {"search": "release-demo-orders", "org_id": "org-quiet-1"})

    monkeypatch.setattr(neon.Neon, "call", lambda self, method, path, body=None, **query: {"organizations": [
        {"id": "org-1", "name": "One"}, {"id": "org-2", "name": "Two"}]})
    with pytest.raises(SystemExit):
        api.organization()
    assert "reaches 2 organizations (One (org-1), Two (org-2)); setup needs exactly one." in capsys.readouterr().err


def test_setup_makes_the_project_in_that_organization(monkeypatch):
    made = []

    def call(self, method, path, body=None, **query):
        if path == "/users/me/organizations":
            return {"organizations": [{"id": "org-quiet-1", "name": "Mateusz"}]}
        if path == "/projects" and method == "GET":
            return {"projects": []}
        if path == "/projects" and method == "POST":
            made.append(body)
            raise RuntimeError("stop after the POST")
        raise AssertionError(path)

    monkeypatch.setattr(neon.Neon, "call", call)
    monkeypatch.setattr(neon, "neon", lambda: neon.Neon("key"))
    with pytest.raises(RuntimeError, match="stop after the POST"):
        neon.setup()
    assert made[0]["project"]["org_id"] == "org-quiet-1"
    assert made[0]["project"]["name"] == "release-demo-orders" and made[0]["project"]["region_id"] == "aws-eu-west-2"


def test_the_app_role_is_made_with_sql_and_keeps_one_password():
    ran = []

    class Connection:
        def __init__(self, exists):
            self.exists = exists

        def run(self, sql, **params):
            ran.append(sql)
            return [[1]] if self.exists and sql.startswith("SELECT") else []

    neon.app_role(Connection(False), "pw-1")
    neon.app_role(Connection(True), "pw-1")
    assert ran[1] == "CREATE ROLE orders_app WITH LOGIN PASSWORD 'pw-1'"
    assert ran[3] == "ALTER ROLE orders_app WITH LOGIN PASSWORD 'pw-1'"


def load_reset():
    spec = importlib.util.spec_from_file_location("trap_reset", trap.HERE / "reset.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_reset_points_the_main_resets_steps_at_1_6_0():
    reset = load_reset()
    shared = reset.shared_reset()
    assert shared.BASELINE == "1.6.0" and shared.SERVICE == "orders-service"
    assert shared.after_baseline("1.6.1") and not shared.after_baseline("1.6.0")
    # The shared reset's own deploy.py, not this folder's wrapper.
    import sys

    assert "deploy" not in sys.modules or sys.modules["deploy"].__file__.endswith("rollback-trap/deploy.py")


def test_the_reset_puts_back_the_1_6_0_tag_an_earlier_reset_archived(monkeypatch):
    reset = load_reset()
    made = []

    def github(method, path, token, body=None, scheme="Bearer"):
        if method == "GET" and path.endswith("tags/orders-service-v1.6.0"):
            return []
        if method == "GET":
            return [{"ref": "refs/tags/takes/orders-service-v1.6.0-20261101T090000Z", "object": {"sha": "a" * 40}},
                    {"ref": "refs/tags/takes/orders-service-v1.6.0-20261102T090000Z", "object": {"sha": "b" * 40}}]
        made.append((path, body))
        return {}

    monkeypatch.setattr(reset, "github_api", github)
    monkeypatch.setattr(reset, "github_app_token", lambda ssm: ("t", "owner/release-demo", None))
    monkeypatch.setattr(reset, "ssm_client", lambda: None)
    actions = reset.shared_reset().Reset(dry_run=False)
    reset.baseline_tag(actions)
    assert made == [("/repos/owner/release-demo/git/refs", {"ref": "refs/tags/orders-service-v1.6.0", "sha": "b" * 40})]


def test_the_reset_job_passes_the_demos_options():
    jenkinsfile = (trap.ROOT / "jenkins" / "reset.Jenkinsfile").read_text()
    assert "set -f; python3 scripts/reset.py \\\"\\$DEMO\\\" --here" in jenkinsfile and "\\$OPTIONS" in jenkinsfile
    jobs = (trap.ROOT / "jenkins" / "jobs" / "jobs.groovy").read_text()
    assert "stringParam('OPTIONS', ''" in jobs


@pytest.mark.parametrize("stage", ["1.6.1", "1.6.2", "1.7.0", "1.7.1"])
def test_every_stage_the_reset_offers_exists(stage):
    assert (trap.ROOT / "demo-states" / "orders-service" / stage / "VERSION").read_text().strip() == stage
    assert stage in load_reset().STAGES


def test_parameters_for_the_database_are_named_and_never_valued():
    import yaml

    catalog = yaml.safe_load((trap.ROOT / "infra" / "parameters.yaml").read_text())
    for name in ("neon/api-key", "orders-service/staging/database-url", "orders-service/production/database-url",
                 "orders-service/staging/migrate-url", "orders-service/production/migrate-url"):
        assert catalog[name]["secure"] is True, name
    assert catalog["neon/api-key"]["source"] == "you"
    assert json.dumps(catalog).count("postgresql://") == 0


@pytest.mark.parametrize("script", ["setup.py", "reset.py", "neon.py", "checks.py", "deploy.py", "migration_check.py",
                                    "rollback_check.py"])
def test_each_script_starts_as_its_callers_start_it(script):
    # scripts/reset.py runs this folder's reset.py as a script of its own, and the
    # tools host runs it with --here: with only this folder on the path, host and
    # common must come through trap's path. Before, reset.py and setup.py stopped
    # at "No module named 'host'".
    import subprocess
    import sys
    from pathlib import Path

    folder = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, str(folder / script), "--help"], cwd=folder.parents[2],
                            capture_output=True, text=True, env={"PATH": "/usr/bin:/bin", "HOME": "/tmp"})
    assert result.returncode == 0, result.stderr[-500:]
    assert result.stdout.startswith("usage: ")
