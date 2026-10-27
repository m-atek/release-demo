"""The optional hooks the shared scripts offer demos (docs/adding-a-demo.md),
and that a service without them gets exactly what it got before. Fakes for
ServiceNow, JCR and SSM.

    .venv/bin/python -m pytest -q scripts/tests
"""

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import change  # noqa: E402
import notify  # noqa: E402
import publish  # noqa: E402
import run_state  # noqa: E402
import servicenow_setup  # noqa: E402

SAM = "U0SAM000001"


@pytest.fixture(autouse=True)
def state_file(tmp_path, monkeypatch):
    monkeypatch.setattr(run_state, "PATH", tmp_path / ".release" / "state.json")
    monkeypatch.setattr(run_state, "REPORTS", tmp_path / ".release" / "reports")


def release_state(service="orders-service", option="", **extra):
    config = {"scripts": {}} if service == "orders-service" else change.service(service)
    return {"service": service, "version": "1.4.0", "jira_key": "ORD-12", "option": option, "requested_by": SAM,
            "build": {"number": "7", "url": "https://jenkins-demo.es.nl/job/release/7/"},
            "package": {"path": f"{service}-candidates/1.4.0/7/{service}-1.4.0.zip", "sha256": "ab" * 32},
            "started_at": 1793000000.0, "slack": {"channel": "C0RELEASES", "ts": "1793.0001"},
            "stages": {name: {"status": "waiting"} for name in run_state.stages_for(config)}, **extra}


class FakeServiceNow:
    url = "https://servicenow.example"

    def __init__(self, cis=None):
        self.cis, self.calls = cis or {}, []

    def find(self, table, query, fields=None):
        if table == "cmdb_ci":
            name = query.removeprefix("name=")
            return {"sys_id": self.cis[name]} if name in self.cis else None
        return {"sys_id": f"{table}-id"}

    def call(self, method, path, body=None, **query):
        self.calls.append((method, path, body))
        if method == "POST" and path.startswith("/api/sn_chg_rest/change/"):
            return {"sys_id": {"value": "change-id"}, "number": {"value": "CHG0030050"}}
        return {}


@pytest.fixture
def servicenow(monkeypatch):
    monkeypatch.setattr(change, "requester_user", lambda sn, slack_id: {"sys_id": "sam-id"})
    monkeypatch.setattr(change, "get_parameter", lambda ssm, name: {
        "jira/site-url": "https://site.atlassian.net", "sonar/organization": "m-atek",
        "grafana/stack-url": "https://stack.grafana.net"}[name])
    monkeypatch.setattr(change, "attach", lambda sn, change_id, path: None)
    monkeypatch.setattr(run_state, "now", lambda: 1793000020.0)
    return FakeServiceNow({"inv-cloud-01": "ci-1", "inv-cloud-02": "ci-2", "inv-dc-01": "ci-3"})


def opened(servicenow, state):
    change.open_change(servicenow, None, state)
    (method, path, fields), *rest = servicenow.calls
    return path, fields, [(m, p, b) for m, p, b in rest if not (m == "PATCH" and "work_notes" in (b or {}))]


def test_a_service_without_hooks_opens_the_change_as_before(servicenow):
    path, fields, rest = opened(servicenow, release_state())
    assert path == "/api/sn_chg_rest/change/normal"
    assert fields["short_description"] == "Release orders-service 1.4.0 (ORD-12)"
    assert fields["description"].endswith("Demo system, not a client's.")
    assert rest == [("PATCH", "/api/sn_chg_rest/change/normal/change-id", {"state": change.ASSESS})]


def test_option_standard_still_makes_a_standard_change(servicenow):
    path, _, rest = opened(servicenow, release_state(option="standard"))
    assert path == "/api/sn_chg_rest/change/standard/std_change_record_producer-id"
    assert rest == []


def test_extras_and_details_reach_the_change_and_affected_items_go_on_before_assess(servicenow):
    state = release_state("store-sync", change_extras={
        "fields": {"implementation_plan": "Wave 1: STORE-001", "backout_plan": "Per server"},
        "affected_cis": ["ci-a", "ci-b"]}, change_details=["Build 57", "Pilot first"])
    _, fields, rest = opened(servicenow, state)
    assert fields["implementation_plan"] == "Wave 1: STORE-001" and fields["backout_plan"] == "Per server"
    assert fields["description"].endswith("Demo system, not a client's.\n\nBuild 57\nPilot first")
    assert rest == [
        ("POST", "/api/now/table/task_ci", {"task": "change-id", "ci_item": "ci-a"}),
        ("POST", "/api/now/table/task_ci", {"task": "change-id", "ci_item": "ci-b"}),
        ("PATCH", "/api/sn_chg_rest/change/normal/change-id", {"state": change.ASSESS}),
    ]


def test_the_servers_in_services_yaml_are_affected_items(servicenow, capsys):
    _, _, rest = opened(servicenow, release_state("inventory"))
    assert [b["ci_item"] for m, p, b in rest if p == "/api/now/table/task_ci"] == ["ci-1", "ci-2", "ci-3"]
    assert "No CI inv-dc-02 in ServiceNow" in capsys.readouterr().out


@pytest.mark.parametrize("option, kind", [("stores", "standard"), ("jamf", "normal"), ("standard", "normal")])
def test_standard_options_name_the_options_that_make_a_standard_change(servicenow, option, kind):
    change.open_change(servicenow, None, release_state("store-floor", option=option))
    assert run_state.load()["change"]["type"] == kind


def test_the_slack_message_shows_a_deploy_scripts_rows_under_the_stages():
    state = release_state()
    assert "Wave 1" not in notify.render(state)
    state["rows"] = ["Wave 1  STORE-001 3.3.0 healthy", "Wave 2  STORE-002 3.2.0 waiting"]
    text = notify.render(state)
    assert "Record\n\nWave 1  STORE-001 3.3.0 healthy\nWave 2  STORE-002 3.2.0 waiting\n\n<https://" in text


def test_a_plan_script_adds_the_plan_stage_before_the_change():
    stages = run_state.stages_for({"scripts": {"plan": "scripts/demos/windows/plan.py"}})
    assert stages[stages.index("Check staging") + 1:stages.index("Check staging") + 3] == ["Plan", "Change"]
    assert "Plan" not in run_state.stages_for({"scripts": {}})


def test_template_texts_from_services_yaml_replace_the_shared_ones_field_by_field():
    pilot = servicenow_setup.template_texts("store-assistant", "pilot")
    assert pilot["short_description"] == "Pilot of Store Assistant"
    assert pilot["test_plan"] == "The pilot Macs report the new version in Jamf inventory."
    shared = servicenow_setup.template_texts("orders-service", "standard")
    assert shared["short_description"] == "Routine release of orders-service"
    assert "live alias" in shared["implementation_plan"]


class FakeJcr:
    """JCR's storage: path to content. PUT refuses an existing path, as the release-pipeline token does."""

    def __init__(self, files):
        self.files, self.puts = dict(files), []

    def request(self, method, path, token, data=None, headers=None):
        if method == "GET" and path.startswith("api/storage/"):
            content = self.files.get(path.removeprefix("api/storage/"))
            if content is None:
                return 404, b"{}"
            return 200, json.dumps({"checksums": {"sha256": hashlib.sha256(content).hexdigest()}}).encode()
        if method == "GET":
            return (200, self.files[path]) if path in self.files else (404, b"")
        if path in self.files:
            return 403, b"not allowed to overwrite"
        self.files[path] = data
        self.puts.append(path)
        return 201, b""


@pytest.fixture
def jcr(tmp_path, monkeypatch):
    package = b"the package of build 8"
    out = tmp_path / "dist" / "orders-service"
    out.mkdir(parents=True)
    (out / "build-info.json").write_text(json.dumps({"version": "1.4.0", "package": "orders-service-1.4.0.zip",
                                                     "sha256": hashlib.sha256(package).hexdigest()}))
    monkeypatch.setenv("WORKSPACE", str(tmp_path))
    monkeypatch.setattr(publish, "get_parameter", lambda ssm, name: "token")
    monkeypatch.setattr(publish, "ssm_client", lambda: None)
    monkeypatch.setattr(sys, "argv", ["publish.py", "orders-service", "--release"])
    state = release_state()
    state["package"] = {"path": "orders-service-candidates/1.4.0/8/orders-service-1.4.0.zip",
                        "sha256": hashlib.sha256(package).hexdigest(), "name": "orders-service-1.4.0.zip"}
    state["build"]["number"] = "8"
    run_state.save(state)

    def with_files(files):
        fake = FakeJcr({state["package"]["path"]: package, **files})
        monkeypatch.setattr(publish, "request", fake.request)
        return fake
    return package, with_files


def test_a_first_release_copies_to_the_version_folder(jcr):
    package, with_files = jcr
    fake = with_files({})
    publish.main()
    assert fake.puts == ["orders-service-releases/1.4.0/orders-service-1.4.0.zip",
                         "orders-service-releases/1.4.0/build-info.json"]
    assert run_state.load()["package"]["release_path"] == "orders-service-releases/1.4.0/orders-service-1.4.0.zip"


def test_the_same_package_released_again_is_not_copied_twice(jcr):
    package, with_files = jcr
    fake = with_files({"orders-service-releases/1.4.0/orders-service-1.4.0.zip": package,
                       "orders-service-releases/1.4.0/build-info.json": b"build 7's record"})
    publish.main()
    assert fake.puts == []


def test_another_build_of_a_released_version_goes_next_to_the_first(jcr, capsys):
    package, with_files = jcr
    fake = with_files({"orders-service-releases/1.4.0/orders-service-1.4.0.zip": b"build 7's package",
                       "orders-service-releases/1.4.0/build-info.json": b"build 7's record"})
    publish.main()
    assert fake.puts == ["orders-service-releases/1.4.0/8/orders-service-1.4.0.zip",
                         "orders-service-releases/1.4.0/8/build-info.json"]
    assert fake.files["orders-service-releases/1.4.0/orders-service-1.4.0.zip"] == b"build 7's package"
    assert run_state.load()["package"]["release_path"] == "orders-service-releases/1.4.0/8/orders-service-1.4.0.zip"
    assert "holds an earlier build of 1.4.0" in capsys.readouterr().out
