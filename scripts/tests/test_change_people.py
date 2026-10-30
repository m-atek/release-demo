"""change.py for videos 12, 13 and 18: who may approve, what Slack says about a
rejection, the cancel that keeps the first close notes, and the change a cost
review makes. A fake ServiceNow; nothing leaves the machine."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import change  # noqa: E402
import notify  # noqa: E402
import run_state  # noqa: E402

USERS = {"alex-id": {"first_name": "Alex", "user_name": "alex.change.manager"},
         "fiona-id": {"first_name": "Fiona", "user_name": "fiona.finance"},
         "sam-id": {"first_name": "Sam", "user_name": "sam.developer"},
         "notes-id": {"first_name": "AI", "user_name": "ai.notes"},
         "pipeline-id": {"first_name": "Release", "user_name": "release.pipeline"}}


@pytest.fixture(autouse=True)
def state_file(tmp_path, monkeypatch):
    monkeypatch.setattr(run_state, "PATH", tmp_path / ".release" / "state.json")
    monkeypatch.setattr(run_state, "REPORTS", tmp_path / ".release" / "reports")
    monkeypatch.setattr(change.time, "sleep", lambda seconds: None)


class FakeServiceNow:
    url = "https://servicenow.example"

    def __init__(self, approval="requested", approved=(), rejected=(), comments="", journal="", state="-4"):
        self.approval, self.approved, self.rejected = approval, list(approved), list(rejected)
        self.comments, self.journal, self.state, self.calls = comments, journal, state, []

    def find(self, table, query, fields=None):
        if table == "sys_user" and query.startswith("sys_id="):
            return USERS.get(query.removeprefix("sys_id="))
        if table == "sys_user" and query.startswith("user_name="):
            name = query.removeprefix("user_name=")
            return next(({"sys_id": key} for key, user in USERS.items() if user["user_name"] == name), None)
        if table == "sys_journal_field":
            return {"value": self.journal} if self.journal else None
        return {"sys_id": f"{table}-id"}

    def call(self, method, path, body=None, **query):
        self.calls.append((method, path, body))
        if method == "GET" and path.startswith("/api/now/table/change_request/"):
            return {"approval": self.approval, "state": self.state}
        if method == "GET" and path == "/api/now/table/sysapproval_approver":
            rows = self.rejected if "state=rejected" in query["sysparm_query"] else self.approved
            return [{"sys_id": f"approval-{user}", "approver": {"value": user}} for user in rows]
        if method == "GET" and path.startswith("/api/now/table/sysapproval_approver/"):
            return {"comments": self.comments}
        if method == "PATCH" and "/api/sn_chg_rest/change/" in path and body.get("state") == change.CANCELED:
            self.state = change.CANCELED
        if method == "POST" and path.startswith("/api/sn_chg_rest/change/"):
            return {"sys_id": {"value": "change-id"}, "number": {"value": "CHG0030071"}}
        return {}

    def cancels(self):
        return [body for method, path, body in self.calls if method == "PATCH" and "close_notes" in (body or {})]


def waiting_state(**extra):
    state = {"service": "orders-service", "version": "1.4.1", "jira_key": "ORD-13", "option": "", "build": {},
             "slack": {"channel": "C0RELEASES", "ts": "1793.0001"}, "started_at": 1793000000.0,
             "stages": {"Approval": {"status": "running"}},
             "change": {"sys_id": "change-id", "number": "CHG0030071", "type": "normal", "requested_by": "sam-id"},
             **extra}
    run_state.save(state)
    return state


@pytest.fixture
def slack(monkeypatch):
    posted = []
    monkeypatch.setattr(notify, "post", lambda state: posted.append(list(state.get("lines", []))))
    return posted


def test_a_rejection_names_the_approver_and_quotes_the_comment(slack):
    sn = FakeServiceNow(approval="rejected", rejected=["alex-id"],
                        comments="2026-10-30 10:12:01 - Alex Change Manager (Comments)\n"
                                 "Add DELIVERY_ESTIMATE_DAYS to production first.\n")
    with pytest.raises(SystemExit):
        change.wait(sn, waiting_state(), timeout=60, interval=1)
    assert slack == [["Rejected by Alex in ServiceNow: Add DELIVERY_ESTIMATE_DAYS to production first."]]
    assert sn.cancels() == [{"state": change.CANCELED,
                             "close_notes": "Rejected in ServiceNow. The release stopped before production."}]


def test_the_stopped_run_s_second_cancel_keeps_the_first_close_notes(slack):
    sn = FakeServiceNow(approval="rejected", rejected=["fiona-id"], journal="Generate it once per order.")
    state = waiting_state()
    with pytest.raises(SystemExit):
        change.wait(sn, state, timeout=60, interval=1)
    assert slack == [["Rejected by Fiona in ServiceNow: Generate it once per order."]]
    change.cancel(sn, run_state.load(), "The release stopped: script returned exit code 1. Nothing changed in production.")
    assert len(sn.cancels()) == 1
    notes = [body["work_notes"] for method, path, body in sn.calls if method == "PATCH" and "work_notes" in (body or {})]
    assert notes[-1] == "The release stopped: script returned exit code 1. Nothing changed in production."


def test_a_rejection_without_a_comment_and_a_long_one(slack):
    sn = FakeServiceNow(approval="rejected", rejected=["alex-id"])
    with pytest.raises(SystemExit):
        change.wait(sn, waiting_state(), timeout=60, interval=1)
    long = "Please " + "check the regional settings again " * 6
    sn = FakeServiceNow(approval="rejected", rejected=["alex-id"], comments=long)
    with pytest.raises(SystemExit):
        change.wait(sn, waiting_state(), timeout=60, interval=1)
    assert slack[0] == ["Rejected by Alex in ServiceNow."]
    comment = slack[1][0].removeprefix("Rejected by Alex in ServiceNow: ")
    assert len(comment) <= 120 and comment.endswith("...")


@pytest.mark.parametrize("approver", ["ai.notes", "release.pipeline", "sam.developer"])
def test_an_approval_by_a_machine_user_or_the_requester_is_refused(approver, slack):
    user_id = next(key for key, user in USERS.items() if user["user_name"] == approver)
    sn = FakeServiceNow(approval="approved", approved=[user_id])
    with pytest.raises(SystemExit):
        change.wait(sn, waiting_state(), timeout=60, interval=1)
    assert "Refused by the pipeline" in sn.cancels()[0]["close_notes"]


def test_two_approvers_are_named_together(slack):
    sn = FakeServiceNow(approval="approved", approved=["alex-id", "fiona-id"])
    change.wait(sn, waiting_state(), timeout=60, interval=1)
    assert run_state.load()["change"]["approved_by"] == "Alex and Fiona"


@pytest.fixture
def opening(monkeypatch, tmp_path):
    attached = []
    monkeypatch.setattr(change, "requester_user", lambda sn, slack_id: {"sys_id": "sam-id"})
    monkeypatch.setattr(change, "get_parameter", lambda ssm, name: {
        "jira/site-url": "https://site.atlassian.net", "sonar/organization": "m-atek",
        "grafana/stack-url": "https://stack.grafana.net"}[name])
    monkeypatch.setattr(change, "attach", lambda sn, change_id, path: attached.append(path.name))
    monkeypatch.setattr(run_state, "now", lambda: 1793000020.0)
    return attached


def open_state(option, **extra):
    return {"service": "orders-service", "version": "1.7.0", "jira_key": "ORD-55", "option": option,
            "requested_by": "U0SAM000001", "build": {"number": "70", "url": "https://jenkins-demo.es.nl/job/release/70/"},
            "package": {"path": "orders-service-candidates/1.7.0/70/orders-service-1.7.0.zip", "sha256": "ab" * 32},
            "started_at": 1793000000.0, "slack": {"channel": "C0RELEASES", "ts": "1793.0001"},
            "stages": {name: {"status": "ok"} for name in run_state.stages_for({"scripts": {}})}, **extra}


@pytest.mark.parametrize("option, review", [("standard", "cost_approvers"), ("", "cost_and_change_approvers")])
def test_a_cost_review_makes_a_normal_change_that_says_whom_to_ask(opening, option, review):
    sn = FakeServiceNow()
    change.open_change(sn, None, open_state(option, cost_review=True, change_details=["Cost check (staging, ...)"]))
    method, path, fields = sn.calls[0]
    assert path == "/api/sn_chg_rest/change/normal" and fields["u_cost_review"] == review
    assert fields["description"].endswith("\n\nCost check (staging, ...)")
    assert ("PATCH", "/api/sn_chg_rest/change/normal/change-id", {"state": change.ASSESS}) in sn.calls
    assert run_state.load()["change"]["type"] == "normal"


def test_without_a_cost_review_nothing_changes_and_cost_json_joins_the_attachments(opening, tmp_path):
    sn = FakeServiceNow()
    reports = run_state.REPORTS
    reports.mkdir(parents=True, exist_ok=True)
    for name in ("junit.xml", "sbom.cdx.json", "grype.json", "build-info.json", "cost.json"):
        (reports / name).write_text("{}")
    change.open_change(sn, None, open_state("standard", change_work_notes=["Cost check: +USD 0 a month.", "Prices: ..."]))
    method, path, fields = sn.calls[0]
    assert path == "/api/sn_chg_rest/change/standard/std_change_record_producer-id" and "u_cost_review" not in fields
    assert opening == ["junit.xml", "sbom.cdx.json", "grype.json", "build-info.json", "cost.json"]
    notes = [body["work_notes"] for method, path, body in sn.calls if method == "PATCH" and "work_notes" in (body or {})]
    assert notes[-1] == "Cost check: +USD 0 a month.\nPrices: ..."


def test_a_release_copy_keeps_the_candidate_s_cost_json(tmp_path, monkeypatch):
    import hashlib
    import json as json_module

    import publish

    package = b"the package of build 70"
    out = tmp_path / "dist" / "orders-service"
    out.mkdir(parents=True)
    (out / "build-info.json").write_text(json_module.dumps({"version": "1.7.0", "package": "orders-service-1.7.0.zip",
                                                            "sha256": hashlib.sha256(package).hexdigest()}))
    files = {"orders-service-candidates/1.7.0/70/orders-service-1.7.0.zip": package,
             "orders-service-candidates/1.7.0/70/cost.json": b'{"result": "above"}'}

    def request(method, path, token, data=None, headers=None):
        if method == "GET" and path.startswith("api/storage/"):
            content = files.get(path.removeprefix("api/storage/"))
            return (200, json_module.dumps({"checksums": {"sha256": hashlib.sha256(content).hexdigest()}}).encode()) \
                if content is not None else (404, b"{}")
        if method == "GET":
            return (200, files[path]) if path in files else (404, b"")
        files[path] = data
        return 201, b""
    monkeypatch.setenv("WORKSPACE", str(tmp_path))
    monkeypatch.setattr(publish, "get_parameter", lambda ssm, name: "token")
    monkeypatch.setattr(publish, "ssm_client", lambda: None)
    monkeypatch.setattr(publish, "request", request)
    monkeypatch.setattr(sys, "argv", ["publish.py", "orders-service", "--release"])
    run_state.save({"service": "orders-service", "version": "1.7.0", "build": {"number": "70"}, "jira_key": "ORD-55",
                    "package": {"path": "orders-service-candidates/1.7.0/70/orders-service-1.7.0.zip",
                                "sha256": hashlib.sha256(package).hexdigest(), "name": "orders-service-1.7.0.zip"}})
    publish.main()
    assert files["orders-service-releases/1.7.0/cost.json"] == b'{"result": "above"}'
