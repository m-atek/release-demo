"""reset.py against fakes of Jenkins, ServiceNow, GitHub, JFrog, Jira, git and
Lambda: it cleans up only what came after 1.3.0, a dry run changes nothing,
and a second run finds nothing to do."""

import json

import pytest

import change
import deploy
import jira
import reset

TAKE_TAGS = ["orders-service-v1.4.0"]


class World:
    """Everything reset.py looks at, as a take left it."""

    def __init__(self):
        self.writes = []
        self.tags = ["orders-service-v1.2.0", "orders-service-v1.3.0", "orders-service-v1.4.0", "store-sync-v3.3.0"]
        self.releases = [{"id": n, "name": tag.replace("-v", " "), "tag_name": tag} for n, tag in enumerate(self.tags)]
        self.jcr = ["1.2.9", "1.3.0", "1.4.0"]
        self.versions = [{"id": "101", "name": "orders-service 1.3.0", "released": True},
                         {"id": "102", "name": "orders-service 1.4.0", "released": True},
                         {"id": "103", "name": "orders-service 1.4.1", "released": False},
                         {"id": "104", "name": "store-sync 3.3.0", "released": True}]
        self.issues = {"ORD-11": "Done", "ORD-12": "Done", "ORD-13": "Backlog"}
        self.changes = [{"sys_id": "c1", "number": "CHG0030042", "type": "normal", "state": "-4",
                         "short_description": "Release orders-service 1.4.0 (ORD-12)"}]
        self.builds = [{"number": 41, "building": True, "displayName": "#41 orders-service 1.4.0",
                        "parameters": {"SERVICE": "orders-service", "JIRA_KEY": "ORD-12"}}]
        self.queued = []
        self.aliases = {"orders-service-staging": "9", "orders-service-production": "8"}
        self.lambda_versions = {"orders-service-staging": [("1", "orders-service in staging."), ("5", "1.3.0"),
                                                           ("7", "1.3.0"), ("9", "1.4.0")],
                                "orders-service-production": [("1", "orders-service in production."), ("6", "1.3.0"),
                                                              ("8", "1.4.0")]}
        self.main = "1.4.1"


class FakeJob:
    def __init__(self, world):
        self.world = world

    def builds(self, limit=50):
        return [dict(b) for b in self.world.builds]

    def queued(self):
        return list(self.world.queued)

    def build(self, number):
        return next(dict(b) for b in self.world.builds if b["number"] == number)

    def stop(self, number):
        self.world.writes.append(("stop", number))
        for build in self.world.builds:
            if build["number"] == number:
                build["building"] = False

    def cancel(self, queue_id):
        self.world.writes.append(("cancel", queue_id))
        self.world.queued = [q for q in self.world.queued if q["id"] != queue_id]


class FakeServiceNow:
    def __init__(self, world):
        self.world = world

    def find(self, table, query, fields=None):
        assert table == "cmdb_ci_appl" and query == "name=orders-service"
        return {"sys_id": "ci1"}

    def call(self, method, path, body=None, **query):
        if method == "GET":
            assert "cmdb_ci=ci1^active=true" in query["sysparm_query"]
            return [dict(c) for c in self.world.changes]
        self.world.writes.append((method, path, body["state"]))
        sys_id = path.rsplit("/", 1)[1]
        self.world.changes = [c for c in self.world.changes if c["sys_id"] != sys_id]
        return {}


class FakeJira:
    world = None

    def call(self, method, path, body=None):
        if method == "GET" and path.endswith("/versions"):
            return [dict(v) for v in self.world.versions]
        if method == "GET" and "/search/jql" in path:
            keys = [k for k in self.world.issues if k in ("ORD-12", "ORD-13")]
            return {"issues": [{"key": k, "fields": {"status": {"name": self.world.issues[k], "statusCategory": {
                "key": "done" if self.world.issues[k] == "Done" else "new"}}}} for k in keys]}
        if method == "PUT":
            self.world.writes.append((method, path, body))
            version_id = path.rsplit("/", 1)[1]
            for version in self.world.versions:
                if version["id"] == version_id:
                    version["released"] = body["released"]
            return None
        raise AssertionError(path)

    def transition(self, key, target):
        if target != "Backlog":
            return False
        self.world.writes.append(("transition", key, target))
        self.world.issues[key] = target
        return True


class FakePaginator:
    def __init__(self, world):
        self.world = world

    def paginate(self, FunctionName):
        yield {"Versions": [{"Version": "$LATEST", "Description": ""}] + [
            {"Version": number, "Description": text} for number, text in self.world.lambda_versions[FunctionName]]}


class FakeLambda:
    def __init__(self, world):
        self.world = world

    def get_paginator(self, name):
        return FakePaginator(self.world)

    def get_alias(self, FunctionName, Name):
        return {"FunctionVersion": self.world.aliases[FunctionName]}

    def update_alias(self, FunctionName, Name, FunctionVersion):
        self.world.writes.append(("alias", FunctionName, FunctionVersion))
        self.world.aliases[FunctionName] = FunctionVersion

    def get_function_url_config(self, FunctionName, Qualifier):
        return {"FunctionUrl": f"https://{FunctionName}.example/"}


@pytest.fixture
def world(monkeypatch):
    world = World()

    def github_api(method, path, token, body=None, scheme="Bearer"):
        if method == "GET" and "/releases?" in path:
            return [dict(r) for r in world.releases] if "page=1" in path else []
        if method == "GET" and "/matching-refs/tags/orders-service-v" in path:
            return [{"ref": f"refs/tags/{t}"} for t in world.tags if t.startswith("orders-service-v")]
        assert method == "DELETE", path
        world.writes.append((method, path))
        if "/releases/" in path:
            world.releases = [r for r in world.releases if str(r["id"]) != path.rsplit("/", 1)[1]]
        else:
            world.tags.remove(path.rsplit("/", 1)[1])
        return None

    def jcr_request(method, path, password):
        assert password == "jcr-admin"
        if method == "GET":
            body = {"children": [{"uri": f"/{v}", "folder": True} for v in world.jcr] + [{"uri": "/x.txt", "folder": False}]}
            return 200, json.dumps(body).encode()
        world.writes.append((method, path))
        world.jcr.remove(path.rsplit("/", 1)[1])
        return 204, b""

    def stage(service, version):
        world.writes.append(("stage", version))
        world.main = version

    FakeJira.world = world
    monkeypatch.setattr(reset.ReleaseJob, "as_release_bot", classmethod(lambda cls: FakeJob(world)))
    monkeypatch.setattr(change, "connect", lambda: (FakeServiceNow(world), None))
    monkeypatch.setattr(reset, "github_app_token", lambda ssm: ("token", "owner/release-demo", {}))
    monkeypatch.setattr(reset, "ssm_client", lambda: None)
    monkeypatch.setattr(reset, "get_parameter", lambda ssm, name: {"jcr/admin-password": "jcr-admin"}[name])
    monkeypatch.setattr(reset, "github_api", github_api)
    monkeypatch.setattr(reset, "jcr_request", jcr_request)
    monkeypatch.setattr(jira, "Jira", FakeJira)
    monkeypatch.setattr(reset.host, "stage", stage)
    monkeypatch.setattr(reset.host, "main_version", lambda path: world.main)
    monkeypatch.setattr(deploy, "lambda_client", lambda service: FakeLambda(world))
    monkeypatch.setattr(reset, "wait_for_version", lambda url, version, timeout: (True, version))
    monkeypatch.setattr(reset, "http_get", lambda url, timeout=5: (200, "<html>"))
    monkeypatch.setattr(reset.time, "sleep", lambda seconds: None)
    return world


def test_dry_run_prints_every_action_and_changes_nothing(world, capsys):
    assert reset.reset_main(dry_run=True) == 0
    assert world.writes == []
    out = capsys.readouterr().out
    for action in ["would stop release build #41", "would cancel CHG0030042", "would delete the GitHub release orders-service 1.4.0",
                   "would delete the tag orders-service-v1.4.0", "would delete orders-service-releases/1.4.0/",
                   "would un-release the Jira version orders-service 1.4.0", "would reopen ORD-12",
                   "would stage orders-service 1.3.0 on main (main has 1.4.1)",
                   "would move orders-service-staging live from version 9 to 7 (1.3.0)",
                   "would move orders-service-production live from version 8 to 6 (1.3.0)",
                   "would call orders-service-production once"]:
        assert action in out
    assert "Dry run" in out and "10 action(s) to take" in out


def test_cleans_up_only_what_came_after_the_baseline(world):
    assert reset.reset_main(dry_run=False) == 0
    assert world.tags == ["orders-service-v1.2.0", "orders-service-v1.3.0", "store-sync-v3.3.0"]
    assert [r["tag_name"] for r in world.releases] == world.tags
    assert world.jcr == ["1.2.9", "1.3.0"]
    assert [v["released"] for v in world.versions] == [True, False, False, True]
    assert world.issues == {"ORD-11": "Done", "ORD-12": "Backlog", "ORD-13": "Backlog"}
    assert world.changes == []
    assert ("PATCH", "/api/sn_chg_rest/change/normal/c1", "4") in world.writes
    assert world.main == "1.3.0"
    assert world.aliases == {"orders-service-staging": "7", "orders-service-production": "6"}
    assert ("stop", 41) in world.writes


def test_a_second_run_finds_nothing_to_do(world, capsys):
    reset.reset_main(dry_run=False)
    world.writes.clear()
    capsys.readouterr()
    assert reset.reset_main(dry_run=False) == 0
    assert world.writes == [("stage", "1.3.0")]  # stage.py itself finds nothing to commit
    assert "1 action(s)" in capsys.readouterr().out


def test_refuses_while_history_runs(world, capsys):
    world.builds = [{"number": 7, "building": True, "displayName": "#7 orders-service 1.2.4",
                     "parameters": {"SERVICE": "orders-service", "JIRA_KEY": "ORD-5"}}]
    assert reset.reset_main(dry_run=False) == 1
    assert world.writes == []
    assert "history.py is running" in capsys.readouterr().out


def test_cancels_queued_runs(world):
    world.queued = [{"id": 77, "parameters": {"JIRA_KEY": "ORD-13"}}]
    reset.reset_main(dry_run=False)
    assert ("cancel", 77) in world.writes


def test_a_failing_step_doesnt_stop_the_others(world, monkeypatch, capsys):
    monkeypatch.setattr(change, "connect", lambda: reset.fail("ServiceNow is asleep: wake it at developer.servicenow.com"))
    assert reset.reset_main(dry_run=False) == 1
    assert world.main == "1.3.0" and world.tags.count("orders-service-v1.4.0") == 0
    assert "these steps failed: 2. Open changes in ServiceNow" in capsys.readouterr().out


def test_needs_a_lambda_version_of_the_baseline(world, capsys):
    world.lambda_versions["orders-service-production"] = [("1", "orders-service in production."), ("8", "1.4.0")]
    assert reset.reset_main(dry_run=False) == 1
    assert "Run history.py first" in capsys.readouterr().err


@pytest.mark.parametrize("text,key", [("1.4.0", (1, 4, 0)), ("1.10.2", (1, 10, 2)), ("1.4", None), ("v1.4.0", None), ("", None)])
def test_version_key(text, key):
    assert reset.version_key(text) == key


def test_after_baseline_compares_numbers_not_text():
    assert reset.after_baseline("1.10.0") and reset.after_baseline("1.4.0")
    assert not reset.after_baseline("1.3.0") and not reset.after_baseline("1.2.9") and not reset.after_baseline("latest")


def test_other_demos_run_their_own_reset(tmp_path, monkeypatch, capfd):
    script = tmp_path / "scripts" / "demos" / "windows" / "reset.py"
    script.parent.mkdir(parents=True)
    script.write_text("import sys\nprint('windows reset', sys.argv[1:])\nsys.exit(3)\n")
    (tmp_path / "scripts" / "demos" / "terraform").mkdir()
    monkeypatch.setattr(reset, "ROOT", tmp_path)
    monkeypatch.setattr("sys.argv", ["reset.py", "windows", "--dry-run", "--here", "--waves", "2"])
    assert reset.main() == 3
    assert "windows reset ['--dry-run', '--waves', '2']" in capfd.readouterr().out
    monkeypatch.setattr("sys.argv", ["reset.py", "terraform"])
    assert reset.main() == 0
    monkeypatch.setattr("sys.argv", ["reset.py", "nope"])
    with pytest.raises(SystemExit):
        reset.main()


def test_main_hands_itself_to_the_tools_host_from_the_mac(monkeypatch):
    calls = []
    monkeypatch.setattr(reset.host, "on_host", lambda: False)
    monkeypatch.setattr(reset.host, "hand_over", lambda *args, **kwargs: calls.append((args, kwargs)) or 0)
    monkeypatch.setattr("sys.argv", ["reset.py", "--dry-run"])
    assert reset.main() == 0
    assert calls == [(("reset", "reset.py", ["main", "--dry-run"]), {"follow_only": False})]
