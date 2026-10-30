import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(1, str(HERE.parents[1]))

import evidence  # noqa: E402
import run_state  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(autouse=True)
def gitleaks_binary_only(monkeypatch):
    """Tests use a local gitleaks of the pinned version, never Docker."""
    import shutil
    import subprocess

    import llm

    def command():
        binary = shutil.which("gitleaks")
        if not binary:
            return None
        version = subprocess.run([binary, "version"], capture_output=True, text=True).stdout.strip().lstrip("v")
        return [binary, *llm.GITLEAKS_ARGS] if version == llm.GITLEAKS_VERSION else None
    monkeypatch.setattr(llm, "gitleaks_command", command)


@pytest.fixture(autouse=True)
def state_file(tmp_path, monkeypatch):
    """Each test gets its own run state, never the repository's."""
    monkeypatch.setattr(run_state, "PATH", tmp_path / ".release" / "state.json")
    monkeypatch.setattr(run_state, "REPORTS", tmp_path / ".release" / "reports")
    monkeypatch.delenv("AI_NOTES_REQUEST_LOG", raising=False)
    return run_state.PATH


def release_evidence(old="1.4.0", new="1.4.1"):
    """evidence.json as the pipeline writes it for a release of `new` with
    production on `old`, built from the two demo states and the fixtures'
    reports, as video 12's take has it."""
    states = evidence.between_states("orders-service", old, new)
    return {
        "run": {"service": "orders-service", "version": new, "commit": "b" * 40, "jira_key": "ORD-13",
                "jira_summary": "Read the delivery estimate from the regional setting", "build": "61",
                "build_url": "https://jenkins-demo.es.nl/job/release/61/", "change_number": "CHG0030071",
                "change_sys_id": "c" * 32, "change_type": "normal"},
        "results": {
            "tests": {**evidence.junit_counts(FIXTURES / "junit.xml"), "source": "junit.xml"},
            "quality_gate": {"result": "ok", "project": "m-atek_release-demo_orders-service", "source": "SonarQube"},
            "scan": {**evidence.grype_counts(FIXTURES / "grype.json"), "source": "grype.json"},
            "sbom": {"components": evidence.sbom_components(FIXTURES / "sbom.cdx.json"), "source": "sbom.cdx.json"},
            "package": {"name": f"orders-service-{new}.zip", "sha256": "9f" * 32, "source": "build-info.json"},
            "check_staging": {"lines": [f"ok   /health reports {new} after 2s", "Staging check passed"],
                              "source": "check-staging.txt"},
        },
        "versions": {"this": new, "production": {"function": "orders-service-production", "lambda_version": "57",
                                                 "version": old, "commit": "a" * 40}, "staging": "63"},
        "diff": {"from_version": old, "from_commit": "a" * 40, "to_commit": "b" * 40, "path": "services/orders-service",
                 **{k: v for k, v in states["diff"].items() if k not in ("from_version", "to_version")}},
        "settings": states["settings"],
        "findings": states["findings"],
    }


@pytest.fixture
def take_evidence():
    return release_evidence()


def sdk_response(data, model="claude-opus-5-5", stop_reason="end_turn", usage=None, content=None):
    """A Messages API reply as the API sends it, for a mock transport."""
    return {
        "id": "msg_test", "type": "message", "role": "assistant", "model": model,
        "content": content if content is not None else [
            {"type": "thinking", "thinking": "", "signature": "sig"},
            {"type": "text", "text": json.dumps(data)}],
        "stop_reason": stop_reason, "stop_sequence": None,
        "usage": usage or {"input_tokens": 11800, "output_tokens": 3900, "cache_read_input_tokens": 0,
                           "cache_creation_input_tokens": 1200},
    }


class Recorder:
    """A mock transport for the real SDK: keeps each request and answers with the next reply."""

    def __init__(self, *replies, status=200):
        self.replies, self.status, self.requests = list(replies), status, []

    def __call__(self, request):
        httpx2 = pytest.importorskip("httpx2")

        self.requests.append({"url": str(request.url), "headers": dict(request.headers),
                              "body": json.loads(request.content or b"{}")})
        return httpx2.Response(self.status, json=self.replies.pop(0) if self.replies else {})

    def client(self, retries=0):
        anthropic = pytest.importorskip("anthropic")  # in the worktree's .venv; see requirements-dev.txt
        httpx2 = pytest.importorskip("httpx2")

        return anthropic.Anthropic(api_key="sk-ant-test-not-a-key", max_retries=retries,
                                   http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(self)))


def no_findings(text):
    """A gitleaks stand-in that finds nothing."""
    return []
