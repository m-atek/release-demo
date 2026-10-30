"""Demo 22's own scripts, without Azure DevOps, Entra, AWS or the tools host: fakes.

    .venv/bin/python -m pytest -q scripts/demos/azure_devops/tests
"""

import importlib.util
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parents[1]


def load(name):
    """A script of this folder by its path: other folders have a setup.py and a reset.py too."""
    spec = importlib.util.spec_from_file_location(f"azure_devops_{name}", HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


release_bot = load("release_bot")
setup = load("setup")
reset = load("reset")
demo = load("demo")
day_one = load("day_one")

ENVIRONMENT = {"id": 3, "name": "production"}
CONNECTION = {"id": "66666666-6666-6666-6666-666666666666"}


def test_the_check_is_invoke_rest_api_in_callback_mode_with_microsofts_headers():
    body = setup.check_body(ENVIRONMENT, CONNECTION)
    assert body["type"] == {"id": "fe1de3ee-a436-41b4-bb20-f6eb4cb879a7", "name": "Task Check"}
    assert body["timeout"] == 60 and body["resource"] == {"type": "environment", "id": "3", "name": "production"}
    settings = body["settings"]
    assert settings["displayName"] == "ServiceNow change approved" and settings["retryInterval"] == 0
    assert settings["definitionRef"]["name"] == "InvokeRESTAPI"
    inputs = settings["inputs"]
    assert inputs["waitForCompletion"] == "true" and inputs["method"] == "POST"
    assert inputs["connectedServiceName"] == CONNECTION["id"] and inputs["urlSuffix"] == "azure-devops/check"
    headers = json.loads(inputs["headers"])
    assert set(headers) == {"Content-Type", "PlanUrl", "ProjectId", "HubName", "PlanId", "JobId", "TimelineId",
                            "TaskInstanceId", "AuthToken", "BuildId"}
    assert headers["AuthToken"] == "$(system.AccessToken)" and headers["BuildId"] == "$(build.BuildId)"


def test_the_bridges_headers_are_the_ones_it_reads():
    spec = importlib.util.spec_from_file_location("bridge_for_azure", HERE.parents[2] / "bridge" / "handler.py")
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    sent = {name.lower() for name in setup.HEADERS if name != "Content-Type"}
    assert sent == set(bridge.CHECK_HEADERS)


def test_release_bot_may_queue_read_and_cancel_the_pipeline_and_nothing_else():
    assert setup.RELEASE_BOT_RIGHTS == 1 + 128 + 512 + 1024  # view, queue and stop builds, view the pipeline



def test_the_slack_handler_may_read_the_names_it_reads_and_not_the_agents_token():
    tf = (HERE.parents[2] / "infra" / "demos" / "22-azure-devops-approval" / "main.tf").read_text()
    statement = tf[tf.index('"ReadAzureDevOpsNames"'):]
    granted = {f"azure-devops/{name}" for name in
               re.findall(r'"([^"]+)"', re.search(r"for name in \[([^\]]*)\]", statement).group(1))}
    handler = (HERE.parents[2] / "slack" / "handler.py").read_text()
    names = re.search(r"AZURE_PARAMETERS = \[(.*?)\]", handler, re.S).group(1)
    assert granted == {name for name in re.findall(r'"([^"]+)"', names) if name.startswith("azure-devops/")}
    assert "azure-devops/agent-registration-token" not in granted and "azure-devops/*" not in statement

class Response(SimpleNamespace):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_release_bot_signs_in_with_aws_signed_token_and_no_secret(monkeypatch):
    asked, sent = [], []

    class STS:
        def get_web_identity_token(self, **kwargs):
            asked.append(kwargs)
            return {"WebIdentityToken": "aws-signed-test-token"}

    def urlopen(request, timeout):
        sent.append(request)
        return Response(read=lambda: b'{"access_token": "azure-devops-test-token"}')

    monkeypatch.setattr(release_bot.urllib.request, "urlopen", urlopen)
    values = {"organization": "demo-org", "pipeline-id": "7", "tenant-id": "tenant", "release-bot-client-id": "client"}
    assert release_bot.token(values, SimpleNamespace(client=lambda name: STS())) == "azure-devops-test-token"
    assert asked == [{"Audience": ["api://AzureADTokenExchange"], "SigningAlgorithm": "RS256", "DurationSeconds": 300}]
    request, = sent
    assert request.full_url == "https://login.microsoftonline.com/tenant/oauth2/v2.0/token"
    assert "client_secret" not in request.data.decode() and "client_assertion=aws-signed-test-token" in request.data.decode()


def test_runs_in_progress_are_the_release_pipelines_only(monkeypatch):
    monkeypatch.setattr(release_bot, "call", lambda method, path, body=None, values=None, bearer=None: {
        "value": [{"id": 1, "state": "inProgress"}, {"id": 2, "state": "completed"}]})
    values = {"organization": "demo-org", "pipeline-id": "7"}
    assert [r["id"] for r in release_bot.runs_in_progress(values, "token")] == [1]


def test_the_reset_cancels_runs_as_release_bot_and_a_dry_run_doesnt(monkeypatch):
    cancelled = []
    monkeypatch.setattr(reset.release_bot, "settings", lambda: {"organization": "demo-org", "pipeline-id": "7"})
    monkeypatch.setattr(reset.release_bot, "token", lambda values: "token")
    going = [[{"id": 40, "name": "orders-service 1.4.0"}]]
    monkeypatch.setattr(reset.release_bot, "runs_in_progress", lambda values, bearer: going[0])
    monkeypatch.setattr(reset.release_bot, "call", lambda *args: cancelled.append(args) or going.__setitem__(0, []))
    steps = reset.Steps(dry_run=True)
    reset.cancel_runs(steps)
    assert cancelled == [] and steps.actions == 1
    reset.cancel_runs(reset.Steps(dry_run=False))
    (method, path, body, *_), = cancelled
    assert (method, path, body) == ("PATCH", "/release-demo/_apis/build/builds/40?api-version=7.1", {"status": "cancelling"})


def test_down_switches_release_to_jenkins_before_the_agent_stops(monkeypatch):
    order = []
    monkeypatch.setattr(demo, "ssm_client", lambda: "ssm")
    monkeypatch.setattr(demo, "switch", lambda ssm, value=None: order.append(("switch", value)) or value)
    monkeypatch.setattr(demo, "shell", lambda script, timeout=60: order.append(("host", script.split(";")[0])) or (0, "inactive"))
    demo.down()
    assert order == [("switch", "jenkins"), ("host", "systemctl stop release-demo-azure-agent")]


def test_the_test_changes_correlation_id_is_the_one_the_pipeline_writes():
    assert day_one.correlation("demo-org", 321) == "azure-pipelines:demo-org/release-demo:321"


def test_clean_needs_mateusz_s_yes(monkeypatch):
    monkeypatch.setattr("sys.argv", ["day_one.py", "clean"])
    with pytest.raises(SystemExit):
        day_one.main()


def test_azure_devops_times_read_as_utc():
    assert day_one.utc("2026-11-02T10:00:05.1234567Z") == day_one.utc("2026-11-02 10:00:05") == 1793613605


class FakeOrganization:
    """Azure DevOps for ensure_release_bot and --check's Release bots lines: answers the reads, records the writes."""

    def __init__(self, bot_known=True, member=True, project_bits=0, build_bits=0):
        self.organization = "demo-org"
        self.writes = []
        self.bot_known, self.member = bot_known, member
        self.bits = {setup.PROJECT_NAMESPACE: project_bits, setup.BUILD_NAMESPACE: build_bits}

    def call(self, method, url, body=None, missing_ok=False):
        if method != "GET":
            self.writes.append((method, url.split("?")[0], body))
            if "serviceprincipalentitlements" in url:
                return {"operationResult": {"result": {"servicePrincipal": {"descriptor": "aad.bot"}}}}
            return None
        if "/graph/serviceprincipals" in url:
            known = [{"originId": "object-1", "descriptor": "aad.bot", "displayName": setup.RELEASE_BOT}]
            return {"value": known if self.bot_known else []}
        if "/graph/descriptors/" in url:
            return {"value": "scp.project"}
        if "/graph/groups" in url:
            return {"value": [{"displayName": setup.GROUP, "descriptor": "vssgp.bots"}]}
        if "/_apis/identities" in url:
            return {"value": [{"descriptor": "Microsoft.TeamFoundation.Identity;S-1-9-bots"}]}
        if "/graph/memberships/vssgp.bots" in url:
            return {"value": [{"memberDescriptor": "aad.bot"}] if self.member else []}
        if "/_apis/accesscontrollists/" in url:
            namespace = url.split("/_apis/accesscontrollists/")[1].split("?")[0]
            return {"value": [{"acesDictionary": {"bots": {"allow": self.bits[namespace], "deny": 0}}}]}
        raise AssertionError(f"unexpected call {method} {url}")


def test_release_bots_see_the_project_and_queue_read_and_cancel_the_pipeline(monkeypatch):
    monkeypatch.setattr(setup, "release_bot_object_id", lambda: "object-1")
    organization = FakeOrganization()
    setup.ensure_release_bot(organization, {"id": "p-1"}, {"id": 7, "name": "release"})
    aces = {url.rsplit("/", 1)[1]: body for method, url, body in organization.writes if "accesscontrolentries" in url}
    project, build = aces[setup.PROJECT_NAMESPACE], aces[setup.BUILD_NAMESPACE]
    assert project["token"] == "$PROJECT:vstfs:///Classification/TeamProject/p-1" and project["merge"] is True
    assert project["accessControlEntries"] == [{"descriptor": "Microsoft.TeamFoundation.Identity;S-1-9-bots",
                                                "allow": 1, "deny": 0}]  # View project-level information only
    assert build["token"] == "p-1/7" and build["accessControlEntries"][0]["allow"] == setup.RELEASE_BOT_RIGHTS
    assert not any("serviceprincipalentitlements" in url for _, url, _ in organization.writes)  # added once, earlier


def test_release_bot_is_added_to_the_organization_only_when_it_is_missing(monkeypatch):
    monkeypatch.setattr(setup, "release_bot_object_id", lambda: "object-1")
    organization = FakeOrganization(bot_known=False)
    setup.ensure_release_bot(organization, {"id": "p-1"}, {"id": 7, "name": "release"})
    (_, _, body), = [w for w in organization.writes if "serviceprincipalentitlements" in w[1]]
    assert body["servicePrincipal"]["originId"] == "object-1" and body["accessLevel"] == {"accountLicenseType": "express"}


def test_the_check_fails_while_release_bots_cant_see_the_project():
    rights = setup.RELEASE_BOT_RIGHTS
    blind = setup.release_bot_problems(FakeOrganization(project_bits=0, build_bits=rights), {"id": "p-1"}, "7")
    assert blind == ["Release bots can't see the project, so Azure DevOps answers Release Bot 404: run --apply"]
    assert setup.release_bot_problems(FakeOrganization(project_bits=1, build_bits=rights), {"id": "p-1"}, "7") == []
    outside = setup.release_bot_problems(FakeOrganization(member=False, project_bits=1, build_bits=rights), {"id": "p-1"}, "7")
    assert outside == ["release-demo Release Bot isn't in Release bots: run --apply"]


def test_whoami_says_whether_release_bot_reads_the_release_pipeline(monkeypatch, capsys):
    values = {"organization": "demo-org", "pipeline-id": "7"}
    monkeypatch.setattr("sys.argv", ["release_bot.py", "whoami"])
    monkeypatch.setattr(release_bot, "settings", lambda: values)
    monkeypatch.setattr(release_bot, "token", lambda values: "token")

    def call(method, path, body=None, values=None, bearer=None):
        if path == "/_apis/connectionData":
            return {"authenticatedUser": {"providerDisplayName": "release-demo Release Bot"}}
        raise release_bot.urllib.error.HTTPError(path, 404, "Not Found", {}, None)

    monkeypatch.setattr(release_bot, "call", call)
    with pytest.raises(SystemExit):
        release_bot.main()
    out = capsys.readouterr()
    assert "Azure DevOps sees: release-demo Release Bot" in out.out
    assert "can't read the release pipeline's runs (404)" in out.out + out.err
    monkeypatch.setattr(release_bot, "runs_in_progress", lambda values, bearer: [])
    release_bot.main()
    assert capsys.readouterr().out.endswith("Release Bot reads the release pipeline: 0 run(s) in progress\n")


def test_day_one_token_shows_whoamis_refusal_too(monkeypatch, capsys):
    monkeypatch.setattr(day_one.host, "Host", lambda: SimpleNamespace(shell=lambda script, timeout=60: (
        1, "Azure DevOps sees: release-demo Release Bot\n", "Release Bot can't read the release pipeline's runs (404).\n")))
    assert day_one.token() == 1
    assert capsys.readouterr().out == ("Azure DevOps sees: release-demo Release Bot\n"
                                       "Release Bot can't read the release pipeline's runs (404).\n")
