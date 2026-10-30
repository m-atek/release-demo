"""servicenow_setup.py for videos 12, 13 and 18: ai.notes, Fiona and Cost
approvers, the field Cost review, and the decisions at Assess. An in-memory
ServiceNow behind the real ServiceNow class; nothing leaves the machine."""

import itertools
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import servicenow_setup as setup  # noqa: E402


class MemoryServiceNow(setup.ServiceNow):
    """Tables as lists of records. Queries: field=value terms joined by ^."""

    def __init__(self, tables=None):
        super().__init__("https://servicenow.example", "release-demo.setup", "x")
        self.tables = {name: [dict(row) for row in rows] for name, rows in (tables or {}).items()}
        self.ids = itertools.count(1)

    def rows(self, table, query):
        terms = [term for term in (query or "").split("^") if term and not term.startswith("ORDERBY") and term != "EQ"]
        def matches(row):
            for term in terms:
                if ">" in term:
                    field, value = term.split(">", 1)
                    if not int(row.get(field, 0)) > int(value):
                        return False
                    continue
                field, value = term.split("=", 1)
                if "." in field:  # a dot-walk the fake doesn't follow
                    continue
                if str(setup.raw(row.get(field, ""))) != value:
                    return False
            return True
        return [row for row in self.tables.get(table, []) if matches(row)]

    def call(self, method, path, body=None, **query):
        parts = path.removeprefix("/api/now/table/").split("/")
        table = parts[0]
        if method == "GET" and len(parts) == 1:
            found = self.rows(table, query.get("sysparm_query"))
            return found[: int(query["sysparm_limit"])] if query.get("sysparm_limit") else found
        if method == "GET":
            return next(row for row in self.tables[table] if row["sys_id"] == parts[1])
        if method == "POST":
            row = {**body, "sys_id": f"{table}-{next(self.ids)}"}
            self.tables.setdefault(table, []).append(row)
            return row
        row = next(row for row in self.tables[table] if row["sys_id"] == parts[1])
        row.update(body)
        return row


def policy_tables():
    return {
        "chg_policy": [{"sys_id": "policy", "name": "Normal Change Policy"}],
        "chg_approval_def": [{"sys_id": "cab", "name": "CAB Approval", "approver_source": "group",
                              "approval_action": "approve", "wait_for": "any"}],
        "sys_decision_question": [
            {"sys_id": "d-assess", "decision_table": "policy", "label": "Assessment - Assignment group",
             "condition": "change_request.state=-4^EQ", "answer": "old", "active": "true", "order": "100",
             "sys_created_on": "2024-01-01"},
            {"sys_id": "d-cab", "decision_table": "policy", "label": "Authorize - CAB",
             "condition": "change_request.state=-3^EQ", "answer": "cab", "active": "true", "order": "200"}],
    }


def test_without_cost_the_approvals_are_as_before():
    sn = MemoryServiceNow(policy_tables())
    setup.setup_approvals(sn, {"Change approvers": "group-change"})
    assess, cab = sn.tables["sys_decision_question"]
    assert assess["condition"] == "change_request.state=-4^EQ" and assess["active"] == "true"
    assert cab["active"] == "false"
    assert not sn.rows("sys_decision_question", f"label={setup.COST_DECISION}")


def test_with_cost_review_assess_asks_by_the_field():
    sn = MemoryServiceNow(policy_tables())
    groups = {"Change approvers": "group-change", "Cost approvers": "group-cost"}
    for _ in range(2):  # idempotent
        setup.setup_approvals(sn, groups, cost=True)
    decisions = {row["label"]: row for row in sn.tables["sys_decision_question"]}
    assert decisions["Assessment - Assignment group"]["condition"] == (
        "change_request.state=-4^change_request.u_cost_reviewISEMPTY^ORchange_request.u_cost_review="
        "cost_and_change_approvers^EQ")
    cost = decisions[setup.COST_DECISION]
    assert cost["condition"] == "change_request.state=-4^change_request.u_cost_reviewISNOTEMPTY^EQ"
    assert cost["order"] == "110" and cost["active"] == "true" and "sys_created_on" not in cost
    definition = sn.rows("chg_approval_def", f"name={setup.COST_DEFINITION}")[0]
    assert cost["answer"] == definition["sys_id"] and definition["group"] == "group-cost"
    assert len(sn.rows("sys_decision_question", f"label={setup.COST_DECISION}")) == 1


def test_a_clause_goes_in_once_before_the_closing_eq():
    once = setup.with_clause("change_request.state=-4^EQ", "x=1")
    assert once == "change_request.state=-4^x=1^EQ" and setup.with_clause(once, "x=1") == once


def test_cost_review_is_a_choice_right_under_type():
    sn = MemoryServiceNow({"sys_ui_element": [
        {"sys_id": "e-number", "sys_ui_section": "main", "element": "number", "position": "0"},
        {"sys_id": "e-type", "sys_ui_section": "main", "element": "type", "position": "1"},
        {"sys_id": "e-state", "sys_ui_section": "main", "element": "state", "position": "2"}]})
    assert setup.setup_cost_field(sn) is True
    field = sn.rows("sys_dictionary", "element=u_cost_review")[0]
    assert (field["column_label"], field["internal_type"], field["choice"]) == ("Cost review", "choice", "1")
    assert [(row["value"], row["label"]) for row in sn.tables["sys_choice"]] == setup.COST_CHOICES
    positions = {row["element"]: int(row["position"]) for row in sn.tables["sys_ui_element"]}
    assert positions == {"number": 0, "type": 1, "u_cost_review": 2, "state": 3}
    setup.setup_cost_field(sn)
    assert len(sn.tables["sys_ui_element"]) == 4 and len(sn.tables["sys_choice"]) == 2


class FakeSsm:
    class exceptions:
        class ParameterNotFound(Exception):
            pass

    def __init__(self, values):
        self.values, self.put = values, {}

    def get_parameter(self, Name, WithDecryption=True):
        name = Name.removeprefix("/release-demo/")
        if name not in self.values:
            raise self.exceptions.ParameterNotFound()
        return {"Parameter": {"Value": self.values[name]}}


def test_ai_notes_gets_a_generated_password_and_fiona_waits_for_hers(monkeypatch):
    ssm = FakeSsm({"servicenow/sam-password": "Sam#pass1", "servicenow/alex-password": "Alex#pass1",
                   "servicenow/release-pipeline-password": "Rp#pass1", "servicenow/ai-notes-password": "unset",
                   "servicenow/fiona-password": "unset", "servicenow/ines-password": "Ines#pass1",
                   "servicenow/evidence-reader-password": "unset"})
    stored = {}
    monkeypatch.setattr(setup, "put_parameter", lambda client, name, value, secure=True: stored.update({name: value}))
    sn = MemoryServiceNow({"sys_user_role": [{"sys_id": f"role-{name}", "name": name} for name in
                                             ("itil", "approver_user", "snc_basic_auth_api_access", "snc_read_only")]})
    monkeypatch.setattr(sn, "find", lambda table, query, fields=None: (
        {"sys_id": "u", "user_password": "set"} if table == "sys_user" and query.startswith("sys_id=")
        else setup.ServiceNow.find(sn, table, query, fields)))
    users = setup.setup_people(sn, ssm)
    assert "ai.notes" in users and "fiona.finance" not in users
    assert list(stored) == ["servicenow/ai-notes-password", "servicenow/evidence-reader-password"]
    notes_user = sn.rows("sys_user", "user_name=ai.notes")[0]
    assert notes_user["web_service_access_only"] == "true" and notes_user["first_name"] == "AI"
    roles = {setup.raw(row["role"]) for row in sn.tables["sys_user_has_role"] if row["user"] == notes_user["sys_id"]}
    assert roles == {"role-itil", "role-snc_basic_auth_api_access"}
    groups = setup.setup_groups(sn, users)
    members = [row for row in sn.tables["sys_user_grmember"] if row["group"] == groups["Cost approvers"]]
    assert members == []
    assert not any(row["user"] == users["ai.notes"] for row in sn.tables["sys_user_grmember"])


def test_a_missing_parameter_says_to_apply_infra_first():
    with pytest.raises(SystemExit):
        setup.get_parameter_or_none(FakeSsm({}), "servicenow/ai-notes-password")
