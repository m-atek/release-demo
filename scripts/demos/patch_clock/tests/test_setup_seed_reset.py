"""The rest of video 24: the three demo states, the seed, the backfill, setup, the reset, what status warns
about, the fallback switch, and the pieces infra/ and the jobs add. Every outside call is a fake.

    .venv/bin/python -m pytest -q scripts/demos/patch_clock/tests
"""

import difflib
import importlib.util
import io
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import backfill
import candidates
import clock
import demo
import host
import live as readers
import publish
import seed
import setup
import tools
import use_fallback
from conftest import HERE, ORDERS, Functions, Store, World, b64digest, hexdigest, saved_bytes

ROOT = HERE.parents[2]
STATES = ROOT / "demo-states" / "orders-service"


def load_reset():
    spec = importlib.util.spec_from_file_location("patch_clock_reset", HERE / "reset.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The demo states.

def state_files(version):
    folder = STATES / version
    return {path.relative_to(folder).as_posix(): path.read_text() for path in sorted(folder.rglob("*"))
            if path.is_file() and "__pycache__" not in path.parts}


def changed_lines(old, new):
    return [line for line in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0)
            if line[:1] in "+-" and not line.startswith(("+++", "---"))]


def pytest_in(state):
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"], cwd=STATES / state, capture_output=True, text=True)


@pytest.mark.parametrize("state", ["1.12.0", "1.12.1", "1.12.2"])
def test_each_states_suite_passes(state):
    done = pytest_in(state)
    assert done.returncode == 0, done.stdout


def test_the_states_pin_what_the_spec_says():
    chosen = candidates.chosen()
    assert (STATES / "1.12.0" / "VERSION").read_text().strip() == "1.12.0"
    for state, version in (("1.12.0", chosen.bad), ("1.12.1", chosen.bad), ("1.12.2", chosen.good)):
        assert (STATES / state / "requirements.txt").read_text() == use_fallback.requirements(candidates.CHOSEN, version)
    assert (STATES / "1.12.0" / "requirements.txt").read_text().startswith("# Compresses /api/orders for the store apps.\n")


def test_1_12_2_is_1_12_1_with_those_two_lines():
    old, new = state_files("1.12.1"), state_files("1.12.2")
    assert set(old) == set(new)
    assert {name for name in old if old[name] != new[name]} == {"VERSION", "requirements.txt"}
    chosen = candidates.chosen()
    assert changed_lines(old["requirements.txt"], new["requirements.txt"]) == [f"-{chosen.package}=={chosen.bad}", f"+{chosen.package}=={chosen.good}"]
    assert changed_lines(old["VERSION"], new["VERSION"]) == ["-1.12.1", "+1.12.2"]


def test_1_12_1_adds_only_the_small_line_to_the_handler():
    old, new = state_files("1.12.0"), state_files("1.12.1")
    assert old["requirements.txt"] == new["requirements.txt"]
    added = changed_lines(old["handler.py"], new["handler.py"])
    assert all(line.startswith("+") for line in added) and len(added) == 3
    assert any("Packed today: {packed_today}" in line for line in added)


@pytest.mark.parametrize("state", ["1.12.0", "1.12.1", "1.12.2"])
def test_no_state_decodes_or_carries_the_demo_tag(state):
    handler = (STATES / state / "handler.py").read_text()
    assert "decompress" not in handler.lower()
    assert "Demo system" not in handler and ".demo" not in handler
    # The import sits inside the function, as 1.6.0's pg8000 does: a plain request never loads it.
    assert not re.search(r"^import brotli", handler, re.M) and re.search(r"^    import brotli", handler, re.M)


def test_1_12_0_is_1_4_0_plus_the_compression_and_minus_the_tag():
    removed = [line for line in changed_lines((STATES / "1.4.0" / "handler.py").read_text(), (STATES / "1.12.0" / "handler.py").read_text()) if line.startswith("-")]
    assert any('class="demo"' in line for line in removed)
    assert all(not re.search(r"def |return |PAGE", line) for line in removed), removed


# The seed.

class DeployClient(Functions):
    """Lambda as the deploy role sees it."""

    def __init__(self, sha_of=None):
        super().__init__({})
        self.code, self.versions, self.aliases = {}, {}, {}
        self.published = []

    def update_function_code(self, FunctionName, ZipFile):
        self.code[FunctionName] = ZipFile

    def get_waiter(self, name):
        return SimpleNamespace(wait=lambda FunctionName: None)

    def publish_version(self, FunctionName, Description):
        number = str(len(self.published) + 30)
        self.published.append((FunctionName, Description, number))
        version = {"Version": number, "Description": Description, "CodeSha256": b64digest(self.code[FunctionName])}
        self.versions.setdefault(FunctionName, []).append(version)
        return version

    def update_alias(self, FunctionName, Name, FunctionVersion):
        assert Name == "live"
        self.aliases[FunctionName] = FunctionVersion

    def get_alias(self, FunctionName, Name):
        return {"FunctionVersion": self.aliases.get(FunctionName, "25")}

    def get_function_url_config(self, FunctionName, Qualifier):
        return {"FunctionUrl": f"https://{FunctionName}.lambda-url.example/"}

    def get_paginator(self, name):
        assert name == "list_versions_by_function"
        client = self
        return SimpleNamespace(paginate=lambda FunctionName: [{"Versions": client.versions.get(FunctionName, [])}])


@pytest.fixture
def jcr(monkeypatch):
    store = Store()
    monkeypatch.setattr(publish, "request", store.request)
    return store


@pytest.fixture
def built(tmp_path, monkeypatch):
    """dist/orders-service as toolchain.build leaves it, and Syft faked."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("handler.py", "print('orders-service')\n")
        archive.writestr("brotli-1.2.0.dist-info/METADATA", "Name: brotli\nVersion: 1.2.0\n")
    package = buffer.getvalue()
    out = tmp_path / "dist" / "orders-service"
    out.mkdir(parents=True)
    (out / "orders-service-1.12.0.zip").write_bytes(package)
    info = {"service": "orders-service", "version": "1.12.0", "commit": "c" * 40, "jira_key": "seed", "package": "orders-service-1.12.0.zip",
            "sha256": hexdigest(package)}
    (out / "build-info.json").write_text(json.dumps(info))
    monkeypatch.setenv("WORKSPACE", str(tmp_path))
    monkeypatch.setattr(tools, "CACHE", tmp_path / "cache")
    return SimpleNamespace(package=package, info=info, out=out)


def test_the_seed_stores_its_files_deploys_both_and_checks_both(built, jcr, monkeypatch):
    unpacked = []

    def syft(package_dir, out_dir, service_name):
        unpacked.append(sorted(path.name for path in Path(package_dir).iterdir()))
        assert service_name == "orders-service"
        return saved_bytes("sbom-brotli-1.2.0.cdx.json")

    monkeypatch.setattr(tools, "syft", syft)
    monkeypatch.setattr(tools, "grype", lambda *a, **k: pytest.fail("the seed runs no Grype"))
    monkeypatch.setattr(seed, "get_parameter", lambda ssm, name: "token")
    monkeypatch.setattr(seed, "ssm_client", lambda: None)
    client = DeployClient()
    import deploy

    monkeypatch.setattr(deploy, "lambda_client", lambda name: client)
    checks = []
    monkeypatch.setattr(seed.subprocess, "run", lambda command: checks.append(command[2:]) or subprocess.CompletedProcess(command, 0))
    assert seed.main() == 0
    assert unpacked == [["brotli-1.2.0.dist-info", "handler.py"]]
    assert sorted(jcr.files) == sorted(f"orders-service-candidates/1.12.0/seed/{name}" for name in seed.FILES)
    assert client.published == [("orders-service-staging", "1.12.0", "30"), ("orders-service-production", "1.12.0", "31")]
    assert client.aliases == {"orders-service-staging": "30", "orders-service-production": "31"}
    assert [(c[0], c[1], c[3]) for c in checks] == [("orders-service", "staging", "1.12.0"), ("orders-service", "production", "1.12.0")]
    tied = json.loads(jcr.files[f"{seed.SEED}/sbom-info.json"])
    assert tied["package_sha256"] == built.info["sha256"]


def test_nothing_the_seed_writes_claims_a_scan_passed(built, jcr, monkeypatch):
    seed.upload("token", built.package, built.info, b"{}")
    for name in ("build-info.json", "sbom-info.json"):
        text = jcr.files[f"{seed.SEED}/{name}"].decode().lower()
        assert not re.search(r"scan|grype|passed", text), name
    assert not any("grype" in name for name in jcr.files)
    source = (HERE / "seed.py").read_text()
    assert "run_state" not in source and "change.py" not in source and "notify" not in source and "events" not in source.replace("no event", "")


def test_a_second_different_seed_stops_before_anything_moves(built, jcr):
    jcr.files[f"{seed.SEED}/orders-service-1.12.0.zip"] = b"the first seed's package"
    with pytest.raises(SystemExit):
        seed.upload("token", built.package, built.info, b"{}")
    assert jcr.puts == []


def test_the_same_seed_again_is_fine_even_from_another_build(built, jcr):
    seed.upload("token", built.package, built.info, b"{}")
    jcr.puts.clear()
    seed.upload("token", built.package, {**built.info, "build_url": "https://jenkins-demo.es.nl/job/patch-clock-seed/9/"}, b"{}")
    assert jcr.puts == []


def test_the_seed_job_is_admin_only_and_builds_in_its_workspace_alone():
    jenkinsfile = (ROOT / "jenkins" / "jobs" / "patch-clock-seed.Jenkinsfile").read_text()
    assert "getBuildCauses('hudson.model.Cause$UserIdCause')" in jenkinsfile and "userId != 'admin'" in jenkinsfile
    assert "stage.py orders-service 1.12.0 --no-commit" in jenkinsfile and "toolchain.build('orders-service', 'seed', cfg)" in jenkinsfile
    assert "sh 'python3 scripts/demos/patch_clock/seed.py'" in jenkinsfile
    code = "\n".join(line for line in jenkinsfile.splitlines() if not line.startswith("//"))
    assert not re.search(r"sbomAndScan|grype|notify\.py|events\.py|change\.py|run_state", code, re.I)
    jobs = (ROOT / "jenkins" / "jobs" / "jobs.groovy").read_text()
    assert "pipelineJob('patch-clock-seed')" in jobs and "fromRepository('jenkins/jobs/patch-clock-seed.Jenkinsfile')" in jobs


# The backfill.

class Act:
    def __init__(self):
        self.done = []

    def __call__(self, what, do):
        self.done.append(what)
        do()


@pytest.fixture
def old_release(world, monkeypatch):
    """pricing-api released before SBOMs travelled: a folder without one."""
    monkeypatch.setattr(publish, "request", world.store.request)
    monkeypatch.setattr(readers, "JFrog", lambda token: world.store)
    for name in ("sbom.cdx.json", "sbom-info.json"):
        del world.store.files[f"demos-releases/pricing-api/2.1.2/{name}"]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("handler.py", "print('pricing-api')\n")
    package = buffer.getvalue()
    world.store.files["demos-releases/pricing-api/2.1.2/package.zip"] = package
    info = json.loads(world.store.files["demos-releases/pricing-api/2.1.2/build-info.json"])
    info["sha256"] = hexdigest(package)
    world.store.files["demos-releases/pricing-api/2.1.2/build-info.json"] = json.dumps(info).encode()
    world.functions.configs["pricing-api-production"]["CodeSha256"] = b64digest(package)
    monkeypatch.setattr(tools, "syft", lambda package_dir, out_dir, name: saved_bytes("sbom-empty.cdx.json"))
    return world


def test_the_backfill_makes_what_a_folder_lacks_and_the_search_then_trusts_it(old_release):
    act = Act()
    assert backfill.backfill(["pricing-api"], act, client=old_release.functions, token="token") == 1
    assert act.done == ["make the SBOM of pricing-api 2.1.2 and store it in demos-releases/pricing-api/2.1.2"]
    assert old_release.store.puts == ["demos-releases/pricing-api/2.1.2/sbom.cdx.json", "demos-releases/pricing-api/2.1.2/sbom-info.json"]
    what = readers.read_live(old_release.functions, readers.searchable()["pricing-api"])
    sbom, why_not = readers.stored_sbom(old_release.store, "pricing-api", what)
    assert why_not is None and sbom == saved_bytes("sbom-empty.cdx.json")


def test_the_backfill_leaves_a_folder_with_an_sbom_alone(old_release, capsys):
    act = Act()
    assert backfill.backfill(["orders-service", "quote-service"], act, client=old_release.functions, token="token") == 2
    assert act.done == [] and old_release.store.puts == []


def test_the_backfill_checks_the_packages_checksum(old_release):
    old_release.store.files["demos-releases/pricing-api/2.1.2/package.zip"] = b"changed after it was published"
    with pytest.raises(SystemExit):
        backfill.backfill(["pricing-api"], Act(), client=old_release.functions, token="token")
    assert old_release.store.puts == []


def test_with_no_release_copy_of_what_is_live_the_backfill_makes_nothing(old_release, capsys):
    old_release.functions.configs["pricing-api-production"]["CodeSha256"] = b64digest(b"not that package")
    assert backfill.backfill(["pricing-api"], Act(), client=old_release.functions, token="token") == 0
    assert "no release copy of what is live" in capsys.readouterr().out and old_release.store.puts == []


def test_from_the_mac_the_backfill_hands_itself_to_the_tools_host(monkeypatch):
    calls = []
    monkeypatch.setattr(host, "on_host", lambda: False)
    monkeypatch.setattr(host, "hand_over", lambda name, script, args, **kwargs: calls.append((name, script, args)) or 0)
    monkeypatch.setattr(sys, "argv", ["backfill.py", "orders-service", "--dry-run"])
    assert backfill.main() == 0
    assert calls == [("patch-clock-backfill", "demos/patch_clock/backfill.py", ["orders-service", "--dry-run"])]


# Setup.

class FakeJira:
    def __init__(self):
        self.issues, self.transitions = [], []

    def search(self, jql, fields="summary,status", limit=50):
        phrase = re.search(r'summary ~ "\\"(.+?)\\""', jql).group(1)
        return [issue for issue in self.issues if phrase in issue["fields"]["summary"]]

    def create(self, kind, summary, description, labels=(), due=None, fix_version=None):
        key = f"ORD-{103 + len(self.issues)}"
        assert labels == ["orders-service"]
        self.issues.append({"key": key, "kind": kind, "fields": {"summary": summary, "status": {"name": "Backlog", "statusCategory": {"key": "new"}}}})
        return key

    def transition(self, key, target):
        self.transitions.append((key, target))
        return True


@pytest.fixture
def jira(monkeypatch):
    import jira as jira_module

    fake = FakeJira()
    monkeypatch.setattr(jira_module, "Jira", lambda: fake)
    return fake


def test_setup_makes_the_two_keys_in_order_prints_them_and_makes_nothing_twice(jira, capsys):
    assert setup.issues() == ["ORD-103", "ORD-104"]
    out = capsys.readouterr().out
    assert "ORD-103: Show how many orders are packed on the order page (made now). It is ORD-[f] in the script." in out
    assert "ORD-104: Patch brotli to 1.2.1 (CVE-2026-111497) (made now). It is ORD-[p] in the script." in out
    assert setup.issues() == ["ORD-103", "ORD-104"] and len(jira.issues) == 2


def test_a_dry_run_makes_nothing(jira):
    setup.issues(dry_run=True)
    assert jira.issues == []


class FakeJenkins:
    """Jenkins as admin: a crumb, then the job's builds."""

    def __init__(self, result="SUCCESS"):
        self.result, self.calls, self.last = result, [], {"number": 2, "building": False, "result": "SUCCESS"}

    def call(self, method, path, body=None, headers=None, auth=None):
        self.calls.append((method, path, headers))
        if path == "/crumbIssuer/api/json":
            return 200, {"crumbRequestField": "Jenkins-Crumb", "crumb": "abc"}
        if method == "POST":
            self.last = {"number": 3, "building": False, "result": self.result}
            return 201, ""
        return 200, {"lastBuild": self.last}


def test_setup_starts_the_seed_as_admin_with_a_crumb_and_waits(monkeypatch):
    jenkins = FakeJenkins()
    monkeypatch.setattr(setup, "admin_client", lambda: (jenkins, {"Jenkins-Crumb": "abc"}))
    assert setup.run_seed(sleep=lambda seconds: None) == 3
    assert ("POST", "/job/patch-clock-seed/build?delay=0sec", {"Jenkins-Crumb": "abc"}) in jenkins.calls


def test_a_failed_seed_stops_the_caller(monkeypatch):
    monkeypatch.setattr(setup, "admin_client", lambda: (FakeJenkins("FAILURE"), {}))
    with pytest.raises(SystemExit):
        setup.run_seed(sleep=lambda seconds: None)


# The reset.

class ResetWorld(World):
    """Where a first reset finds things: no seed, production on 1.3.0, main on 1.3.0, a take's clock left."""

    def __init__(self):
        super().__init__()
        self.main_version = "1.3.0"
        self.deploy = DeployClient()
        self.steps = []
        self.seed_runs = 0
        self.switch = "off"
        self.url_calls = []


@pytest.fixture
def resetting(world, jira, monkeypatch, tmp_path):
    reset_module = load_reset()
    fake = ResetWorld()
    fake.store = world.store
    for name in list(fake.store.files):
        if name.startswith("orders-service-releases/"):
            del fake.store.files[name]
    fake.functions = world.functions
    monkeypatch.setattr(publish, "request", fake.store.request)
    monkeypatch.setattr(readers, "JFrog", lambda token: fake.store)
    monkeypatch.setattr(reset_module, "get_parameter", lambda ssm, name: "token")
    monkeypatch.setattr(reset_module, "ssm_client", lambda: None)
    monkeypatch.setattr(setup, "get_parameter", lambda ssm, name: "token")
    monkeypatch.setattr(setup, "ssm_client", lambda: None)
    setup.issues()

    def run_seed():
        fake.seed_runs += 1
        package = ORDERS
        info = {"service": "orders-service", "version": "1.12.0", "package": seed.PACKAGE, "sha256": hexdigest(package)}
        seed.upload("token", package, info, saved_bytes("sbom-brotli-1.2.0.cdx.json"))
        for function in ("orders-service-staging", "orders-service-production"):
            fake.deploy.update_function_code(function, package)
            fake.deploy.publish_version(function, "1.12.0")

    monkeypatch.setattr(setup, "run_seed", run_seed)
    import deploy

    monkeypatch.setattr(deploy, "lambda_client", lambda name: fake.deploy)
    import check

    monkeypatch.setattr(check, "wait_for_version", lambda url, version, timeout: (fake.url_calls.append(url) or True, version))
    monkeypatch.setattr(reset_module, "http_get", lambda url, timeout: (200, "the page"))
    monkeypatch.setattr(host, "main_version", lambda path: fake.main_version)
    monkeypatch.setattr(host, "stage", lambda name, version: setattr(fake, "main_version", version))
    monkeypatch.setattr(tools, "db_update", lambda: "updated")
    monkeypatch.setattr(tools, "db_status", lambda: {"built": "2026-11-02T00:00:00Z", "valid": True})
    monkeypatch.setattr(reset_module.SWITCH, "is_on", lambda ssm=None: fake.switch == "on")
    monkeypatch.setattr(reset_module.SWITCH, "set", lambda value, ssm=None: setattr(fake, "switch", value))
    monkeypatch.setattr(tools, "syft", lambda package_dir, out_dir, name: saved_bytes("sbom-empty.cdx.json"))

    real_shared = reset_module.shared_reset

    def shared_stubbed():
        shared = real_shared()
        for name in ("stop_runs", "cancel_changes", "github_takes", "jfrog_takes", "jira_versions", "warm_slack_handler", "switches_off"):
            monkeypatch.setattr(shared, name, lambda *args, name=name: fake.steps.append(name))
        shared.release_jobs = lambda: []
        fake.shared = shared
        return shared

    monkeypatch.setattr(reset_module, "shared_reset", shared_stubbed)
    clock.start("CVE-2026-111497", 1793000000.0, {"orders-service": {"package": "brotli", "version": "1.2.0"}})
    fake.reset_module = reset_module
    return fake


def test_a_first_reset_ends_ready_for_a_take(resetting, capsys):
    assert resetting.reset_module.run(False, None) == 0
    out = capsys.readouterr().out
    assert out.rstrip().endswith("Ready for a take: production on 1.12.0, 1.12.1 on main, 3 live services with SBOMs.")
    assert resetting.seed_runs == 1 and resetting.main_version == "1.12.1" and resetting.switch == "on"
    assert clock.open_clocks() == []
    assert resetting.deploy.aliases == {"orders-service-staging": "30", "orders-service-production": "31"}
    assert sorted(name for name in resetting.store.files if name.startswith("orders-service-releases/1.12.0/")) == \
        sorted(f"orders-service-releases/1.12.0/{name}" for name in seed.FILES)
    assert resetting.steps == ["stop_runs", "cancel_changes", "github_takes", "jfrog_takes", "jira_versions", "warm_slack_handler", "switches_off"]
    assert len(resetting.url_calls) == 2


def test_a_second_run_finds_nothing_to_do(resetting, capsys):
    assert resetting.reset_module.run(False, None) == 0
    capsys.readouterr()
    puts = list(resetting.store.puts)
    assert resetting.reset_module.run(False, None) == 0
    out = capsys.readouterr().out
    assert "Reset in" in out and ": 0 action(s)." in out
    assert resetting.seed_runs == 1 and resetting.store.puts == puts


def test_a_first_reset_makes_the_two_issues_when_setup_never_ran(resetting, jira, capsys):
    jira.issues.clear()
    assert resetting.reset_module.run(False, None) == 0
    out = capsys.readouterr().out
    assert [issue["fields"]["summary"] for issue in jira.issues] == list(setup.summaries())
    assert "It is ORD-[f] in the script." in out and "It is ORD-[p] in the script." in out
    assert resetting.reset_module.run(False, None) == 0
    out = capsys.readouterr().out
    assert len(jira.issues) == 2 and ": 0 action(s)." in out
    assert f"{jira.issues[0]['key']} is ORD-[f]: Backlog" in out and f"{jira.issues[1]['key']} is ORD-[p]: Backlog" in out


def test_a_dry_run_makes_no_issue_either(resetting, jira, capsys):
    jira.issues.clear()
    assert resetting.reset_module.run(True, None) == 0
    assert jira.issues == [] and "would make ORD-[f] and ORD-[p] in Jira" in capsys.readouterr().out


def test_a_dry_run_changes_nothing(resetting, capsys):
    assert resetting.reset_module.run(True, None) == 0
    out = capsys.readouterr().out
    assert "Dry run in" in out and "Ready for a take" not in out and resetting.seed_runs == 0
    assert resetting.main_version == "1.3.0" and resetting.switch == "off" and len(clock.open_clocks()) == 1
    assert "would run the job patch-clock-seed" in out and "would stage orders-service 1.12.1" in out and "would turn patch-clock/enabled on" in out
    assert "would run grype db update once" in out


def test_the_aliases_go_only_to_the_version_with_the_seeds_checksum(resetting):
    assert resetting.reset_module.run(False, None) == 0
    fake = resetting.deploy
    fake.versions["orders-service-production"].append({"Version": "99", "Description": "1.12.0", "CodeSha256": b64digest(b"another package")})
    fake.aliases["orders-service-production"] = "25"
    resetting.reset_module.lambda_aliases(resetting.shared.Reset(False))
    assert fake.aliases["orders-service-production"] == "31"


def test_the_aliases_never_take_latest_for_a_version(resetting):
    assert resetting.reset_module.run(False, None) == 0
    fake = resetting.deploy
    fake.versions["orders-service-production"].append({"Version": "$LATEST", "Description": "1.12.0", "CodeSha256": b64digest(ORDERS)})
    fake.aliases["orders-service-production"] = "25"
    resetting.reset_module.lambda_aliases(resetting.shared.Reset(False))
    assert fake.aliases["orders-service-production"] == "31"


def test_no_version_with_the_seeds_checksum_is_an_error(resetting):
    assert resetting.reset_module.run(False, None) == 0
    resetting.deploy.versions["orders-service-production"] = []
    with pytest.raises(SystemExit):
        resetting.reset_module.lambda_aliases(resetting.shared.Reset(False))


def test_the_main_reset_archives_everything_but_this_reset_keeps_the_seeds_release_copy():
    main = importlib.util.spec_from_file_location("shared_reset_for_test", ROOT / "scripts" / "reset.py")
    shared = importlib.util.module_from_spec(main)
    main.loader.exec_module(shared)
    assert shared.after_baseline("1.12.0") and shared.after_baseline("1.12.2")
    assert "patch-clock/enabled" in shared.SWITCHES
    kept = load_reset().shared_reset()
    assert not kept.after_baseline("1.12.0") and kept.after_baseline("1.12.2") and kept.after_baseline("1.4.0") and not kept.after_baseline("1.3.0")
    assert "patch-clock/enabled" not in kept.SWITCHES and "ai-notes/enabled" in kept.SWITCHES


def test_reset_py_finds_this_demo():
    assert (ROOT / "scripts" / "demos" / "patch-clock".replace("-", "_") / "reset.py").exists()


# Status.

def test_status_warns_while_a_version_below_the_fix_is_live():
    live = readers.Live("1.12.0", "25", sha256="ab")
    (line,) = demo.warnings({"orders-service": live})
    assert line.startswith("WARNING: orders-service 1.12.0 is live and holds brotli 1.2.0, below 1.2.1.")
    assert "scripts/reset.py" in line
    assert len(demo.warnings({"orders-service": readers.Live("1.12.1", "26", sha256="ab")})) == 1


@pytest.mark.parametrize("version", ["1.12.2", "1.3.0", "1.4.0"])
def test_status_doesnt_warn_otherwise(version):
    assert demo.warnings({"orders-service": readers.Live(version, "25", sha256="ab")}) == []
    assert demo.warnings({"orders-service": "can't read it (AccessDenied)"}) == []


def test_status_on_a_stopped_host_still_shows_what_it_can(monkeypatch, capsys):
    monkeypatch.setattr(demo, "host_facts", lambda warm=False: (_ for _ in ()).throw(SystemExit("stopped")))
    monkeypatch.setattr(demo, "live_now", lambda: {"orders-service": readers.Live("1.12.0", "25", sha256="ab")})
    monkeypatch.setattr(demo.SWITCH, "value", lambda ssm=None: "on")
    assert demo.status() == 0
    out = capsys.readouterr().out
    assert "Switch patch-clock/enabled: on" in out and "Tools host: not answering" in out and "WARNING: orders-service 1.12.0" in out


def test_down_turns_the_switch_off_and_stops_nothing(monkeypatch, capsys):
    values = []
    monkeypatch.setattr(demo.SWITCH, "set", lambda value, ssm=None: values.append(value))
    assert demo.down() == 0
    assert values == ["off"] and "Nothing to stop" in capsys.readouterr().out


def test_up_says_ready_only_with_a_valid_database_and_the_functions_read(monkeypatch, capsys):
    monkeypatch.setattr(demo, "release_ci", lambda: "jenkins")
    monkeypatch.setattr(demo, "host_facts", lambda warm=False: {"built": "2026-11-02", "valid": warm, "error": None, "clocks": []})
    monkeypatch.setattr(demo, "live_now", lambda: {"orders-service": readers.Live("1.3.0", "25", sha256="ab")})
    assert demo.up() == 0 and capsys.readouterr().out.rstrip().endswith("Ready")
    monkeypatch.setattr(demo, "live_now", lambda: {"orders-service": "can't read it (ResourceNotFoundException)"})
    with pytest.raises(SystemExit):
        demo.up()


@pytest.mark.parametrize("ci, demo_name", [("azure-pipelines", "azure-devops"), ("gitlab-ci", "gitlab-port")])
def test_up_isnt_ready_while_another_demo_has_release(monkeypatch, capsys, ci, demo_name):
    monkeypatch.setattr(demo, "release_ci", lambda: ci)
    monkeypatch.setattr(demo, "host_facts", lambda warm=False: {"built": "2026-11-02", "valid": True, "error": None, "clocks": []})
    monkeypatch.setattr(demo, "live_now", lambda: {"orders-service": readers.Live("1.3.0", "25", sha256="ab")})
    with pytest.raises(SystemExit):
        demo.up()
    assert f"scripts/demo.sh down {demo_name} first" in capsys.readouterr().out


@pytest.mark.parametrize("parameters, ci", [([], "jenkins"), ([{"Value": "jenkins"}], "jenkins"), ([{"Value": "unset"}], "jenkins"),
                                            ([{"Value": "azure-pipelines"}], "azure-pipelines"), ([{"Value": "gitlab-ci"}], "gitlab-ci")])
def test_release_ci_reads_as_the_slack_handler_does(monkeypatch, parameters, ci):
    monkeypatch.setattr(demo, "ssm_client", lambda: SimpleNamespace(get_parameters=lambda Names: {"Parameters": parameters}))
    assert demo.release_ci() == ci


# The fallback.

def test_the_fallback_keeps_the_fix_one_line_and_brotli_at_its_fixed_version(tmp_path):
    for state in ("1.12.0", "1.12.1", "1.12.2"):
        (tmp_path / state).mkdir()
    source = tmp_path / "candidates.py"
    source.write_text('CHOSEN = "brotli"\n')
    chosen = use_fallback.switch("h11", states=tmp_path, source=source)
    assert chosen.bad == "0.14.0" and source.read_text() == 'CHOSEN = "h11"\n'
    assert (tmp_path / "1.12.0" / "requirements.txt").read_text() == (
        "# Compresses /api/orders for the store apps.\nbrotli==1.2.1\n# HTTP/1.1 for the carrier tracking client.\nh11==0.14.0\n")
    assert changed_lines((tmp_path / "1.12.1" / "requirements.txt").read_text(), (tmp_path / "1.12.2" / "requirements.txt").read_text()) == ["-h11==0.14.0", "+h11==0.16.0"]
    use_fallback.switch("brotli", states=tmp_path, source=source)
    assert (tmp_path / "1.12.2" / "requirements.txt").read_text() == "# Compresses /api/orders for the store apps.\nbrotli==1.2.1\n"
    assert source.read_text() == 'CHOSEN = "brotli"\n'


def test_the_candidates_are_the_specs():
    assert candidates.CHOSEN == "brotli"
    assert [(c.cve, c.package, c.bad, c.good) for c in candidates.CANDIDATES.values()] == [
        ("CVE-2026-111497", "brotli", "1.2.0", "1.2.1"), ("CVE-2025-43859", "h11", "0.14.0", "0.16.0"), ("CVE-2018-20060", "urllib3", "1.22", "2.8.0")]


# First day: the checks' own logic.

def test_check_one_wants_critical_and_a_non_zero_exit_then_no_match_and_zero(monkeypatch, capsys):
    import first_day

    chosen = candidates.chosen()
    reports = {chosen.bad: (json.loads(saved_bytes("grype-scan-brotli-1.2.0.json")), 2), chosen.good: ({"matches": []}, 0)}
    monkeypatch.setattr(first_day, "scanned", lambda package, version: reports[version])
    assert first_day.stands(chosen) is True
    reports[chosen.bad] = ({"matches": []}, 0)
    assert first_day.stands(chosen) is False
    reports[chosen.bad] = (json.loads(saved_bytes("grype-scan-brotli-1.2.0.json").replace(b'"Critical"', b'"High"')), 0)
    assert first_day.stands(chosen) is False
    assert "FAIL brotli 1.2.0" in capsys.readouterr().out


def test_check_one_moves_on_to_the_fallback_and_says_how(monkeypatch, capsys):
    import first_day
    import search

    monkeypatch.setattr(first_day, "stands", lambda candidate: candidate.package == "h11")
    monkeypatch.setattr(first_day.tools, "db_update", lambda: "updated")
    monkeypatch.setattr(first_day.tools, "db_status", lambda: {"built": "today"})
    monkeypatch.setattr(search, "read_facts", lambda cve: {"severity": "Critical", "known_exploited": False, "published": "24 Apr 2025", "packages": [], "names": set(), "ids": set()})
    assert first_day.check_cve() == 0
    assert "use_fallback.py h11" in capsys.readouterr().out


def test_all_three_failing_says_stop_and_ask(monkeypatch, capsys):
    import first_day

    monkeypatch.setattr(first_day, "stands", lambda candidate: False)
    monkeypatch.setattr(first_day.tools, "db_update", lambda: "updated")
    monkeypatch.setattr(first_day.tools, "db_status", lambda: {"built": "today"})
    assert first_day.check_cve() == 1
    assert "stop and ask" in capsys.readouterr().out


def test_the_rows_check_sees_a_swap_a_missing_sbom_and_a_denied_role(world, monkeypatch):
    import first_day

    monkeypatch.setattr(first_day.readers, "evidence_reader", lambda: world.store)

    class Tampered:
        def __init__(self, inner, answer):
            self.inner, self.answer = inner, answer

        def get(self, path):
            return self.answer if path.endswith("/sbom.cdx.json") else self.inner.get(path)

    monkeypatch.setattr(first_day, "Tampered", Tampered)
    assert first_day.check_rows() == 0


# What infra/ and the jobs add.

def test_the_role_holds_two_reads_and_nothing_else():
    main = (ROOT / "infra" / "demos" / "24-patch-clock" / "main.tf").read_text()
    assert 'name                 = "release-demo-sbom-reader"' in main
    assert 'key          = "release-demo/demos/24-patch-clock.tfstate"' in main
    assert 'Project = "release-demo", Demo = "24-patch-clock"' in main
    assert re.findall(r'actions\s*=\s*\[(.*?)\]', main) == ['"sts:AssumeRole"', '"lambda:GetAlias", "lambda:GetFunctionConfiguration"']
    assert 'values   = ["arn:aws:iam::${local.account_id}:role/release-demo-tools-host"]' in main


def test_the_host_may_assume_the_role_and_write_the_switch():
    host_tf = (ROOT / "infra" / "tools_host.tf").read_text()
    assert 'sid       = "AssumeSbomReader"' in host_tf and "role/release-demo-sbom-reader" in host_tf
    assert '"patch-clock/enabled"' in host_tf
    parameter = yaml.safe_load((ROOT / "infra" / "parameters.yaml").read_text())["patch-clock/enabled"]
    assert parameter["source"] == "script" and parameter["secure"] is False


def test_the_switch_is_an_ai_notes_switch_on_its_own_name():
    from switch import SWITCH

    assert SWITCH.name == "patch-clock/enabled" and SWITCH.label == "Patch clock"
    assert SWITCH.value(ssm=SimpleNamespace(get_parameter=lambda Name: (_ for _ in ()).throw(RuntimeError()))) is None
    assert not SWITCH.is_on(ssm=SimpleNamespace(get_parameter=lambda Name: {"Parameter": {"Value": "unset"}}))
    assert SWITCH.is_on(ssm=SimpleNamespace(get_parameter=lambda Name: {"Parameter": {"Value": "on"}}))


def test_no_dash_longer_than_a_hyphen_in_this_demo():
    forbidden = re.compile(f"[{chr(0x2013)}{chr(0x2014)}]")
    paths = [*HERE.rglob("*.py"), *HERE.glob("*.sh"), *(STATES / state / "handler.py" for state in ("1.12.0", "1.12.1", "1.12.2")),
             ROOT / "scripts" / "scan_line.py", ROOT / "jenkins" / "sbom-search.Jenkinsfile", ROOT / "jenkins" / "jobs" / "patch-clock-seed.Jenkinsfile",
             ROOT / "jenkins" / "shared-library" / "vars" / "patchClock.groovy", ROOT / "infra" / "demos" / "24-patch-clock" / "main.tf"]
    for path in paths:
        assert not forbidden.search(path.read_text()), path


def test_the_up_down_status_and_tf_scripts_can_run():
    import os

    for name in ("up.sh", "down.sh", "status.sh", "tf.sh"):
        assert os.access(HERE / name, os.X_OK), name
