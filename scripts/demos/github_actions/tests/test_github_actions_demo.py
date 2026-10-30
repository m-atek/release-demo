"""Demo 14's own scripts, without GitHub, AWS or ServiceNow: the calls are fakes.

    .venv/bin/python -m pytest -q scripts/demos/github_actions/tests
"""

import importlib.util
from pathlib import Path

import pytest

import app_hook
import bridge_test
import environments
import github_demo
import pr

# The demo's reset.py by its path: scripts/reset.py has the same name.
_spec = importlib.util.spec_from_file_location("github_actions_reset", Path(__file__).resolve().parents[1] / "reset.py")
reset = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reset)

APP_ID = "123456"


# The pull request

def test_the_pull_request_replaces_the_service_with_the_state_and_deletes_what_it_lacks():
    entries = pr.tree_entries(["services/orders-service/handler.py", "services/orders-service/old.py"],
                              [("services/orders-service/handler.py", "new"), ("services/orders-service/VERSION", "1.4.0\n")])
    assert entries == [
        {"path": "services/orders-service/handler.py", "mode": "100644", "type": "blob", "content": "new"},
        {"path": "services/orders-service/VERSION", "mode": "100644", "type": "blob", "content": "1.4.0\n"},
        {"path": "services/orders-service/old.py", "mode": "100644", "type": "blob", "sha": None},
    ]


def test_the_state_is_1_4_0_without_caches():
    files = dict(pr.state_files())
    assert files["services/orders-service/VERSION"].strip() == "1.4.0"
    assert "Delivery estimate" in files["services/orders-service/handler.py"]
    assert not [path for path in files if "__pycache__" in path or ".pytest_cache" in path]


@pytest.mark.parametrize("refs, branch", [
    ([], "demo-14/delivery-estimate"),
    (["demo-14/delivery-estimate"], "demo-14/delivery-estimate-2"),
    (["demo-14/delivery-estimate", "demo-14/delivery-estimate-2"], "demo-14/delivery-estimate-3"),
])
def test_a_branch_an_earlier_take_used_gets_the_next_number(monkeypatch, refs, branch):
    monkeypatch.setattr(pr, "gh_api", lambda method, path, body=None, check=True: [{"ref": f"refs/heads/{r}"} for r in refs])
    assert pr.free_branch("m-atek/release-demo") == branch


def test_open_refuses_while_main_isnt_on_the_baseline(monkeypatch):
    import base64

    replies = {"/repos/r/pulls?state=open&per_page=50": [], "/repos/r/git/ref/heads/main": {"object": {"sha": "f" * 40}},
               f"/repos/r/contents/services/orders-service/VERSION?ref={'f' * 40}": {"content": base64.b64encode(b"1.4.0\n").decode()}}
    monkeypatch.setattr(pr, "gh_api", lambda method, path, body=None, check=True: replies[path])
    with pytest.raises(SystemExit):
        pr.open_pull("r")


# The environments

def environment(bypass=False, reviewers=False, timer=0, custom=True):
    rules = ([{"type": "required_reviewers"}] if reviewers else []) + ([{"type": "wait_timer", "wait_timer": timer}] if timer else [])
    return {"protection_rules": rules, "can_admins_bypass": bypass,
            "deployment_branch_policy": {"protected_branches": not custom, "custom_branch_policies": custom}}


RULE = [{"app": {"id": int(APP_ID), "slug": "release-demo-m-atek"}}]
MAIN = [{"name": "main"}]


def test_production_as_wanted_has_no_problems():
    assert environments.problems("production", environment(), RULE, MAIN, APP_ID, 0) == []
    assert environments.problems("staging", environment(), [], MAIN, APP_ID, 0) == []


@pytest.mark.parametrize("env, rules, policies, secrets, says", [
    (environment(bypass=True), RULE, MAIN, 0, "lets administrators bypass"),
    (environment(reviewers=True), RULE, MAIN, 0, "has required reviewers"),
    (environment(timer=5), RULE, MAIN, 0, "has a wait timer"),
    (environment(custom=False), RULE, MAIN, 0, "deploys from more than main"),
    (environment(), RULE, MAIN + [{"name": "demo-14/*"}], 0, "deploys from more than main"),
    (environment(), [], MAIN, 0, "not the GitHub App alone"),
    (environment(), RULE + [{"app": {"id": 7, "slug": "other"}}], MAIN, 0, "not the GitHub App alone"),
    (environment(), RULE, MAIN, 1, "holds 1 secret"),
])
def test_anything_else_is_named(env, rules, policies, secrets, says):
    assert any(says in line for line in environments.problems("production", env, rules, policies, APP_ID, secrets))


def test_the_wanted_environment_turns_off_reviewers_timer_and_admin_bypass():
    assert environments.wanted_environment() == {
        "wait_timer": 0, "prevent_self_review": False, "reviewers": [],
        "deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True},
        "can_admins_bypass": False}


# The app's webhook

def test_the_app_lists_what_its_settings_still_lack():
    app = {"permissions": {"contents": "write", "metadata": "read"}, "events": []}
    lines = app_hook.missing(app, {"permissions": {}, "events": []})
    assert lines == ["app: Repository permissions, Deployments: Read and write (now none)",
                     "app: Repository permissions, Actions: Read-only (now none)",
                     "app: Subscribe to events, Deployment protection rule"]


def test_new_permissions_must_be_accepted_on_the_installation():
    app = {"permissions": {"deployments": "write", "actions": "read"}, "events": ["deployment_protection_rule"]}
    assert app_hook.missing(app, {"permissions": {"deployments": "write", "actions": "read"},
                                  "events": ["deployment_protection_rule"]}) == []
    lines = app_hook.missing(app, {"permissions": {}, "events": []})
    assert lines[0].startswith("installation: accept the new deployments permission")
    assert lines[-1].startswith("installation: accept the Deployment protection rule event")


# The reset

class FakeApp:
    repository = "m-atek/release-demo"

    def __init__(self, runs):
        self.runs, self.posts = runs, []

    def call(self, method, path, body=None):
        status = path.split("status=")[1].split("&")[0] if "status=" in path else ""
        return {"workflow_runs": [r for r in self.runs if r["status"] == status]}

    def status(self, method, path, body=None):
        self.posts.append((path, body))
        return 204, None


def run(id_, status):
    return {"id": id_, "status": status, "display_title": "ORD-12 Show the delivery estimate", "html_url": f"https://x/{id_}"}


def test_a_waiting_run_is_rejected_as_the_app_and_a_running_one_cancelled_as_you(monkeypatch):
    app = FakeApp([run(2, "waiting"), run(1, "in_progress")])
    cancelled = []
    monkeypatch.setattr(reset, "gh_signed_in", lambda: True)
    monkeypatch.setattr(reset, "gh", lambda *args: cancelled.append(args))
    steps = reset.Steps(dry_run=False)
    reset.stop_runs(steps, app)
    assert app.posts == [("/repos/m-atek/release-demo/actions/runs/2/deployment_protection_rule",
                          {"environment_name": "production", "state": "rejected",
                           "comment": "Cancelled by reset.py before the next take."})]
    assert cancelled == [("run", "cancel", "1", "-R", "m-atek/release-demo")]


def test_on_the_tools_host_a_running_run_is_named_not_cancelled(monkeypatch, capsys):
    app = FakeApp([run(1, "queued")])
    monkeypatch.setattr(reset, "gh_signed_in", lambda: False)
    reset.stop_runs(reset.Steps(dry_run=False), app)
    assert "run 1 is queued: cancel it on GitHub" in capsys.readouterr().out


def test_a_dry_run_changes_nothing(monkeypatch):
    app = FakeApp([run(2, "waiting")])
    steps = reset.Steps(dry_run=True)
    reset.stop_runs(steps, app)
    assert app.posts == [] and steps.actions == 1


# Day one

def test_servicenow_and_github_times_read_as_utc():
    assert bridge_test.utc("2026-11-02 10:00:05") == bridge_test.utc("2026-11-02T10:00:05Z") == 1793613605


def test_clean_needs_mateusz_s_yes(monkeypatch):
    monkeypatch.setattr("sys.argv", ["bridge_test.py", "clean"])
    with pytest.raises(SystemExit):
        bridge_test.main()


def test_the_throwaway_workflow_comes_and_goes_with_your_gh_login_not_the_app(monkeypatch):
    calls = []
    on_main = {}

    def gh_api(method, path, body=None, check=True):
        calls.append((method, path.split("?")[0]))
        if method == "GET":
            return {"name": "bridge-test"} if "/environments/" in path else on_main.get("file")
        if method == "PUT":
            on_main["file"] = {"sha": "abc"}
        if method == "DELETE" and "contents" in path:
            assert body["sha"] == "abc" and "[skip ci]" in body["message"]
            on_main.pop("file")
        return {}

    monkeypatch.setattr(bridge_test, "gh_api", gh_api)
    monkeypatch.setattr(bridge_test, "gh_signed_in", lambda: True)
    monkeypatch.setattr(bridge_test, "repository", lambda: "owner/release-demo")
    monkeypatch.setattr(bridge_test, "App", lambda *a: pytest.fail("the GitHub App may not write workflow files"))
    bridge_test.workflow_on_main("owner/release-demo")
    bridge_test.workflow_on_main("owner/release-demo")
    workflow = "/repos/owner/release-demo/contents/.github/workflows/bridge-test.yml"
    assert [call for call in calls if call[0] == "PUT"] == [("PUT", workflow)]
    bridge_test.clean()
    assert ("DELETE", workflow) in calls and ("DELETE", "/repos/owner/release-demo/environments/bridge-test") in calls


def test_the_demos_names():
    assert github_demo.TITLE == "ORD-12 Show the delivery estimate on the order page"
    assert github_demo.BRANCH == "demo-14/delivery-estimate"
