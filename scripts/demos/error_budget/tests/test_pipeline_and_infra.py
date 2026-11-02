"""With the switch off nothing changes: the pipeline's additions, change.py's three small
changes and what the demo adds to the shared files. Read from the files themselves, and a
fake ServiceNow for change.py."""

import importlib.util
import re
import sys

import pytest
import yaml

import run_state
from conftest import HERE, release_state

ROOT = HERE.parents[2]
sys.path.insert(1, str(ROOT / "scripts" / "tests"))

import change  # noqa: E402
import pipeline_calls  # noqa: E402

PIPELINE = (ROOT / "jenkins" / "shared-library" / "vars" / "releasePipeline.groovy").read_text()
BUDGET = (ROOT / "jenkins" / "shared-library" / "vars" / "errorBudget.groovy").read_text()
SERVICES = yaml.safe_load((ROOT / "services.yaml").read_text())
PARAMETERS = yaml.safe_load((ROOT / "infra" / "parameters.yaml").read_text())


def load_switch():
    spec = importlib.util.spec_from_file_location("budget_switch", HERE / "switch.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.SWITCH


class Ssm:
    def __init__(self, value):
        self.value = value

    def get_parameter(self, Name):
        return {"Parameter": {"Value": self.value}}


def test_the_switch_applies_only_to_a_service_that_names_the_check():
    switch = load_switch()
    assert switch.name == "error-budget/enabled"
    assert switch.on_for("orders-service", Ssm("on")) and not switch.on_for("orders-service", Ssm("off"))
    assert not switch.on_for("orders-service", Ssm("unset"))
    assert not switch.on_for("pricing-api", Ssm("on"))
    assert [name for name, config in SERVICES.items() if (config.get("scripts") or {}).get("error_budget")] == ["orders-service"]


def test_the_stage_is_first_and_every_addition_is_behind_the_switch():
    assert "boolean budgetOn = errorBudget.on(cfg)" in PIPELINE
    body = pipeline_calls.groovy_function(PIPELINE, "call")
    assert re.search(r"try \{\n\s+if \(budgetOn\) \{\n\s+releaseStage\('Error budget'\) \{ errorBudget\.run\(cfg\) \}\n\s+\}\n"
                     r"\s+releaseStage\('Build'\)", body)
    uses = [line.strip() for line in body.splitlines() if "errorBudget." in line]
    assert uses == ["boolean budgetOn = errorBudget.on(cfg)", "releaseStage('Error budget') { errorBudget.run(cfg) }",
                    "errorBudget.approved(cfg)", "errorBudget.released(cfg)"]
    for call in ("errorBudget.run(cfg)", "errorBudget.approved(cfg)", "errorBudget.released(cfg)"):
        before = body[:body.index(call)].rstrip().splitlines()[-1] if call != "errorBudget.run(cfg)" else ""
        if before:
            assert before.strip() == "if (budgetOn) {", call


def test_the_approval_line_comes_before_the_reason_and_record_before_the_mark():
    body = pipeline_calls.groovy_function(PIPELINE, "call")
    assert body.index('in ServiceNow."') < body.index("errorBudget.approved(cfg)") < body.index("releaseStage('Production'")
    assert body.index("releaseStage('Record')") < body.index("errorBudget.released(cfg)") < body.rindex("} catch (error)")


def test_on_is_off_for_a_service_without_the_script_and_if_the_switch_cant_be_read():
    on = pipeline_calls.groovy_function(BUDGET, "on")
    assert "if (!cfg.scripts?.error_budget) {\n        return false" in on
    assert "catch (error)" in on and "return false" in on[on.index("catch (error)"):]
    assert 'switch.py is-on "$SERVICE"' in on and '--add-stage' in on


def test_exit_3_refuses_and_any_other_failure_goes_to_a_person():
    run = pipeline_calls.groovy_function(BUDGET, "run")
    assert "status != 0 && status != 3" in run and "force_normal=true" in run and "--no-reading" in run
    assert "status == 3" in run and "error 'Error budget spent'" in run
    assert run.index("archiveArtifacts") < run.index("status == 3")
    assert "error-budget.json" in run


def test_an_approval_it_cant_confirm_stops_the_release():
    approved = pipeline_calls.groovy_function(BUDGET, "approved")
    assert "approved" in approved and "status != 0" in approved and "error " in approved


def test_nothing_on_the_other_cis_and_not_on_rollback():
    for path in (".gitlab-ci.yml", "azure-pipelines.yml", ".github/workflows/release.yml", "jenkins/rollback.Jenkinsfile",
                 "jenkins/shared-library/vars/rollbackPipeline.groovy", "slack/handler.py"):
        text = (ROOT / path).read_text()
        assert "error_budget" not in text and "errorBudget" not in text and "error-budget" not in text, path
    assert "Error budget" in pipeline_calls.NOT_ON_OTHER_CIS and "Error budget" not in pipeline_calls.jenkins_stages()


def test_services_yaml_holds_the_rule_and_the_new_word():
    orders = SERVICES["orders-service"]
    assert orders["slack"]["options"] == ["standard", "override"]
    assert "override" not in orders["servicenow"]["standard_options"]
    assert orders["scripts"]["error_budget"] == "scripts/demos/error_budget/budget.py"
    settings = orders["error_budget"]
    assert (settings["slo"], settings["window_hours"], settings["min_requests"], settings["fix_max_changed_lines"],
            settings["issue_label"], settings["function"]) == (99.9, 24, 1000, 20, "error-budget", "orders-service-production")
    assert settings["metrics"] == {"requests": "UrlRequestCount", "failed": "Errors"}


def test_the_parameters_have_names_and_no_values_and_the_main_reset_turns_the_check_off():
    for name in ("error-budget/enabled", "error-budget/traffic-until"):
        entry = PARAMETERS[name]
        assert entry["source"] == "script" and entry["secure"] is False and "value" not in entry and len(entry["help"]) > 30
    assert '"error-budget/enabled"' in (ROOT / "scripts" / "reset.py").read_text()
    assert "traffic-until" not in (ROOT / "scripts" / "reset.py").read_text()


def test_the_evidence_reader_gains_exactly_one_read():
    text = (ROOT / "infra" / "demos" / "evidence-reader" / "main.tf").read_text()
    statement = text[text.index('sid       = "ReadMetrics"') - 20:]
    assert 'actions   = ["cloudwatch:GetMetricData"]' in statement and 'resources = ["*"]' in statement


def test_the_demo_root_holds_the_traffic_and_its_narrow_role():
    text = (ROOT / "infra" / "demos" / "27-error-budget" / "main.tf").read_text()
    for needle in ('key          = "release-demo/demos/27-error-budget.tfstate"', 'Demo = "27-error-budget"',
                   'runtime          = "python3.13"', "memory_size      = 128", "timeout          = 65",
                   'retention_in_days = 14', 'schedule_expression = "rate(1 minute)"', 'qualifier     = "live"',
                   'kms:ViaService', '"error-budget/traffic-until"', '"grafana/logs-url"', '"grafana/logs-user"', '"grafana/logs-token"',
                   "maximum_retry_attempts       = 0", 'role   = "release-demo-tools-host"'):
        assert needle in text, needle
    # The function URL comes from a data source: no address in a public repository.
    assert "lambda-url" not in text and not re.search(r"\b\d{12}\b", text)


# change.py's three small changes.

class FakeServiceNow:
    url = "https://servicenow.example"

    def __init__(self, approvals=(), comments=None, journal=None):
        self.approvals, self.comments, self.journal, self.calls = list(approvals), comments or {}, journal or {}, []

    def find(self, table, query, fields=None):
        if table == "sys_journal_field":
            key = query.split("element_id=")[1].split("^")[0]
            return {"value": self.journal[key]} if key in self.journal else None
        return {"sys_id": f"{table}-id"}

    def call(self, method, path, body=None, **query):
        self.calls.append((method, path, body, query))
        if path == "/api/now/table/sysapproval_approver":
            state = query["sysparm_query"].split("^state=")[1]
            return [row for row in self.approvals if row["state"] == state]
        if path.startswith("/api/now/table/sysapproval_approver/"):
            return {"comments": self.comments.get(path.rsplit("/", 1)[1], "")}
        if method == "POST" and path.startswith("/api/sn_chg_rest/change/"):
            return {"sys_id": {"value": "change-id"}, "number": {"value": "CHG0030090"}}
        return {}


@pytest.fixture
def opened(monkeypatch):
    monkeypatch.setattr(change, "requester_user", lambda sn, slack_id: {"sys_id": "sam-id"})
    monkeypatch.setattr(change, "get_parameter", lambda ssm, name: {
        "jira/site-url": "https://site.atlassian.net", "sonar/organization": "m-atek",
        "grafana/stack-url": "https://stack.grafana.net"}[name])
    attached = []
    monkeypatch.setattr(change, "attach", lambda sn, change_id, path: attached.append(path.name))
    monkeypatch.setattr(change, "code_review", lambda state: None)  # who wrote the code: GitHub, and not this test's business
    monkeypatch.setattr(run_state, "now", lambda: 1793000020.0)

    def open_with(**extra):
        state = release_state(**extra)
        state["package"] = {"path": "orders-service-candidates/1.28.1/90/x.zip", "sha256": "ab" * 32}
        sn = FakeServiceNow()
        change.open_change(sn, None, state)
        return sn, run_state.load(), attached

    return open_with


def test_a_no_reading_makes_a_standard_request_a_normal_change_without_cost_review(opened):
    sn, state, _ = opened(option="standard", force_normal=True, change_details=["Error budget: no reading (x). Alex decides."])
    assert state["change"]["type"] == "normal"
    method, path, fields, _ = sn.calls[0]
    assert path == "/api/sn_chg_rest/change/normal" and "u_cost_review" not in fields
    assert fields["description"].endswith("\n\nError budget: no reading (x). Alex decides.")


def test_override_is_a_normal_change_with_its_lines_and_report(opened, tmp_path):
    report = tmp_path / "error-budget.json"
    report.write_text("{}")
    lines = ["Error budget, last 24 hours to 13:44: 140 of 86,455 requests failed. 99.9% allows 86. Spent: 161%.",
             "Source: CloudWatch, UrlRequestCount and Errors, orders-service-production.",
             "Override asked by Sam Developer. While the budget is spent, only the budget's own issue releases without this approval."]
    sn, state, attached = opened(option="override", override=True, change_details=lines, change_attachments=[str(report)])
    assert state["change"]["type"] == "normal"
    assert sn.calls[0][2]["description"].endswith("\n\n" + "\n".join(lines))
    assert "error-budget.json" in attached


def test_without_the_run_state_keys_the_change_is_as_it_was(opened):
    _, state, _ = opened(option="standard")
    assert state["change"]["type"] == "standard"
    _, state, _ = opened(option="")
    assert state["change"]["type"] == "normal"


def test_the_close_notes_lead_with_the_number_and_the_reason_on_success_only(monkeypatch):
    monkeypatch.setattr(run_state, "now", lambda: 1793000020.0)
    change_record = {"sys_id": "c1", "number": "CHG0030090", "type": "normal"}
    extra = "Released with the error budget 161% spent. Override approved by Alex: The fix is live."
    usual = "Released orders-service 1.28.1. Total time: 5m 40s. Build: https://jenkins-demo.es.nl/job/release/90/"

    def notes_of(code, **state):
        sn = FakeServiceNow()
        change.close(sn, {"change": change_record, **state}, code, usual)
        return sn.calls[-1][2]["close_notes"]

    assert notes_of("successful", close_notes_extra=extra) == f"{extra}\n{usual}"
    assert notes_of("successful") == usual
    assert notes_of("unsuccessful", close_notes_extra=extra) == usual


def test_the_comment_is_read_from_either_state_through_the_same_reader():
    sn = FakeServiceNow(approvals=[{"sys_id": "a1", "state": "approved", "approver": {"value": "alex-id"}}],
                        comments={"a1": "2026-11-04 13:50:01 - Alex Change Manager (Comments)\nThe fix is live.\n\n"})
    assert change.approved_comment(sn, {"sys_id": "c1"}) == "The fix is live."
    sn = FakeServiceNow(approvals=[{"sys_id": "a1", "state": "approved", "approver": {"value": "alex-id"}}],
                        journal={"a1": "From the journal."})
    assert change.approved_comment(sn, {"sys_id": "c1"}) == "From the journal."
    sn = FakeServiceNow(approvals=[{"sys_id": "a1", "state": "approved", "approver": {"value": "alex-id"}}])
    assert change.approved_comment(sn, {"sys_id": "c1"}) == ""
    assert change.approved_comment(FakeServiceNow(), {"sys_id": "c1"}) == ""
