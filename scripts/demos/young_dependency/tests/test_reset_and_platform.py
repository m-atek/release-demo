"""The reset's order, and that nothing here touches another version, another service or another CI."""

import importlib.util
import subprocess
from pathlib import Path

import pytest
import yaml

import run_state
from common import ROOT, service
from conftest import HERE

STATES = ROOT / "demo-states" / "orders-service"


def load_reset():
    spec = importlib.util.spec_from_file_location("young_reset", HERE / "reset.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


reset = load_reset()
REAL_SHARED = reset.shared_reset


class FakeJira:
    def __init__(self, found=None):
        self.found, self.created = found, []

    def search(self, jql, fields=""):
        return [{"key": self.found, "fields": {"summary": reset.SUMMARY}}] if self.found else []

    def create(self, kind, summary, description, labels=()):
        self.created.append((kind, summary, labels))
        return "ORD-109"


class FakeShared:
    """scripts/reset.py's steps, recorded in order."""

    def __init__(self, dry_run=False):
        self.calls, self.DEMO_ISSUES = [], []
        self.HistoryRunning = type("HistoryRunning", (Exception,), {})
        self.dry_run = dry_run

    def Reset(self, dry_run):
        real = load_shared()
        return real.Reset(dry_run)

    def __getattr__(self, name):
        def step(*args, **kwargs):
            self.calls.append(name)
            return []
        return step


def load_shared():
    return REAL_SHARED()


# The steps.

def test_only_this_videos_versions_are_archived():
    shared = load_shared()
    assert shared.BASELINE == "1.3.0"
    assert shared.after_baseline("1.32.0") and shared.after_baseline("1.32.1")
    for other in ("1.4.0", "1.12.2", "1.28.1", "1.30.0", "1.31.0", "1.33.0"):
        assert not shared.after_baseline(other)


def test_the_steps_run_in_order_and_the_package_comes_before_the_aliases(monkeypatch):
    shared = FakeShared()
    order = []
    monkeypatch.setattr(reset, "shared_reset", lambda: shared)
    monkeypatch.setattr(reset, "jira_step", lambda r, s: order.append("jira"))
    monkeypatch.setattr(reset, "on_jenkins", lambda: order.append("jenkins"))
    monkeypatch.setattr(reset, "the_package", lambda r: order.append("package"))
    assert reset.run(False, None) == 0
    assert shared.calls == ["release_jobs", "stop_runs", "cancel_changes", "github_takes", "jfrog_takes", "switches_off",
                            "lambda_aliases", "warm_slack_handler"]
    assert order == ["jira", "jenkins", "package"]


def test_no_package_fails_the_reset_and_says_so(monkeypatch, capsys):
    shared = FakeShared()
    monkeypatch.setattr(reset, "shared_reset", lambda: shared)
    monkeypatch.setattr(reset, "jira_step", lambda r, s: None)
    monkeypatch.setattr(reset, "on_jenkins", lambda: None)

    def nothing(version):
        raise SystemExit("Nothing on the watch list qualifies right now, so nothing is staged. The take waits.")

    monkeypatch.setattr(reset.pick, "stage", nothing)
    assert reset.run(False, None) == 1
    out = capsys.readouterr().out
    assert "these steps failed: 8. The package" in out and "Ready for a take" not in out


def test_a_dry_run_reads_the_index_and_stages_nothing(monkeypatch, capsys):
    shared = load_shared()
    fake = shared.Reset(True)
    monkeypatch.setattr(reset.pick, "show", lambda now=None: 0)
    monkeypatch.setattr(reset.pick, "stage", lambda version: pytest.fail("a dry run stages nothing"))
    reset.the_package(fake)
    assert fake.actions == 0


def test_a_dry_run_with_nothing_young_says_a_real_run_would_stop(monkeypatch):
    shared = load_shared()
    monkeypatch.setattr(reset.pick, "show", lambda now=None: 1)
    with pytest.raises(RuntimeError, match="would stage nothing"):
        reset.the_package(shared.Reset(True))


def test_the_package_step_stages_1_32_0():
    shared = load_shared()
    staged = []
    fake = shared.Reset(False)
    reset.the_package(fake, stage=staged.append)
    assert staged == ["1.32.0"] and fake.actions == 1


# Jira.

def test_ord_u_is_made_once(capsys, monkeypatch):
    shared = FakeShared()
    jira = FakeJira()
    real = load_shared().Reset(False)
    assert reset.jira_step(real, shared, jira) == "ORD-109"
    assert jira.created == [("Story", reset.SUMMARY, ["package-age"])]
    assert shared.DEMO_ISSUES == ["ORD-109"] and shared.calls == ["jira_versions"]
    assert "ORD-[u] is ORD-109" in capsys.readouterr().out
    again = FakeJira(found="ORD-109")
    assert reset.jira_step(real, FakeShared(), again) == "ORD-109"
    assert again.created == []


def test_a_dry_run_makes_no_issue():
    jira = FakeJira()
    assert reset.jira_step(load_shared().Reset(True), FakeShared(), jira) is None
    assert jira.created == []


# Another CI.

class FakeSSM:
    def __init__(self, value):
        self.value = value

    def get_parameters(self, Names):
        return {"Parameters": [{"Value": self.value}] if self.value else []}


@pytest.mark.parametrize("value", [None, "jenkins"])
def test_it_runs_on_jenkins(value):
    reset.on_jenkins(FakeSSM(value))


@pytest.mark.parametrize("value", ["gitlab-ci", "azure-pipelines"])
def test_it_refuses_while_another_ci_holds_release(value):
    with pytest.raises(RuntimeError, match=value):
        reset.on_jenkins(FakeSSM(value))


# No other version sees the stage.

def stages(tmp_path, version, **changes):
    folder = tmp_path / version
    folder.mkdir(exist_ok=True)
    (folder / "VERSION").write_text(version + "\n")
    config = {**service("orders-service"), "dir": folder, **changes}
    return run_state.stages_for(config)


@pytest.mark.parametrize("version", ["1.32.0", "1.32.1"])
def test_its_versions_get_the_stage_first(tmp_path, version):
    assert stages(tmp_path, version)[0:2] == ["Package age", "Build"]


@pytest.mark.parametrize("version", ["1.3.0", "1.4.0", "1.12.1", "1.28.1", "1.30.0", "1.31.0", "1.32.2", "1.33.0"])
def test_no_other_version_gets_it(tmp_path, version):
    assert "Package age" not in stages(tmp_path, version)


def test_no_other_service_gets_it():
    for name, config in yaml.safe_load((ROOT / "services.yaml").read_text()).items():
        if name != "orders-service":
            assert "package_age" not in config and "package_age" not in (config.get("scripts") or {})


def test_a_config_without_the_setting_gets_nothing(tmp_path):
    config = service("orders-service")
    assert "Package age" not in stages(tmp_path, "1.32.0", package_age={})
    assert "Package age" not in stages(tmp_path, "1.32.0", scripts={k: v for k, v in config["scripts"].items() if k != "package_age"})


def test_the_setting_is_a_setting():
    settings = service("orders-service")["package_age"]
    assert settings == {"min_hours": 72, "versions": ["1.32.0", "1.32.1"]}
    assert (ROOT / service("orders-service")["scripts"]["package_age"]).exists()


# The pipeline.

PIPELINE = (ROOT / "jenkins" / "shared-library" / "vars" / "releasePipeline.groovy").read_text()
GROOVY = (ROOT / "jenkins" / "shared-library" / "vars" / "packageAge.groovy").read_text()


def test_the_stage_runs_before_build_and_only_where_it_is_planned():
    start = PIPELINE.index("if (planned('Package age'))")
    assert start < PIPELINE.index("releaseStage('Build')")
    assert PIPELINE.index("releaseStage('Error budget')") < start
    assert "releaseStage('Package age') { packageAge.run(cfg) }" in PIPELINE


def test_the_approval_hook_is_behind_the_same_test():
    approved = PIPELINE.index("packageAge.approved(cfg)")
    assert PIPELINE.rindex("if (planned('Package age'))", 0, approved) > PIPELINE.index("releaseStage('Approval')")
    assert approved < PIPELINE.index("releaseStage('Production'")


def test_groovy_exit_3_refuses_and_anything_else_goes_to_a_person():
    assert "status == 3" in GROOVY and "force_normal=true" in GROOVY and "--no-reading" in GROOVY
    assert ".release/reports/package-age.json" in GROOVY


def test_the_other_cis_never_call_it():
    for name in (".gitlab-ci.yml", "azure-pipelines.yml", ".github/workflows/release.yml", "jenkins/rollback.Jenkinsfile"):
        path = ROOT / name
        if path.exists():
            assert "package_age" not in path.read_text() and "Package age" not in path.read_text()


# host.stage.

def test_host_stage_runs_stage_py_unless_told_otherwise(monkeypatch):
    import host

    seen = []

    def run(command, **kwargs):
        seen.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(host, "ssm_client", lambda: None)
    monkeypatch.setattr(host, "github_app_token", lambda client: ("token", "owner/repo", None))
    monkeypatch.setattr(host.subprocess, "run", run)
    host.stage("orders-service", "1.4.0")
    assert seen[-1][1:] == ["scripts/stage.py", "orders-service", "1.4.0"]
    host.stage("orders-service", "1.32.0", wrapper=["scripts/demos/young_dependency/pick.py", "write", "1.32.0"])
    assert seen[-1][1:] == ["scripts/demos/young_dependency/pick.py", "write", "1.32.0"]


# The states.

@pytest.mark.parametrize("version", ["1.32.0", "1.32.1"])
def test_a_state_says_its_version_and_shows_no_demo_tag(version):
    state = STATES / version
    assert (state / "VERSION").read_text().strip() == version
    assert "Demo system" not in (state / "handler.py").read_text()
    assert "import boto3" in (state / "handler.py").read_text()


def test_only_the_version_and_the_pins_differ_between_the_two():
    first, second = STATES / "1.32.0", STATES / "1.32.1"
    assert (first / "handler.py").read_text() == (second / "handler.py").read_text()
    assert (first / "tests" / "test_handler.py").read_text() == (second / "tests" / "test_handler.py").read_text()
    left, right = (first / "requirements.txt").read_text().splitlines(), (second / "requirements.txt").read_text().splitlines()
    assert [a.split("==")[0] for a in left] == [a.split("==")[0] for a in right]
    assert {a.split("==")[0] for a, b in zip(left, right) if a != b} == {"boto3", "botocore"}


def test_every_package_the_sdk_brings_is_pinned():
    names = {line.split("==")[0] for line in (STATES / "1.32.0" / "requirements.txt").read_text().splitlines() if "==" in line}
    assert names == {"boto3", "botocore", "jmespath", "python-dateutil", "s3transfer", "six", "urllib3"}


# Words.

def test_docs_name_it():
    assert "scripts.package_age" in (ROOT / "docs" / "adding-a-demo.md").read_text()
    assert "a young dependency (video 30)" in (ROOT / "README.md").read_text()


def test_no_long_dash_anywhere():
    files = [*HERE.rglob("*.py"), *HERE.rglob("*.sh"), ROOT / "jenkins" / "shared-library" / "vars" / "packageAge.groovy",
             *(STATES / "1.32.0").rglob("*.py"), *(STATES / "1.32.1").rglob("*.py")]
    for path in files:
        assert not set(Path(path).read_text()) & {chr(0x2013), chr(0x2014)}, path
