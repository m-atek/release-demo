"""Demo 20: the migration check, the rollback check, the deploy's migrations, the
switch and the demo states, against fakes for Lambda, JFrog, GitHub and the
database, and the real squawk where it is installed.

    .venv/bin/python -m pytest -q scripts/demos/rollback-trap/tests
"""

import base64
import importlib.util
import io
import json
import os
import re
import shutil
import sys
import zipfile
from pathlib import Path

import pytest

import checks
import database
import deploy
import migration_check
import rollback_check
import run_state
import trap

ROOT = trap.ROOT
STATES = ROOT / "demo-states" / "orders-service"
SQUAWK = os.environ.get("SQUAWK") or shutil.which("squawk") or str(Path(sys.executable).with_name("squawk"))
needs_squawk = pytest.mark.skipif(not Path(SQUAWK).exists(), reason="squawk isn't installed here")


def migrations(version):
    return trap.folder_migrations(STATES / version)


def package(files):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr("handler.py", "")
        for name, content in files.items():
            archive.writestr(f"migrations/{name}", content)
    return data.getvalue()


# The SQL helpers.

def test_statements_keep_function_bodies_and_quotes_whole():
    sql = migrations("1.7.0")["0003_add_delivery_estimate_days.sql"].decode()
    found = trap.statements(sql)
    assert [trap.effect(s) for s in found] == ["none", "none", "add", "add", "add", "add"]
    assert found[3].startswith("CREATE OR REPLACE FUNCTION") and "RETURN NEW; END; $$" in found[3]
    assert trap.statements("INSERT INTO t VALUES ('a;b', 'it''s');\n-- a; comment\nSELECT 1") == [
        "INSERT INTO t VALUES ('a;b', 'it''s')", "SELECT 1"]


def test_only_additions_or_not():
    assert trap.only_add(migrations("1.7.0") | {})
    assert not trap.only_add({"0003": migrations("1.6.1")["0003_rename_delivery_days.sql"]})
    assert not trap.only_add({"0004": migrations("1.7.1")["0004_drop_delivery_days.sql"]})
    assert trap.effect("ALTER TABLE orders ADD COLUMN a bigint, DROP COLUMN b") == "change"
    assert trap.effect("GRANT SELECT ON orders TO orders_app") == "none"


def test_ignore_comments_name_their_rules():
    assert trap.ignored_rules(migrations("1.6.2")["0003_rename_delivery_days.sql"].decode()) == ["renaming-column"]
    assert trap.ignored_rules(migrations("1.7.1")["0004_drop_delivery_days.sql"].decode()) == [
        "ban-drop-function", "ban-drop-column"]
    assert trap.ignored_rules(migrations("1.6.1")["0003_rename_delivery_days.sql"].decode()) == []


# The migration check, with squawk itself.

@pytest.fixture
def squawk_here(monkeypatch):
    monkeypatch.setenv("SQUAWK", SQUAWK)


@needs_squawk
def test_the_rename_stops_the_release_by_name(squawk_here, told, capsys):
    assert migration_check.check(migrations("1.6.1"), migrations("1.6.0"), "1.6.0") == 1
    assert told == ["Migration check failed: renaming-column in 0003_rename_delivery_days.sql."]
    out = capsys.readouterr().out
    assert "FAIL 0003_rename_delivery_days.sql:5:3: renaming-column: Renaming a column may break existing clients." in out


@needs_squawk
def test_a_silenced_rename_passes_and_says_so(squawk_here, told, capsys):
    assert migration_check.check(migrations("1.6.2"), migrations("1.6.0"), "1.6.0") == 0
    assert told == []
    assert "squawk-ignore renaming-column in 0003_rename_delivery_days.sql" in capsys.readouterr().out
    assert run_state.load()["migrations"] == {"base_version": "1.6.0", "new": ["0003_rename_delivery_days.sql"],
                                              "only_add": False, "findings": 0}


@needs_squawk
def test_the_expand_step_passes_with_no_ignore_comment(squawk_here, told, capsys):
    assert migration_check.check(migrations("1.7.0"), migrations("1.6.0"), "1.6.0") == 0
    assert "squawk-ignore" not in capsys.readouterr().out
    assert run_state.load()["migrations"]["only_add"] is True


@needs_squawk
def test_the_contract_step_passes_with_its_ignore_comments(squawk_here, told, capsys):
    assert migration_check.check(migrations("1.7.1"), migrations("1.7.0"), "1.7.0") == 0
    out = capsys.readouterr().out
    assert "squawk-ignore ban-drop-column in 0004_drop_delivery_days.sql" in out
    assert run_state.load()["migrations"]["only_add"] is False


@needs_squawk
def test_1_6_0s_own_migrations_pass_against_a_version_without_any(squawk_here, told):
    assert migration_check.check(migrations("1.6.0"), {}, "1.4.0") == 0


def test_a_migration_production_ran_and_that_changed_fails_by_name(told):
    edited = migrations("1.7.0") | {"0002_seed_orders.sql": b"INSERT INTO orders VALUES (1);\n"}
    assert migration_check.check(edited, migrations("1.6.0"), "1.6.0") == 1
    assert told == ["Migration check failed: 0002_seed_orders.sql changed after production ran it."]


def test_nothing_new_passes_without_squawk(told, monkeypatch):
    monkeypatch.setattr(migration_check, "squawk", lambda files: pytest.fail("squawk ran"))
    assert migration_check.check(migrations("1.6.0"), migrations("1.6.0"), "1.6.0") == 0


def test_several_findings_read_as_one_line():
    found = [{"file": "0003_a.sql", "rule_name": "renaming-column"}, {"file": "0003_a.sql", "rule_name": "x-rule"},
             {"file": "0004_b.sql", "rule_name": "ban-drop-column"}]
    assert migration_check.summary(found) == "renaming-column, x-rule in 0003_a.sql, ban-drop-column in 0004_b.sql"


def test_every_version_carries_the_same_first_two_migrations():
    """The check would call any edit of 0001 or 0002 a changed migration."""
    first = migrations("1.6.0")
    for version in ("1.6.1", "1.6.2", "1.7.0", "1.7.1"):
        assert {k: v for k, v in migrations(version).items() if k < "0003"} == first, version


def test_the_files_the_video_shows_fit_its_terminal():
    for version, name in (("1.6.1", "0003_rename_delivery_days.sql"), ("1.6.2", "0003_rename_delivery_days.sql"),
                          ("1.7.0", "0003_add_delivery_estimate_days.sql")):
        lines = migrations(version)[name].decode().splitlines()
        assert max(len(line) for line in lines) <= 56, (version, name)
    rename = migrations("1.6.1")["0003_rename_delivery_days.sql"].decode()
    assert "ALTER TABLE orders\n  RENAME COLUMN delivery_days TO delivery_estimate_days;\n" in rename
    assert "  -- squawk-ignore renaming-column\n  RENAME COLUMN" in migrations("1.6.2")["0003_rename_delivery_days.sql"].decode()


# The demo states, against a database that knows its columns.

class Schema:
    """A database that answers SELECTs on the orders table only for columns it has."""

    def __init__(self, columns):
        self.columns = set(columns)

    def run(self, sql):
        selected = [c.strip() for c in re.match(r"SELECT (.*) FROM orders", sql).group(1).split(",")]
        for column in selected:
            if column not in self.columns:
                error = Exception({"S": "ERROR", "M": f'column "{column}" does not exist'})
                raise error
        return [["26-1001", "Utrecht Centraal", ["Running shoes"], 89.95, "packed", 2]]


BASE = {"id", "store", "items", "total_eur", "status"}
SCHEMAS = {"baseline": BASE | {"delivery_days"}, "renamed": BASE | {"delivery_estimate_days"},
           "expanded": BASE | {"delivery_days", "delivery_estimate_days"}, "contracted": BASE | {"delivery_estimate_days"}}


def handler(version, schema, monkeypatch):
    spec = importlib.util.spec_from_file_location(f"handler_{version.replace('.', '_')}", STATES / version / "handler.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._database.update(url="postgresql://db.example/orders", connection=Schema(SCHEMAS[schema]))
    monkeypatch.setenv("DEPLOYED_AT", "2 Nov 2026, 13:00 UTC")
    return module


def answers(module, path):
    try:
        return module.handler({"rawPath": path, "requestContext": {"http": {"method": "GET", "path": path}}}, None)["statusCode"]
    except Exception:
        return 502


@pytest.mark.parametrize("version,schema,page,api", [
    ("1.6.0", "baseline", 200, 200),
    ("1.6.1", "renamed", 200, 502),     # take A: production's check fails on /api/orders
    ("1.6.0", "renamed", 502, 502),     # and the rollback's check fails too
    ("1.6.0", "expanded", 200, 200),    # take D: 1.6.0 still runs after 1.7.0's migration
    ("1.7.0", "expanded", 200, 200),
    ("1.7.0", "contracted", 200, 200),  # 1.7.1's drop leaves 1.7.0 running
    ("1.6.0", "contracted", 502, 502),
])
def test_each_version_against_each_schema(version, schema, page, api, monkeypatch, capsys):
    module = handler(version, schema, monkeypatch)
    assert answers(module, "/health") == 200
    assert (answers(module, "/"), answers(module, "/api/orders")) == (page, api)
    if api == 502:
        assert f'{version} database error: column "delivery_days" does not exist' in capsys.readouterr().out


def test_the_queries_the_video_greps_for():
    def lines(version):
        return [line for line in (STATES / version / "handler.py").read_text().splitlines() if "delivery_days" in line]

    assert lines("1.6.0") == ['PAGE_SQL = SELECT + "delivery_days" + FROM', 'API_SQL = SELECT + "delivery_days" + FROM']
    assert lines("1.6.1") == lines("1.6.2") == ['API_SQL = SELECT + "delivery_days" + FROM']
    assert lines("1.7.0") == lines("1.7.1") == []
    assert all(len(f"handler.py:28:{line}") <= 56 for line in lines("1.6.0"))


def test_the_states_vendor_a_pinned_driver_and_carry_no_disclaimer():
    for version in ("1.6.0", "1.6.1", "1.6.2", "1.7.0", "1.7.1"):
        assert (STATES / version / "VERSION").read_text().strip() == version
        pins = [line for line in (STATES / version / "requirements.txt").read_text().splitlines()
                if line and not line.startswith("#")]
        assert pins and all("==" in pin for pin in pins) and pins[0] == "pg8000==1.31.5"
        assert "Demo system" not in (STATES / version / "handler.py").read_text()


# The deploy's migrations, against a fake connection.

class FakeConnection:
    def __init__(self, applied=None, fail_on=None):
        self.applied, self.fail_on, self.ran = dict(applied or {}), fail_on, []

    def run(self, sql, **params):
        self.ran.append(sql)
        if sql.startswith("SELECT file, sha256"):
            return [[k, v] for k, v in self.applied.items()]
        if self.fail_on and self.fail_on in sql:
            raise Exception({"M": "syntax error"})
        if sql.startswith("INSERT INTO schema_migrations"):
            self.applied[params["file"]] = params["digest"]
        return []

    def close(self):
        pass


def test_migrate_applies_only_what_the_database_hasnt_run_in_order():
    files = migrations("1.7.0")
    connection = FakeConnection({name: trap.sha256(files[name]) for name in ("0001_orders.sql", "0002_seed_orders.sql")})
    said = []
    applied = database.migrate(database.Database(connection), files, "57", out=said.append, where="staging")
    assert applied == ["0003_add_delivery_estimate_days.sql"]
    assert said[0] == "staging has run 0001_orders.sql, 0002_seed_orders.sql"
    assert connection.ran[connection.ran.index("BEGIN") + 1] == files["0003_add_delivery_estimate_days.sql"].decode()
    assert connection.ran[-1] == "COMMIT"
    assert database.migrate(database.Database(connection), files, "58", out=said.append) == []


def test_migrate_refuses_a_file_that_changed_after_it_ran():
    files = migrations("1.7.0")
    connection = FakeConnection({"0002_seed_orders.sql": "0" * 64})
    with pytest.raises(database.ChangedMigration, match="0002_seed_orders.sql changed after"):
        database.migrate(database.Database(connection), files, "57")
    assert "BEGIN" not in connection.ran


def test_a_failed_migration_rolls_back_and_says_where():
    files = {"0001_x.sql": b"CREATE TABLE x (id bigint);"}
    connection = FakeConnection(fail_on="CREATE TABLE x")
    with pytest.raises(database.MigrationFailed, match="0001_x.sql failed in production: syntax error"):
        database.migrate(database.Database(connection), files, "9", where="production")
    assert connection.ran[-1] == "ROLLBACK"


def test_the_deploy_runs_migrations_first_and_then_the_shared_deploy(monkeypatch, tmp_path):
    files = migrations("1.6.1")
    calls = []
    monkeypatch.setattr(deploy, "exec_shared", lambda: calls.append("shared") or (_ for _ in ()).throw(SystemExit(0)))
    monkeypatch.setattr(trap, "config", lambda: {"dir": STATES / "1.6.1", "path": "services/orders-service",
                                                 "environments": trap.service("orders-service")["environments"]})
    monkeypatch.setattr(deploy, "migrate", lambda env, state: calls.append(("migrate", env)))
    monkeypatch.setattr(sys, "argv", ["deploy.py", "orders-service", "staging"])
    run_state.save({"package": {"path": "p", "sha256": "s"}, "build": {"number": "7"}})
    with pytest.raises(SystemExit):
        deploy.main()
    assert calls == [("migrate", "staging"), "shared"]
    assert files


def test_a_version_without_migrations_goes_straight_to_the_shared_deploy(monkeypatch):
    calls = []
    monkeypatch.setattr(deploy, "exec_shared", lambda: calls.append("shared") or (_ for _ in ()).throw(SystemExit(0)))
    monkeypatch.setattr(trap, "config", lambda: {"dir": STATES / "1.4.0"})
    monkeypatch.setattr(deploy, "migrate", lambda env, state: pytest.fail("migrated a version without migrations"))
    for argv in (["staging"], ["production", "--back"]):
        monkeypatch.setattr(sys, "argv", ["deploy.py", "orders-service", *argv])
        with pytest.raises(SystemExit):
            deploy.main()
    assert calls == ["shared", "shared"]


def test_going_back_says_the_database_stays(monkeypatch, capsys):
    monkeypatch.setattr(trap, "config", lambda: {"dir": STATES / "1.6.1"})
    monkeypatch.setattr(deploy, "run_shared", lambda: 0)
    monkeypatch.setattr(sys, "argv", ["deploy.py", "orders-service", "production", "--back"])
    assert deploy.main() == 0
    assert capsys.readouterr().out.strip() == deploy.DATABASE_STAYS


class FakeLambda:
    """orders-service-production's versions and the alias live."""

    def __init__(self, descriptions, live):
        self.descriptions, self.live, self.moved = descriptions, live, None

    def get_paginator(self, name):
        return self

    def paginate(self, FunctionName):
        return [{"Versions": [{"Version": "$LATEST", "Description": ""},
                              *({"Version": n, "Description": d} for n, d in self.descriptions.items())]}]

    def get_alias(self, FunctionName, Name):
        return {"FunctionVersion": self.live}

    def get_function_configuration(self, FunctionName, Qualifier):
        return {"Description": self.descriptions[Qualifier]}

    def update_alias(self, FunctionName, Name, FunctionVersion):
        self.moved = FunctionVersion

    def get_function_url_config(self, FunctionName, Qualifier):
        return {"FunctionUrl": "https://production.example/"}


def test_rollback_goes_to_the_last_release_not_to_a_rolled_back_take(monkeypatch, capsys):
    """Acceptance check 7: after 1.7.0, /rollback puts 1.6.0 back, not take A's 1.6.1."""
    client = FakeLambda({"20": "1.6.0", "21": "1.6.1", "22": "1.7.0"}, live="22")
    monkeypatch.setattr(deploy, "released", lambda app: app != "1.6.1")
    target, usual = deploy.previous_target(client, "orders-service-production", "22", "1.7.0")
    assert target == ("20", "1.6.0") and usual == ("21", "1.6.1")
    assert "version 21 (1.6.1) never reached a release" in capsys.readouterr().out

    monkeypatch.setattr(trap, "lambda_client", lambda: client)
    monkeypatch.setattr(deploy, "live_has_migrations", lambda app: True)
    deploy.previous("production")
    assert client.moved == "20"
    production = run_state.load()["deploys"]["production"]
    assert production == {"previous_number": "22", "previous_version": "1.7.0", "rolled_back_to": "1.6.0",
                          "rolled_back_number": "20", "url": "https://production.example/"}
    assert deploy.DATABASE_STAYS in capsys.readouterr().out


def test_rollback_without_jfrog_picks_as_the_shared_deploy_does(monkeypatch):
    client = FakeLambda({"20": "1.6.0", "21": "1.6.1", "22": "1.7.0"}, live="22")
    monkeypatch.setattr(deploy, "released", lambda app: None)
    assert deploy.previous_target(client, "f", "22", "1.7.0") == (("21", "1.6.1"), ("21", "1.6.1"))


# The rollback check.

class FakeInvoke(FakeLambda):
    def __init__(self, descriptions, failing=()):
        super().__init__(descriptions, live="1")
        self.failing, self.invoked = set(failing), []

    def invoke(self, FunctionName, Qualifier, LogType, Payload):
        path = json.loads(Payload)["rawPath"]
        self.invoked.append((FunctionName, Qualifier, path))
        if path in self.failing:
            log = '1.6.0 database error: column "delivery_days" does not exist\n'
            return {"FunctionError": "Unhandled", "LogResult": base64.b64encode(log.encode()).decode(),
                    "Payload": io.BytesIO(b'{"errorType": "DatabaseError"}')}
        body = json.dumps({"version": "1.6.0"}) if path == "/health" else "<html>"
        return {"Payload": io.BytesIO(json.dumps({"statusCode": 200, "body": body}).encode())}


def run_check(monkeypatch, client, only_add=True):
    monkeypatch.setattr(trap, "lambda_client", lambda: client)
    monkeypatch.setattr(trap, "live_version", lambda env, client=None: ("15", "1.6.0"))
    monkeypatch.setattr(trap, "clock", lambda at=None: "14:07:33")
    monkeypatch.setattr(sys, "argv", ["rollback_check.py", "orders-service"])
    run_state.save({"version": "1.7.0", "migrations": {"only_add": only_add}})
    return rollback_check.main()


def test_the_old_version_failing_against_the_new_schema_stops_the_release(monkeypatch, told, capsys):
    client = FakeInvoke({"30": "1.6.0", "31": "1.6.2"}, failing={"/", "/api/orders"})
    assert run_check(monkeypatch, client) == 1
    assert told == ["Rollback check failed: 1.6.0 fails against the new schema."]
    out = capsys.readouterr().out
    assert "ok   /health reports 1.6.0" in out
    assert 'FAIL / answered 502: 1.6.0 database error: column "delivery_days" does not exist' in out
    assert "Staging stays on 1.7.0" in out
    assert {qualifier for _, qualifier, _ in client.invoked} == {"30"}
    assert "change_extras" not in run_state.load()


def test_a_passing_rollback_check_writes_the_tested_backout_plan(monkeypatch, told):
    client = FakeInvoke({"30": "1.6.0", "29": "1.6.0", "31": "1.7.0"})
    assert run_check(monkeypatch, client) == 0
    assert told == ["Rollback check: 1.6.0 passes against the new schema."]
    plan = run_state.load()["change_extras"]["fields"]["backout_plan"]
    assert plan == ("Automatic: if the production check fails, the pipeline moves the live alias back to 1.6.0, checks "
                    "it, and closes this change as unsuccessful. Verified before this change: 1.6.0 answered /health, / "
                    "and /api/orders against this release's schema in staging at 14:07:33. The database isn't rolled "
                    "back; this release's migrations only add.")
    assert {qualifier for _, qualifier, _ in client.invoked} == {"30"}  # the newest staging version of 1.6.0


def test_the_only_add_sentence_goes_when_a_migration_drops(monkeypatch, told):
    assert run_check(monkeypatch, FakeInvoke({"40": "1.6.0"}), only_add=False) == 0
    assert run_state.load()["change_extras"]["fields"]["backout_plan"].endswith("in staging at 14:07:33.")


# The stages only some versions have, and the switch.

def test_the_two_stages_show_only_for_versions_with_migrations(tmp_path):
    with_checks = {"scripts": {"migration_check": "x", "rollback_check": "y"}}
    (tmp_path / "migrations").mkdir()
    stages = run_state.stages_for({**with_checks, "dir": tmp_path})
    assert stages.index("Migration check") == stages.index("SBOM and scan") + 1
    assert stages.index("Rollback check") == stages.index("Check staging") + 1
    both = run_state.stages_for({"scripts": {**with_checks["scripts"], "contract": "z"}, "dir": tmp_path})
    assert both.index("Migration check") == both.index("Contract check") + 1
    assert run_state.stages_for({**with_checks, "dir": STATES / "1.4.0"}) == run_state.STAGES
    assert run_state.stages_for({"scripts": {}, "dir": tmp_path}) == run_state.STAGES


def test_the_switch_adds_and_removes_two_lines_in_one_commit(monkeypatch):
    text = (ROOT / "services.yaml").read_text()
    puts = []

    def github(method, path, token, body=None, scheme="Bearer"):
        if method == "GET":
            return {"sha": "abc", "content": base64.b64encode(text.encode()).decode()}
        puts.append(body)
        return {}

    monkeypatch.setattr(checks, "github_api", github)
    monkeypatch.setattr(checks, "github_app_token", lambda ssm: ("t", "owner/release-demo", None))
    monkeypatch.setattr(checks, "ssm_client", lambda: None)
    assert checks.switch(True) == "the checks are on on main already"
    assert checks.switch(False) == "switched the checks off on main"
    (body,) = puts
    assert body["message"] == "demo: rollback-trap checks off [skip ci]"
    off = base64.b64decode(body["content"]).decode()
    assert not checks.is_on(off) and checks.switched(off, True) == text
