"""Deploys wait on the service or app itself where the deployer can't read the cloud's operations."""

import pytest

import targets
from targets import AzureContainerApp, CloudError, GoogleCloudRun


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(targets.time, "sleep", lambda seconds: None)


def google(states):
    """A GoogleCloudRun whose PATCH answers an operation for generation 5 and whose reads follow states."""
    run = object.__new__(GoogleCloudRun)
    run.base = "https://run.googleapis.com/v2/projects/p/locations/l/services/s"
    run.calls, run.reads = [], 0

    def _call(method, url, body=None):
        run.calls.append(method)
        return 200, {}, {"name": "projects/p/locations/l/operations/1", "metadata": {"generation": "5"}}

    def service():
        run.reads += 1
        return states[run.reads - 1]

    run._call, run.service = _call, service
    return run


def test_google_watches_the_service_and_never_reads_the_operation():
    run = google([{"generation": "5", "observedGeneration": "4", "reconciling": True},
                  {"generation": "5", "observedGeneration": "5", "reconciling": False,
                   "terminalCondition": {"state": "CONDITION_SUCCEEDED"}}])
    run._update({"template": {}}, "deploy 1.2.0")
    assert run.calls == ["PATCH"] and run.reads == 2


def test_google_waits_for_the_patched_generation():
    run = google([{"generation": "4", "observedGeneration": "4", "reconciling": False},
                  {"generation": "5", "observedGeneration": "5", "reconciling": False}])
    run._update({}, "deploy 1.2.0")
    assert run.reads == 2


def test_google_reports_a_revision_that_failed():
    run = google([{"generation": "5", "observedGeneration": "5", "reconciling": False,
                   "terminalCondition": {"state": "CONDITION_FAILED", "message": "container failed to start"}}])
    with pytest.raises(CloudError, match="container failed to start"):
        run._update({}, "deploy 1.2.0")


def azure(monkeypatch, error, states):
    app = object.__new__(AzureContainerApp)
    app.login = type("Login", (), {"access": {"arm": "token"}})()

    def call(method, url, token, body=None, timeout=60):
        raise error

    monkeypatch.setattr(targets, "call", call)
    remaining = iter(states)
    app.app = lambda: {"provisioningState": next(remaining)}
    return app


def test_azure_watches_the_app_when_it_may_not_read_the_operation(monkeypatch):
    app = azure(monkeypatch, CloudError("GET op answered 403: no access", 403), ["InProgress", "Succeeded"])
    app._wait({"Azure-AsyncOperation": "https://management.azure.com/op"}, "deploy 1.2.0")


def test_azure_reports_a_failed_app(monkeypatch):
    app = azure(monkeypatch, CloudError("GET op answered 403: no access", 403), ["InProgress", "Failed"])
    with pytest.raises(CloudError, match="provisioning failed"):
        app._wait({"Azure-AsyncOperation": "https://management.azure.com/op"}, "deploy 1.2.0")


def test_azure_still_raises_other_errors(monkeypatch):
    app = azure(monkeypatch, CloudError("GET op answered 500: broken", 500), [])
    with pytest.raises(CloudError, match="500"):
        app._wait({"Azure-AsyncOperation": "https://management.azure.com/op"}, "deploy 1.2.0")
