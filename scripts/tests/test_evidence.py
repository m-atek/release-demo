"""scripts/evidence.py: the readers video 11 shares, the settings and findings
of video 12's acceptance check 5, and that no setting value reaches the file."""

import io
import json
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import evidence  # noqa: E402
import run_state  # noqa: E402

FIXTURES = Path(__file__).resolve().parents[1] / "demos" / "ai_notes" / "tests" / "fixtures"


@pytest.fixture(autouse=True)
def state_file(tmp_path, monkeypatch):
    monkeypatch.setattr(run_state, "PATH", tmp_path / ".release" / "state.json")
    monkeypatch.setattr(run_state, "REPORTS", tmp_path / ".release" / "reports")


def test_the_readers():
    assert evidence.junit_counts(FIXTURES / "junit.xml") == {"total": 10, "passed": 10, "failed": 0, "errors": 0,
                                                             "skipped": 0}
    assert evidence.grype_counts(FIXTURES / "grype-findings.json") == {
        "critical": 0, "high": 1, "medium": 2, "low": 0, "negligible": 0, "unknown": 1, "total": 4}
    assert evidence.sbom_components(FIXTURES / "sbom.cdx.json") == 3
    assert evidence.base64_to_hex("n4bQgYhMfWWaL+qgxVrQFaO/TxsrC4Is0V1sFbDwCgg=") == \
        "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
    keys = [f"CHG00300{n:02d}" for n in range(20)]
    assert evidence.ranked(4127, keys) == evidence.ranked(4127, list(reversed(keys))) != evidence.ranked(4128, keys)


@pytest.mark.parametrize("old, new, found", [
    ("1.4.0", "1.4.1", [("F1", "DELIVERY_ESTIMATE_DAYS", "handler.py", ["staging"], ["production"], True)]),
    ("1.3.0", "1.4.0", []),
    ("1.2.3", "1.2.4", [("F1", "STORES", "handler.py", [], ["staging", "production"], True)]),
    ("1.2.7", "1.2.8", [("F1", "BRAND_TITLE", "handler.py", [], ["staging", "production"], True)]),
])
def test_the_findings_of_the_releases_the_videos_show(old, new, found):
    result = evidence.between_states("orders-service", old, new)
    assert [(f["id"], f["setting"], f["file"], f["defined_in"], f["missing_in"], f["new"])
            for f in result["findings"]] == found


def test_optional_reads_and_runtime_names_are_no_findings():
    reads = evidence.reads_in({"handler.py": "import os\nfrom os import environ\n"
                                             "a = os.environ.get('A_B', 'x')\nb = os.getenv('C_D')\n"
                                             "c = os.environ['AWS_REGION']\nd = environ['E_F']\n"
                                             "e = 'G_H' in os.environ\nos.environ['I_J'] = '1'\n"})
    assert [(r["name"], r["required"]) for r in reads] == [("A_B", False), ("C_D", False), ("AWS_REGION", True),
                                                           ("E_F", True), ("G_H", False)]
    found = evidence.findings_for(reads, {"staging": ["E_F"], "production": []})
    assert [(f["setting"], f["missing_in"], f["new"]) for f in found] == [("E_F", ["production"], None)]


def test_the_live_configuration_is_what_a_new_version_gets():
    config = {"platform": "aws-lambda", "environments": {"staging": {"settings": {"DELIVERY_ESTIMATE_DAYS": "2"}},
                                                         "production": {"settings": {"DELIVERY_DAYS": "2"}}}}
    defined, sources, differences = evidence.defined_settings(config, {"production": ["DELIVERY_DAYS", "ENVIRONMENT",
                                                                                      "MESSAGES_TABLE"]})
    assert defined == {"staging": ["DELIVERY_ESTIMATE_DAYS", "ENVIRONMENT"],
                       "production": ["DELIVERY_DAYS", "ENVIRONMENT", "MESSAGES_TABLE"]}
    assert sources == {"staging": "services.yaml", "production": "live function configuration"}
    assert differences == ["production: the live configuration has MESSAGES_TABLE, services.yaml doesn't"]


class FakeLambda:
    def __init__(self, variables):
        self.variables = variables

    def get_alias(self, FunctionName, Name):
        return {"FunctionVersion": "57"}

    def get_function(self, FunctionName, Qualifier):
        return {"Code": {"Location": "https://lambda-package.example/57.zip"}}

    def get_function_configuration(self, FunctionName, Qualifier=None):
        return {"Environment": {"Variables": self.variables[FunctionName]}}


class FakeSession:
    def __init__(self, variables):
        self.variables = variables

    def client(self, name):
        return FakeLambda(self.variables)


def package(version, commit):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr("VERSION", version + "\n")
        archive.writestr("build_info.json", json.dumps({"service": "orders-service", "version": version, "commit": commit}))
    return data.getvalue()


def test_a_run_s_evidence_names_settings_and_never_their_values(tmp_path, monkeypatch):
    root = evidence.ROOT / "demo-states" / "orders-service"
    states = {commit: {path.relative_to(root / version).as_posix(): path.read_text()
                       for path in (root / version).rglob("*") if path.is_file() and "__pycache__" not in path.parts}
              for commit, version in (("a" * 40, "1.4.0"), ("b" * 40, "1.4.1"))}
    monkeypatch.setattr(evidence, "has_commit", lambda commit: commit in states)
    monkeypatch.setattr(evidence, "text_files_at", lambda commit, path: states[commit])
    monkeypatch.setattr(evidence, "python_files", lambda directory: evidence.python_only(states["b" * 40]))

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False
    monkeypatch.setattr(evidence.urllib.request, "urlopen",
                        lambda url, timeout: Response(package("1.4.0", "a" * 40)))
    reports = tmp_path / ".release" / "reports"
    reports.mkdir(parents=True)
    for name in ("junit.xml", "grype.json", "sbom.cdx.json"):
        (reports / name).write_bytes((FIXTURES / name).read_bytes())
    (tmp_path / ".release" / "check-staging.txt").write_text("ok   /health reports 1.4.1 after 2s\nStaging check passed\n")
    secret_values = {"orders-service-staging": {"DELIVERY_ESTIMATE_DAYS": "value-staging-7731", "ENVIRONMENT": "staging"},
                     "orders-service-production": {"DELIVERY_DAYS": "value-production-7732", "ENVIRONMENT": "production"}}
    state = {"service": "orders-service", "version": "1.4.1", "commit": "b" * 40, "jira_key": "ORD-13",
             "build": {"number": "61", "url": "https://jenkins-demo.es.nl/job/release/61/"},
             "stages": {"Quality gate": {"status": "ok"}}, "deploys": {"staging": {"number": "63"}},
             "package": {"name": "orders-service-1.4.1.zip", "sha256": "9f" * 32}}
    result = evidence.collect(state, reports=reports, dist=tmp_path / "dist", reader=FakeSession(secret_values),
                              summary="Read the delivery estimate from the regional setting")
    text = json.dumps(result)
    assert "7731" not in text and "7732" not in text
    assert result["versions"]["production"] == {"function": "orders-service-production", "lambda_version": "57",
                                                "version": "1.4.0", "commit": "a" * 40}
    assert result["settings"]["defined"] == {"staging": ["DELIVERY_ESTIMATE_DAYS", "ENVIRONMENT"],
                                             "production": ["DELIVERY_DAYS", "ENVIRONMENT"]}
    assert [f["setting"] for f in result["findings"]] == ["DELIVERY_ESTIMATE_DAYS"]
    assert result["results"]["tests"]["passed"] == 10 and result["results"]["check_staging"]["lines"][1] == \
        "Staging check passed"
    assert result["diff"]["from_version"] == "1.4.0" and "handler.py" in [f["file"] for f in result["diff"]["files"]]


def test_without_the_reader_role_the_evidence_says_why(tmp_path):
    class Broken:
        def client(self, name):
            raise RuntimeError("AccessDenied")
    state = {"service": "orders-service", "version": "1.4.1", "commit": "b" * 40, "jira_key": "ORD-13",
             "build": {"number": "61"}, "stages": {}}
    result = evidence.collect(state, reports=tmp_path, dist=tmp_path, reader=Broken())
    assert result["versions"]["production"]["unavailable"].startswith("the evidence reader role couldn't read")
    assert result["diff"] == {"unavailable": "production's version is unknown"}
    assert result["results"]["tests"] == {"unavailable": "no junit.xml"}


def test_the_change_is_added_once_it_exists(tmp_path, monkeypatch):
    out = tmp_path / "evidence.json"
    out.write_text(json.dumps({"run": {"service": "orders-service", "change_number": None}}))
    run_state.save({"service": "orders-service", "version": "1.4.1", "build": {},
                    "change": {"number": "CHG0030071", "sys_id": "c" * 32, "type": "normal"}})
    monkeypatch.setattr(sys, "argv", ["evidence.py", "--out", str(out), "--change"])
    evidence.main()
    assert json.loads(out.read_text())["run"] == {"service": "orders-service", "change_number": "CHG0030071",
                                                  "change_sys_id": "c" * 32, "change_type": "normal"}
