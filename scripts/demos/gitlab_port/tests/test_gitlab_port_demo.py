"""Demo 17's own scripts, without GitLab, AWS or the tools host: fakes.

    .venv/bin/python -m pytest -q scripts/demos/gitlab_port/tests
"""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

import demo
import gitlab_demo
import setup
import sync

# The demo's reset.py by its path: scripts/reset.py has the same name.
_spec = importlib.util.spec_from_file_location("gitlab_port_reset", Path(__file__).resolve().parents[1] / "reset.py")
reset = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reset)


class SSM:
    exceptions = SimpleNamespace(ParameterNotFound=KeyError)

    def __init__(self, values=None):
        self.values = dict(values or {})

    def put_parameter(self, Name, Value, Overwrite, Type):
        self.values[Name.removeprefix("/release-demo/")] = Value

    def get_parameter(self, Name, WithDecryption=False):
        return {"Parameter": {"Value": self.values.get(Name.removeprefix("/release-demo/"), "unset")}}


class FakeGitLab:
    def __init__(self, project=None, runners=(), triggers=(), variables=(), pipelines=None):
        self.ssm, self.calls = SSM(), []
        self.project_value, self.runner_list, self.trigger_list = project, list(runners), list(triggers)
        self.variable_list, self.pipelines = list(variables), pipelines or {}

    def project(self, missing_ok=False):
        return self.project_value

    def runners(self, project_id):
        return self.runner_list

    def call(self, method, path, body=None, ok=None):
        self.calls.append((method, path, body))
        if method == "POST" and path == "/projects":
            return {"id": 61, "path_with_namespace": "demo-account/release-demo", **body}
        if method == "POST" and path == "/user/runners":
            return {"id": 7, "token": "glrt-test-token-not-real"}
        if method == "POST" and path.endswith("/reset_authentication_token"):
            return {"token": "glrt-new-test-token"}
        if method == "POST" and path.endswith("/triggers"):
            return {"token": "glptt-test-trigger-not-real"}
        if path.endswith("/triggers"):
            return self.trigger_list
        if path.endswith("/variables"):
            return self.variable_list
        if "/pipelines?status=" in path:
            return self.pipelines.get(path.split("status=")[1].split("&")[0], [])
        return None


PROJECT = {"id": 61, "path_with_namespace": "demo-account/release-demo", "visibility": "private",
           "shared_runners_enabled": False}
RUNNER = {"id": 7, "description": "tools-host", "status": "online", "tag_list": ["tools-host"],
          "run_untagged": False, "locked": True}
TRIGGER = {"description": "release-demo Slack handler", "token": "glpt"}


def test_a_missing_project_is_made_private_without_instance_runners():
    gl = FakeGitLab()
    project = setup.ensure_project(gl)
    method, path, body = gl.calls[0]
    assert (method, path) == ("POST", "/projects")
    assert body["visibility"] == "private" and body["shared_runners_enabled"] is False
    assert gl.ssm.values["gitlab/project-id"] == "61" and project["id"] == 61


def test_instance_runners_are_turned_off_on_an_existing_project():
    gl = FakeGitLab(project={**PROJECT, "shared_runners_enabled": True})
    setup.ensure_project(gl)
    assert ("PUT", "/projects/61", {"shared_runners_enabled": False}) in gl.calls


def test_the_runner_is_made_locked_tagged_and_refusing_untagged_jobs_and_its_token_goes_to_ssm():
    gl = FakeGitLab(project=PROJECT)
    setup.ensure_runner(gl, PROJECT)
    (method, path, body), = gl.calls
    assert (method, path) == ("POST", "/user/runners")
    assert body == {"runner_type": "project_type", "project_id": 61, "description": "tools-host", "tag_list": "tools-host",
                    "run_untagged": False, "locked": True, "access_level": "not_protected", "maximum_timeout": 3600}
    assert gl.ssm.values["gitlab/runner-token"] == "glrt-test-token-not-real"


def test_a_runner_whose_token_ssm_lost_gets_a_new_one():
    gl = FakeGitLab(project=PROJECT, runners=[RUNNER])
    setup.ensure_runner(gl, PROJECT)
    assert gl.calls == [("POST", "/runners/7/reset_authentication_token", None)]
    assert gl.ssm.values["gitlab/runner-token"] == "glrt-new-test-token"


def test_a_runner_with_its_token_in_ssm_is_left_alone():
    gl = FakeGitLab(project=PROJECT, runners=[RUNNER])
    gl.ssm.values["gitlab/runner-token"] = "glrt-stored"
    setup.ensure_runner(gl, PROJECT)
    assert gl.calls == []


def test_the_trigger_token_goes_to_ssm_once():
    gl = FakeGitLab(project=PROJECT)
    setup.ensure_trigger(gl, PROJECT)
    assert ("POST", "/projects/61/triggers", {"description": "release-demo Slack handler"}) in gl.calls
    assert gl.ssm.values["gitlab/trigger-token"] == "glptt-test-trigger-not-real"


def test_check_passes_as_the_demo_needs_it(capsys):
    gl = FakeGitLab(project=PROJECT, runners=[RUNNER], triggers=[TRIGGER])
    assert setup.check(gl) == 0
    assert "As the demo needs it." in capsys.readouterr().out


@pytest.mark.parametrize("change, says", [
    ({"project": {**PROJECT, "visibility": "public"}}, "the project isn't private"),
    ({"project": {**PROJECT, "shared_runners_enabled": True}}, "instance runners are on"),
    ({"runners": [{**RUNNER, "status": "offline"}]}, "tools-host is offline"),
    ({"runners": [RUNNER, {**RUNNER, "id": 8, "description": "other"}]}, "not tools-host alone"),
    ({"variables": [{"key": "X"}]}, "1 CI/CD variable"),
    ({"triggers": []}, "no trigger token"),
])
def test_check_names_what_isnt_as_wanted(capsys, change, says):
    settings = {"project": PROJECT, "runners": [RUNNER], "triggers": [TRIGGER], "variables": [], **change}
    assert setup.check(FakeGitLab(**settings)) == 1
    assert says in capsys.readouterr().out


def test_the_reset_cancels_pipelines_still_going_and_a_dry_run_doesnt(monkeypatch):
    gl = FakeGitLab(project=PROJECT, pipelines={"running": [{"id": 900, "status": "running"}]})
    steps = reset.Steps(dry_run=True)
    reset.cancel_pipelines(steps, gl, PROJECT)
    assert not [c for c in gl.calls if c[0] == "POST"] and steps.actions == 1
    gl.pipelines = {}
    steps = reset.Steps(dry_run=False)
    gl.pipelines = {"pending": [{"id": 901, "status": "pending"}]}
    monkeypatch.setattr(reset.time, "sleep", lambda seconds: gl.pipelines.clear())
    reset.cancel_pipelines(steps, gl, PROJECT)
    assert ("POST", "/projects/61/pipelines/901/cancel", None) in gl.calls


def test_a_gitlab_main_that_moved_on_its_own_is_never_pushed_over(monkeypatch):
    class Result:
        returncode, stderr = 1, " ! [rejected]        main -> main (non-fast-forward)"

    monkeypatch.setattr(sync.subprocess, "run", lambda *args, **kwargs: Result())
    gl = SimpleNamespace(username=lambda: "demo-account", token="not-a-token")
    with pytest.raises(SystemExit):
        sync.push(gl, PROJECT, "m-atek/release-demo")


def test_down_switches_release_to_jenkins_before_the_runner_stops(monkeypatch):
    order = []
    monkeypatch.setattr(demo, "GitLab", lambda: SimpleNamespace(ssm="ssm"))
    monkeypatch.setattr(demo, "switch", lambda ssm, value=None: order.append(("switch", value)) or value)
    monkeypatch.setattr(demo, "runner_unit", lambda action: order.append(("runner", action)) or "inactive")
    demo.down()
    assert order == [("switch", "jenkins"), ("runner", "stop")]


def test_the_account_token_is_never_read_on_the_tools_host(monkeypatch):
    monkeypatch.setattr(gitlab_demo.host, "on_host", lambda: True)
    with pytest.raises(SystemExit):
        gitlab_demo.GitLab(ssm=SSM())


def test_the_switch_reads_jenkins_until_set():
    ssm = SSM()
    assert gitlab_demo.switch(ssm) == "jenkins"
    gitlab_demo.switch(ssm, "gitlab-ci")
    assert gitlab_demo.switch(ssm) == "gitlab-ci"
