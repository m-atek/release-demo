"""Demo 22's own scripts, without Azure DevOps, Entra, AWS or the tools host: fakes.

    .venv/bin/python -m pytest -q scripts/demos/azure_devops/tests
"""

import importlib.util
import json
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
