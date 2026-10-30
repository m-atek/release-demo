"""The CI adapter, scripts/ci_env.py, and what the stage scripts take from it:
under Jenkins everything stays as it was; GitHub Actions and the neutral names
give their own run ids, links, build ids and correlation ids. No AWS: fakes.

    .venv/bin/python -m pytest -q scripts/tests
"""

import io
import json
import subprocess
import sys
import urllib.error
from argparse import Namespace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import change  # noqa: E402
import ci_env  # noqa: E402
import common  # noqa: E402
import deploy  # noqa: E402
import events  # noqa: E402
import publish  # noqa: E402
import run_state  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
HEAD = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
SAM = "U0SAM000001"
JENKINS = {"BUILD_NUMBER": "57", "BUILD_URL": "https://jenkins-demo.es.nl/job/release/57/", "GIT_COMMIT": "a" * 40}
GITHUB = {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "m-atek/release-demo", "GITHUB_RUN_ID": "18300000001",
          "GITHUB_RUN_NUMBER": "12", "GITHUB_SHA": HEAD, "GITHUB_ACTOR": "m-atek", "GITHUB_SERVER_URL": "https://github.com"}
GITLAB = {"RELEASE_CI_NAME": "GitLab pipeline", "RELEASE_RUN_ID": "2100000001",
          "RELEASE_RUN_URL": "https://gitlab.com/demo/release-demo/-/pipelines/2100000001", "RELEASE_BUILD_ID": "gl-7",
          "RELEASE_COMMIT": "b" * 40, "RELEASE_CORRELATION_ID": "gitlab-ci:demo/release-demo:2100000001"}
CI_VARIABLES = {*JENKINS, *GITHUB, *GITLAB, "WORKSPACE"}
PEOPLE = {"mateusz": {"github_login": "m-atek", "slack_user_id": "U0MATEUSZ01"},
          "sam": {"github_login": "m-atek", "slack_user_id": SAM, "releaser": True},
          "alex": {"slack_user_id": None, "approver": True}}


@pytest.fixture(autouse=True)
def clean(tmp_path, monkeypatch):
    for name in CI_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(run_state, "PATH", tmp_path / ".release" / "state.json")
    monkeypatch.setattr(run_state, "REPORTS", tmp_path / ".release" / "reports")
    monkeypatch.setattr(common, "people", lambda client=None: PEOPLE)


def ci(monkeypatch, values):
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return ci_env.current()


# The three routes

def test_jenkins_gives_what_the_scripts_read_before():
    run = ci_env.current(JENKINS)
    assert (run.ci, run.name, run.build_id, run.run_url, run.commit) == (
        "jenkins", "Jenkins build", "57", "https://jenkins-demo.es.nl/job/release/57/", "a" * 40)
    assert (run.correlation_id, run.splunk, run.job_role, run.masks) == ("", True, False, False)


def test_a_run_by_hand_is_local():
    run = ci_env.current({})
    assert (run.ci, run.build_id, run.run_url, run.commit) == ("jenkins", "local", "", "")


def test_github_actions_names_its_run_and_never_clashes_with_jenkins_builds():
    run = ci_env.current(GITHUB)
    assert run.name == "GitHub Actions run"
    assert run.build_id == "gh-12"
    assert run.run_url == "https://github.com/m-atek/release-demo/actions/runs/18300000001"
    assert run.correlation_id == "github-actions:m-atek/release-demo:18300000001"
    assert (run.commit, run.actor, run.splunk, run.job_role, run.masks) == (HEAD, "m-atek", False, True, True)


def test_the_neutral_names_win_and_nothing_else_is_read():
    run = ci_env.current({**GITHUB, **JENKINS, **GITLAB})
    assert (run.ci, run.name, run.run_id, run.run_url, run.build_id, run.commit, run.correlation_id) == (
        "neutral", "GitLab pipeline", "2100000001", GITLAB["RELEASE_RUN_URL"], "gl-7", "b" * 40,
        "gitlab-ci:demo/release-demo:2100000001")
    assert (run.actor, run.splunk, run.job_role, run.masks, run.request_time_is_commit) == ("", True, False, False, False)


def test_on_github_the_requester_is_the_releaser_whose_login_merged():
    assert ci_env.requester(ci_env.current(GITHUB), PEOPLE) == SAM
    assert ci_env.requester(ci_env.current({**GITHUB, "GITHUB_ACTOR": "someone-else"}), PEOPLE) is None
    assert ci_env.requester(ci_env.current(JENKINS), PEOPLE) is None


def test_github_masks_a_secret_and_each_line_of_a_key_and_no_other_ci_does():
    out = io.StringIO()
    ci_env.mask("line one\nline two", ci_env.current(GITHUB), out)
    assert out.getvalue().splitlines() == ["::add-mask::line one%0Aline two", "::add-mask::line one",
                                           "::add-mask::line two"]
    out = io.StringIO()
    ci_env.mask("50%off", ci_env.current(GITHUB), out)
    assert out.getvalue() == "::add-mask::50%25off\n"
    out = io.StringIO()
    ci_env.mask("secret", ci_env.current(JENKINS), out)
    assert out.getvalue() == ""


def test_get_parameter_masks_secure_strings_only(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")

    class SSM:
        exceptions = Namespace(ParameterNotFound=KeyError)

        def get_parameter(self, Name, WithDecryption):
            secure = Name.endswith("token")
            return {"Parameter": {"Value": f"value of {Name}", "Type": "SecureString" if secure else "String"}}

    assert common.get_parameter(SSM(), "sonar/token") == "value of /release-demo/sonar/token"
    assert common.get_parameter(SSM(), "sonar/organization") == "value of /release-demo/sonar/organization"
    assert capsys.readouterr().out == "::add-mask::value of /release-demo/sonar/token\n"


# What the stage scripts do with it

def init(**overrides):
    args = {"service": "orders-service", "jira_key": "ORD-12", "option": "", "requested_by": "",
            "slack_channel": "", "slack_ts": "", "requested_at": "", "rows": "", **overrides}
    run_state.init(Namespace(**args))
    return run_state.load()


def test_under_jenkins_the_run_state_is_as_before(monkeypatch):
    ci(monkeypatch, {**JENKINS, "GIT_COMMIT": HEAD})
    monkeypatch.setattr(run_state, "now", lambda: 1793000100.0)
    state = init(requested_by=SAM, requested_at="1793000000")
    assert state["build"] == {"number": "57", "url": JENKINS["BUILD_URL"]}
    assert state["requested_by"] == SAM and state["started_at"] == 1793000000.0
    assert state["ci"] == {"name": "Jenkins build", "correlation_id": "", "splunk": True, "origin": ""}


def test_on_github_the_run_counts_from_the_merge_and_names_sam(monkeypatch):
    ci(monkeypatch, GITHUB)
    commit_time = int(subprocess.run(["git", "log", "-1", "--format=%ct", HEAD], cwd=ROOT, capture_output=True,
                                     text=True).stdout)
    monkeypatch.setattr(run_state, "now", lambda: commit_time + 20.0)
    state = init()
    assert state["build"] == {"number": "gh-12", "url": "https://github.com/m-atek/release-demo/actions/runs/18300000001"}
    assert state["commit"] == HEAD and state["started_at"] == commit_time
    assert state["requested_by"] == SAM
    assert state["ci"] == {"name": "GitHub Actions run", "correlation_id": "github-actions:m-atek/release-demo:18300000001",
                           "splunk": False, "origin": "by a merged pull request"}


def released(ci_state):
    return {"service": "orders-service", "version": "1.4.0", "jira_key": "ORD-12", "option": "", "requested_by": SAM,
            "build": {"number": "gh-12", "url": "https://github.com/m-atek/release-demo/actions/runs/18300000001"},
            "package": {"path": "orders-service-candidates/1.4.0/gh-12/orders-service-1.4.0.zip", "sha256": "ab" * 32},
            "started_at": 1793000000.0, "slack": {"channel": "C0RELEASES", "ts": "1793.0001"},
            "stages": {name: {"status": "waiting"} for name in run_state.STAGES}, **({"ci": ci_state} if ci_state else {})}


@pytest.fixture
def links(monkeypatch):
    monkeypatch.setattr(change, "get_parameter", lambda ssm, name: {
        "jira/site-url": "https://site.atlassian.net", "sonar/organization": "m-atek",
        "grafana/stack-url": "https://stack.grafana.net"}[name])
    return lambda state: change.links(state, None)


def test_a_jenkins_change_links_its_build_and_splunk_as_before(links):
    found = links(released(None))
    assert list(found) == ["Jira", "Jenkins build", "Package", "SonarQube", "Splunk", "Grafana"]


def test_a_github_change_links_the_run_and_leaves_splunk_out(links):
    found = links(released({"name": "GitHub Actions run", "correlation_id": "github-actions:m-atek/release-demo:1",
                            "splunk": False}))
    assert list(found) == ["Jira", "GitHub Actions run", "Package", "SonarQube", "Grafana"]
    assert found["GitHub Actions run"] == "https://github.com/m-atek/release-demo/actions/runs/18300000001"


def test_the_change_carries_the_correlation_id_the_bridge_finds_it_by(monkeypatch, links):
    calls = []

    class SN:
        url = "https://servicenow.example"

        def find(self, table, query, fields=None):
            return {"sys_id": f"{table}-id"}

        def call(self, method, path, body=None, **query):
            calls.append((method, path, body))
            return {"sys_id": {"value": "change-id"}, "number": {"value": "CHG0030050"}} if method == "POST" else {}

    monkeypatch.setattr(change, "requester_user", lambda sn, slack_id: {"sys_id": "sam-id"})
    monkeypatch.setattr(change, "attach", lambda sn, change_id, path: None)
    state = released({"name": "GitHub Actions run", "correlation_id": "github-actions:m-atek/release-demo:18300000001",
                      "splunk": False, "origin": "by a merged pull request"})
    change.open_change(SN(), None, state)
    fields = calls[0][2]
    assert fields["correlation_id"] == "github-actions:m-atek/release-demo:18300000001"
    assert fields["description"] == ("Release of orders-service 1.4.0 for ORD-12, started by a merged pull request "
                                     "and run by the release pipeline.")
    evidence = next(b["work_notes"] for m, p, b in calls if b and b.get("work_notes", "").startswith("Evidence:"))
    assert "GitHub Actions run: https://github.com/m-atek/release-demo/actions/runs/18300000001" in evidence
    assert "Splunk" not in evidence


def test_events_from_github_go_to_grafana_alone(monkeypatch, capsys):
    run_state.save(released({"name": "GitHub Actions run", "splunk": False}))
    sent = []
    monkeypatch.setattr(events, "ssm_client", lambda: None)
    monkeypatch.setattr(events, "to_splunk", lambda ssm, body: sent.append("splunk") or 200)
    monkeypatch.setattr(events, "to_loki", lambda ssm, body: sent.append("grafana") or 204)
    monkeypatch.setattr(sys, "argv", ["events.py", "--stage", "Build", "--result", "ok", "--seconds", "12.4"])
    events.main()
    assert sent == ["grafana"]
    assert capsys.readouterr().out.strip() == "event Build ok: Splunk skipped, Grafana 204"


def test_jenkins_events_still_go_to_both(monkeypatch):
    run_state.save(released(None))
    sent = []
    monkeypatch.setattr(events, "ssm_client", lambda: None)
    monkeypatch.setattr(events, "to_splunk", lambda ssm, body: sent.append("splunk") or 200)
    monkeypatch.setattr(events, "to_loki", lambda ssm, body: sent.append("grafana") or 204)
    monkeypatch.setattr(sys, "argv", ["events.py", "--stage", "Build", "--result", "ok"])
    events.main()
    assert sent == ["splunk", "grafana"]


def test_jfrog_through_cloudflare_access_sends_the_service_token(monkeypatch):
    seen = []

    class Response:
        status = 201

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b""

    monkeypatch.setattr(publish, "ACCESS", "cloudflare/github-actions")
    monkeypatch.setattr(publish, "_access", {})
    monkeypatch.setattr(publish, "ssm_client", lambda: None)
    monkeypatch.setattr(publish, "get_parameter", lambda ssm, name: f"value of {name}")
    monkeypatch.setattr(publish.urllib.request, "urlopen", lambda request, timeout: seen.append(request) or Response())
    assert publish.request("PUT", "orders-service-candidates/x", "jcr-token", b"data")[0] == 201
    headers = {k.lower(): v for k, v in seen[0].header_items()}
    assert headers["cf-access-client-id"] == "value of cloudflare/github-actions-client-id"
    assert headers["cf-access-client-secret"] == "value of cloudflare/github-actions-client-secret"
    assert headers["authorization"] == "Bearer jcr-token" and headers["user-agent"] == publish.USER_AGENT


def test_without_access_jfrog_gets_no_extra_headers(monkeypatch):
    monkeypatch.setattr(publish, "ACCESS", "")
    assert publish.access_headers() == {}


def test_a_stopped_tools_host_fails_publish_in_one_line(monkeypatch, capsys):
    monkeypatch.setattr(publish, "ACCESS", "cloudflare/github-actions")
    monkeypatch.setattr(publish, "_access", {"CF-Access-Client-Id": "x"})

    def unreachable(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 530, "origin unreachable", {}, io.BytesIO(b"error code: 1033"))

    monkeypatch.setattr(publish.urllib.request, "urlopen", unreachable)
    with pytest.raises(SystemExit):
        publish.request("PUT", "orders-service-candidates/x", "jcr-token", b"data")
    assert capsys.readouterr().err.strip() == ("JFrog doesn't answer through Cloudflare (530): the tools host is stopped. "
                                               "Start it with scripts/tools.sh up, then release again.")


def test_on_github_deploy_uses_the_jobs_own_role(monkeypatch):
    clients = []

    class Session:
        def client(self, name, **kwargs):
            clients.append((name, kwargs))
            return name

    monkeypatch.setattr(deploy, "aws_session", lambda: Session())
    ci(monkeypatch, GITHUB)
    assert deploy.lambda_client("orders-service") == "lambda"
    assert clients == [("lambda", {})]


def test_under_jenkins_deploy_still_assumes_the_services_deploy_role(monkeypatch):
    assumed = []

    class STS:
        def get_caller_identity(self):
            return {"Account": "123456789012"}

        def assume_role(self, RoleArn, RoleSessionName):
            assumed.append(RoleArn)
            return {"Credentials": {"AccessKeyId": "a", "SecretAccessKey": "b", "SessionToken": "c"}}

    class Session:
        def client(self, name, **kwargs):
            return STS() if name == "sts" else (name, kwargs)

    monkeypatch.setattr(deploy, "aws_session", lambda: Session())
    ci(monkeypatch, JENKINS)
    name, kwargs = deploy.lambda_client("orders-service")
    assert assumed == ["arn:aws:iam::123456789012:role/release-demo-deploy-orders-service"]
    assert name == "lambda" and kwargs["aws_session_token"] == "c"


def test_build_info_names_the_cis_run(monkeypatch, tmp_path):
    import build

    ci(monkeypatch, GITHUB)
    monkeypatch.setattr(build, "ROOT", tmp_path)  # where it prints the package's path from
    monkeypatch.setattr(sys, "argv", ["build.py", "orders-service", "--jira-key", "ORD-12", "--out", str(tmp_path)])
    build.main()
    info = json.loads((tmp_path / "orders-service" / "build-info.json").read_text())
    assert info["commit"] == HEAD
    assert info["build_url"] == "https://github.com/m-atek/release-demo/actions/runs/18300000001"
