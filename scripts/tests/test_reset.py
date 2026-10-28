"""reset.py against fakes of Jenkins, ServiceNow, GitHub, JFrog, Jira, git and
Lambda, and the real Slack handler: it archives only what came after 1.3.0,
keeps the past, a dry run changes nothing, and a second run finds nothing to do.

    .venv/bin/python -m pytest -q scripts/tests
"""

import hashlib
import hmac
import importlib.util
import json
import sys
import urllib.parse
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import change  # noqa: E402
import deploy  # noqa: E402
import jira  # noqa: E402
import reset  # noqa: E402

STAMP = "20261028T090000Z"
SHA = {"1.2.0": "a" * 40, "1.3.0": "b" * 40, "1.4.0": "c" * 40, "1.4.1": "d" * 40}
SECRET = "test-signing-secret, not a real one"
MATEUSZ, SAM = "U0MATEUSZ01", "U0SAM000001"


class World:
    """Everything reset.py looks at, as history.py and one take left it."""

    def __init__(self):
        self.writes = []
        self.tags = {"orders-service-v1.2.0": SHA["1.2.0"], "orders-service-v1.3.0": SHA["1.3.0"],
                     "orders-service-v1.4.0": SHA["1.4.0"], "store-sync-v3.3.0": "e" * 40}
        self.releases = [{"id": n, "name": tag.replace("-v", " "), "tag_name": tag, "prerelease": False,
                          "target_commitish": sha} for n, (tag, sha) in enumerate(self.tags.items())]
        self.jcr = {f"orders-service-releases/{v}/{name}": f"{v} {name}".encode()
                    for v in ("1.2.9", "1.3.0", "1.4.0") for name in ("orders-service.zip", "build-info.json")}
        self.versions = [{"id": "101", "name": "orders-service 1.3.0", "released": True},
                         {"id": "102", "name": "orders-service 1.4.0", "released": True},
                         {"id": "103", "name": "orders-service 1.4.1", "released": False},
                         {"id": "104", "name": "store-sync 3.3.0", "released": True}]
        self.issues = {"ORD-11": "Done", "ORD-12": "Done", "ORD-13": "Backlog"}
        self.changes = [{"sys_id": "c1", "number": "CHG0030042", "type": "normal", "state": "-4",
                         "short_description": "Release orders-service 1.4.0 (ORD-12)"}]
        self.builds = {"release": [{"number": 41, "building": True, "displayName": "#41 orders-service 1.4.0",
                                    "parameters": {"SERVICE": "orders-service", "JIRA_KEY": "ORD-12"}}],
                       "rollback": []}
        self.queued = {"release": [], "rollback": []}
        self.aliases = {"orders-service-staging": "9", "orders-service-production": "8"}
        self.lambda_versions = {"orders-service-staging": [("1", "orders-service in staging."), ("5", "1.3.0"),
                                                           ("7", "1.3.0"), ("9", "1.4.0")],
                                "orders-service-production": [("1", "orders-service in production."), ("6", "1.3.0"),
                                                              ("8", "1.4.0")]}
        self.main = "1.4.1"
        self.slack_calls = 0


class FakeJob:
    def __init__(self, world, job):
        self.world, self.job = world, job

    def exists(self):
        return True

    def builds(self, limit=50):
        return [dict(b) for b in self.world.builds[self.job]]

    def queued(self):
        return list(self.world.queued[self.job])

    def build(self, number):
        return next(dict(b) for b in self.world.builds[self.job] if b["number"] == number)

    def stop(self, number):
        self.world.writes.append(("stop", self.job, number))
        for build in self.world.builds[self.job]:
            if build["number"] == number:
                build["building"] = False

    def cancel(self, queue_id):
        self.world.writes.append(("cancel", self.job, queue_id))
        self.world.queued[self.job] = [q for q in self.world.queued[self.job] if q["id"] != queue_id]


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
        self.world.changes = [c for c in self.world.changes if c["sys_id"] != path.rsplit("/", 1)[1]]
        return {}


class FakeJira:
    world = None

    def call(self, method, path, body=None):
        if method == "GET" and path.endswith("/versions"):
            return [dict(v) for v in self.world.versions]
        if method == "GET" and "/search/jql" in path:
            jql = urllib.parse.unquote(path.split("jql=", 1)[1].split("&", 1)[0])
            assert "key in (ORD-12, ORD-13)" in jql
            return {"issues": [{"key": k, "fields": {"status": {"name": self.world.issues[k], "statusCategory": {
                "key": "done" if self.world.issues[k] == "Done" else "new"}}}} for k in ("ORD-12", "ORD-13")]}
        assert method == "PUT", path
        self.world.writes.append((method, path, body))
        for version in self.world.versions:
            if version["id"] == path.rsplit("/", 1)[1]:
                version["released"] = body["released"]

    def transition(self, key, target):
        if target != "Backlog":
            return False
        self.world.writes.append(("transition", key, target))
        self.world.issues[key] = target
        return True


class FakeLambda:
    def __init__(self, world):
        self.world = world

    def get_paginator(self, name):
        world = self.world
        return SimpleNamespace(paginate=lambda FunctionName: iter([{"Versions": [{"Version": "$LATEST", "Description": ""}] + [
            {"Version": number, "Description": text} for number, text in world.lambda_versions[FunctionName]]}]))

    def get_alias(self, FunctionName, Name):
        return {"FunctionVersion": self.world.aliases[FunctionName]}

    def update_alias(self, FunctionName, Name, FunctionVersion):
        self.world.writes.append(("alias", FunctionName, FunctionVersion))
        self.world.aliases[FunctionName] = FunctionVersion

    def get_function_url_config(self, FunctionName, Qualifier):
        return {"FunctionUrl": f"https://{FunctionName}.example/"}


def slack_handler(world, monkeypatch):
    """The real Slack handler, with SSM and Lambda faked: every warm-up request goes through it."""
    spec = importlib.util.spec_from_file_location("slack_handler_for_reset", reset.ROOT / "slack" / "handler.py")
    app = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app)
    monkeypatch.setenv("RELEASERS", "sam")
    monkeypatch.setattr(app, "parameters", lambda *names: {n: {"slack/signing-secret": SECRET,
                                                                "slack/member-id/sam": SAM}.get(n) for n in names})
    monkeypatch.setattr(app, "aws", lambda name: pytest.fail("the warm-up must start nothing"))
    monkeypatch.setattr(app, "log", lambda message: None)

    class Response:
        def __init__(self, reply):
            self.status, self.body = reply["statusCode"], reply["body"].encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return self.body

    def urlopen(request, timeout=None):
        world.slack_calls += 1
        event = {"body": request.data.decode(), "isBase64Encoded": False, "headers": dict(request.header_items())}
        return Response(app.handler(event, SimpleNamespace(invoked_function_arn="arn:test")))

    return urlopen


@pytest.fixture
def world(monkeypatch):
    world = World()

    def github_api(method, path, token, body=None, scheme="Bearer"):
        if method == "GET" and "/releases?" in path:
            return [dict(r) for r in world.releases] if "page=1" in path else []
        if method == "GET" and "/matching-refs/tags/orders-service-v" in path:
            return [{"ref": f"refs/tags/{t}", "object": {"sha": sha}} for t, sha in world.tags.items()
                    if t.startswith("orders-service-v")]
        world.writes.append((method, path, body))
        if method == "POST" and path.endswith("/git/refs"):
            tag = body["ref"].removeprefix("refs/tags/")
            assert tag not in world.tags
            world.tags[tag] = body["sha"]
        elif method == "PATCH":
            release = next(r for r in world.releases if str(r["id"]) == path.rsplit("/", 1)[1])
            assert body["tag_name"] in world.tags, "a release moves only onto a tag that exists"
            release.update({k: body[k] for k in ("tag_name", "name", "prerelease")})
        elif method == "DELETE" and "/git/refs/tags/" in path:
            tag = path.split("/git/refs/tags/", 1)[1]
            assert all(r["tag_name"] != tag for r in world.releases), "no release may lose its tag"
            del world.tags[tag]
        else:
            raise AssertionError((method, path))

    def jcr_request(method, path, password, data=None, headers=None):
        assert password == "jcr-admin"
        if method == "GET" and path.startswith("api/storage/"):
            folder = path.removeprefix("api/storage/")
            if folder in world.jcr:
                return 200, json.dumps({"checksums": {"sha256": hashlib.sha256(world.jcr[folder]).hexdigest()}}).encode()
            names = {p[len(folder) + 1:].split("/", 1)[0]: "/" in p[len(folder) + 1:]
                     for p in world.jcr if p.startswith(folder + "/")}
            return 200, json.dumps({"children": [{"uri": f"/{n}", "folder": f} for n, f in sorted(names.items())]}).encode()
        if method == "GET":
            return 200, world.jcr[path]
        world.writes.append((method, path))
        if method == "PUT":
            assert headers["X-Checksum-Sha256"] == hashlib.sha256(data).hexdigest()
            world.jcr[path] = data
            return 201, b""
        assert method == "DELETE"
        for key in [p for p in world.jcr if p.startswith(path + "/")]:
            del world.jcr[key]
        return 204, b""

    def stage(service, version):
        world.writes.append(("stage", version))
        world.main = version

    FakeJira.world = world
    monkeypatch.setattr(reset.ReleaseJob, "as_release_bot", classmethod(lambda cls, job="release": FakeJob(world, job)))
    monkeypatch.setattr(change, "connect", lambda: (FakeServiceNow(world), None))
    monkeypatch.setattr(reset, "github_app_token", lambda ssm: ("token", "owner/release-demo", {}))
    monkeypatch.setattr(reset, "ssm_client", lambda: None)
    values = {"jcr/admin-password": "jcr-admin", "slack/signing-secret": SECRET, "slack/member-id/mateusz": MATEUSZ}
    monkeypatch.setattr(reset, "get_parameter", lambda ssm, name: values[name])
    monkeypatch.setattr(reset, "github_api", github_api)
    monkeypatch.setattr(reset, "jcr_request", jcr_request)
    monkeypatch.setattr(jira, "Jira", FakeJira)
    monkeypatch.setattr(reset.host, "stage", stage)
    monkeypatch.setattr(reset.host, "main_version", lambda path: world.main)
    monkeypatch.setattr(reset.host, "unit_running", lambda name: False)
    monkeypatch.setattr(deploy, "lambda_client", lambda service: FakeLambda(world))
    monkeypatch.setattr(reset, "wait_for_version", lambda url, version, timeout: (True, version))
    monkeypatch.setattr(reset, "http_get", lambda url, timeout=5: (200, "<html>"))
    monkeypatch.setattr(reset.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(reset.time, "strftime", lambda fmt, t=None: STAMP)
    monkeypatch.setattr(reset.urllib.request, "urlopen", slack_handler(world, monkeypatch))
    return world


def run(dry_run=False):
    return reset.reset_main(dry_run, slack_url="https://slack-handler.example/")


def test_dry_run_prints_every_action_and_changes_nothing(world, capsys):
    assert run(dry_run=True) == 0
    assert world.writes == [] and world.slack_calls == 0
    out = capsys.readouterr().out
    for action in ["would stop release build #41", "would cancel CHG0030042",
                   f"would tag ccccccc as takes/orders-service-v1.4.0-{STAMP}",
                   f"would move the GitHub release orders-service 1.4.0 onto takes/orders-service-v1.4.0-{STAMP}, "
                   'as the pre-release "orders-service 1.4.0 (earlier take)"',
                   "would remove the tag orders-service-v1.4.0",
                   f"would archive orders-service-releases/1.4.0/ as orders-service-releases/takes/1.4.0-{STAMP}/",
                   "would un-release the Jira version orders-service 1.4.0", "would reopen ORD-12",
                   "would stage orders-service 1.3.0 on main (main has 1.4.1)",
                   "would move orders-service-staging live from version 9 to 7 (1.3.0)",
                   "would move orders-service-production live from version 8 to 6 (1.3.0)",
                   "would call orders-service-production once",
                   "would call the Slack handler once as Mateusz"]:
        assert action in out
    assert "11 action(s) to take" in out


def test_archives_the_take_and_keeps_the_past(world, capsys):
    assert run() == 0
    archive = f"takes/orders-service-v1.4.0-{STAMP}"
    assert world.tags == {"orders-service-v1.2.0": SHA["1.2.0"], "orders-service-v1.3.0": SHA["1.3.0"],
                          "store-sync-v3.3.0": "e" * 40, archive: SHA["1.4.0"]}
    moved = next(r for r in world.releases if r["tag_name"] == archive)
    assert moved["name"] == "orders-service 1.4.0 (earlier take)" and moved["prerelease"] is True
    assert sum(1 for r in world.releases if r["prerelease"]) == 1 and len(world.releases) == 4
    kept = [f"orders-service-releases/{v}/{n}" for v in ("1.2.9", "1.3.0") for n in ("orders-service.zip", "build-info.json")]
    archived = [f"orders-service-releases/takes/1.4.0-{STAMP}/{n}" for n in ("orders-service.zip", "build-info.json")]
    assert sorted(world.jcr) == sorted(kept + archived)
    assert world.jcr[archived[1]] == b"1.4.0 build-info.json"
    assert [v["released"] for v in world.versions] == [True, False, False, True]
    assert world.issues == {"ORD-11": "Done", "ORD-12": "Backlog", "ORD-13": "Backlog"}
    assert world.changes == [] and ("PATCH", "/api/sn_chg_rest/change/normal/c1", "4") in world.writes
    assert world.main == "1.3.0"
    assert world.aliases == {"orders-service-staging": "7", "orders-service-production": "6"}
    assert ("stop", "release", 41) in world.writes
    assert world.slack_calls == 1
    assert "the Slack handler answered \"You can't start releases here.\"" in capsys.readouterr().out


def test_the_tag_is_archived_before_its_name_is_freed(world):
    run()
    github = [w[0] for w in world.writes if len(w) == 3 and isinstance(w[1], str) and w[1].startswith("/repos/")]
    assert github == ["POST", "PATCH", "DELETE"]


def test_a_second_run_finds_nothing_to_do(world, capsys):
    run()
    world.writes.clear()
    capsys.readouterr()
    assert run() == 0
    assert world.writes == [("stage", "1.3.0")]  # stage.py itself finds nothing to commit
    assert "1 action(s)" in capsys.readouterr().out


def test_a_release_left_without_its_tag_is_archived_on_its_commit(world):
    """A reset cut short after freeing the tag's name but before moving its release."""
    del world.tags["orders-service-v1.4.0"]
    assert run() == 0
    archive = f"takes/orders-service-v1.4.0-{STAMP}"
    assert world.tags[archive] == SHA["1.4.0"]
    assert next(r for r in world.releases if r["id"] == 2)["tag_name"] == archive


def test_jfrog_keeps_the_original_when_the_copy_doesnt_match(world, monkeypatch):
    faked = reset.jcr_request

    def corrupting(method, path, password, data=None, headers=None):
        if method == "GET" and path.startswith("api/storage/") and "/takes/" in path:
            return 200, json.dumps({"checksums": {"sha256": "0" * 64}}).encode()
        return faked(method, path, password, data, headers)

    monkeypatch.setattr(reset, "jcr_request", corrupting)
    assert run() == 1
    assert "orders-service-releases/1.4.0/orders-service.zip" in world.jcr
    assert not any(w[0] == "DELETE" for w in world.writes if len(w) == 2)


def test_refuses_while_history_runs(world, capsys):
    world.builds["release"] = [{"number": 7, "building": True, "displayName": "#7 orders-service 1.2.4",
                                "parameters": {"SERVICE": "orders-service", "JIRA_KEY": "ORD-5"}}]
    assert run() == 1
    assert world.writes == [] and "history.py is running" in capsys.readouterr().out


def test_refuses_while_the_history_unit_runs_between_two_releases(world, monkeypatch):
    monkeypatch.setattr(reset.host, "unit_running", lambda name: name == "history")
    world.builds["release"] = []
    assert run() == 1
    assert world.writes == []


def test_stops_rollback_runs_and_cancels_queued_ones(world):
    world.builds["rollback"] = [{"number": 3, "building": True, "displayName": "#3 orders-service",
                                 "parameters": {"SERVICE": "orders-service"}}]
    world.queued["release"] = [{"id": 77, "parameters": {"JIRA_KEY": "ORD-13"}}]
    run()
    assert ("stop", "rollback", 3) in world.writes and ("cancel", "release", 77) in world.writes


def test_a_failing_step_doesnt_stop_the_others(world, monkeypatch, capsys):
    monkeypatch.setattr(change, "connect", lambda: reset.fail("ServiceNow is asleep: wake it at developer.servicenow.com"))
    assert run() == 1
    assert world.main == "1.3.0" and "orders-service-v1.4.0" not in world.tags
    assert "these steps failed: 2. Open changes in ServiceNow" in capsys.readouterr().out


def test_needs_a_lambda_version_of_the_baseline(world, capsys):
    world.lambda_versions["orders-service-production"] = [("1", "orders-service in production."), ("8", "1.4.0")]
    assert run() == 1
    assert "Run history.py first" in capsys.readouterr().err


def test_without_the_slack_handlers_url_the_warm_up_is_skipped(world, monkeypatch, capsys):
    monkeypatch.setattr(reset, "slack_handler_url", lambda: None)
    assert reset.reset_main(False) == 0
    assert world.slack_calls == 0 and "skipped: the tools host may not look up" in capsys.readouterr().out


def test_the_warm_up_signature_is_what_slack_sends():
    request = reset.signed_request("https://x.example/", SECRET, {"command": "/release"}, now=1793000000)
    expected = "v0=" + hmac.new(SECRET.encode(), b"v0:1793000000:command=%2Frelease", hashlib.sha256).hexdigest()
    assert request.get_header("X-slack-signature") == expected
    assert request.get_header("X-slack-request-timestamp") == "1793000000"


@pytest.mark.parametrize("text,key", [("1.4.0", (1, 4, 0)), ("1.10.2", (1, 10, 2)), ("1.4", None), ("v1.4.0", None), ("", None)])
def test_version_key(text, key):
    assert reset.version_key(text) == key


def test_after_baseline_compares_numbers_not_text():
    assert reset.after_baseline("1.10.0") and reset.after_baseline("1.4.0")
    assert not reset.after_baseline("1.3.0") and not reset.after_baseline("1.2.9") and not reset.after_baseline("takes")


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


def test_main_hands_itself_to_the_tools_host_with_the_slack_handlers_url(monkeypatch):
    calls = []
    monkeypatch.setattr(reset.host, "on_host", lambda: False)
    monkeypatch.setattr(reset, "slack_handler_url", lambda: "https://abc.lambda-url.eu-west-1.on.aws/")
    monkeypatch.setattr(reset.host, "hand_over", lambda *args, **kwargs: calls.append((args, kwargs)) or 0)
    monkeypatch.setattr("sys.argv", ["reset.py", "--dry-run"])
    assert reset.main() == 0
    assert calls == [(("reset", "reset.py", ["main", "--dry-run", "--slack-handler-url",
                                             "https://abc.lambda-url.eu-west-1.on.aws/"]),
                      {"follow_only": False, "title": "reset.py main --dry-run"})]


def test_a_shared_service_without_releases_has_no_copies_to_archive(world, monkeypatch, capsys):
    """demos-releases/<service> exists only once the service has a release."""
    monkeypatch.setattr(reset, "SERVICE", "pricing-api")
    monkeypatch.setattr(reset, "jcr_request", lambda method, path, password, data=None, headers=None: (404, b"{}"))
    dry = reset.Reset(dry_run=False, stamp=STAMP)
    reset.jfrog_takes(dry)
    assert dry.actions == 0 and "no release copies yet: demos-releases/pricing-api" in capsys.readouterr().out
