"""reset.py database-blip against fakes: where it refuses, what it points the main reset at,
what a dry run leaves alone, and that a second run finds nothing to do."""

import contextlib
from argparse import Namespace

import pytest

import blip_check
import host
import kube
import reset


class Cluster:
    """Both namespaces as a take leaves them: staging on 1.10.0, production on 1.9.1."""

    def __init__(self):
        self.staging, self.production, self.block = "1.10.0", "1.9.1", True
        self.database = {"ready": True, "restartCount": 0}
        self.restarts = {"staging": 0, "production": 0}
        self.deleted, self.renewed = [], []

    def history(self, kubeconfig, release, namespace):
        return [{"revision": 1, "status": "deployed", "app_version": getattr(self, namespace)}]

    def put_on_baseline(self):
        self.staging = self.production = reset.BASELINE
        self.restarts = {"staging": 0, "production": 0}


class Api:
    def __init__(self, cluster):
        self.cluster = cluster

    def get(self, path):
        if path.endswith(f"/pods/{reset.POSTGRES}"):
            return {"status": {"containerStatuses": [self.cluster.database]}}
        assert path.endswith(f"/networkpolicies/{blip_check.POLICY}"), path
        return {"metadata": {"name": blip_check.POLICY}} if self.cluster.block else None

    def delete(self, path):
        self.cluster.deleted.append(path)
        self.cluster.block = False
        return True

    def pods(self, namespace, release):
        version = getattr(self.cluster, namespace)
        return [{"metadata": {"name": f"catalog-{i}", "labels": {"app.kubernetes.io/version": version}},
                 "status": {"containerStatuses": [{"ready": True, "restartCount": self.cluster.restarts[namespace]}]}}
                for i in range(3)]


@pytest.fixture
def world(monkeypatch, capsys):
    cluster = Cluster()
    monkeypatch.setattr(kube, "credentials", lambda quiet=False: contextlib.nullcontext("kubeconfig"))
    monkeypatch.setattr(kube, "Api", lambda kubeconfig: Api(cluster))
    monkeypatch.setattr(kube, "history", cluster.history)
    monkeypatch.setattr(reset, "put_on_baseline", cluster.put_on_baseline)
    monkeypatch.setattr(reset, "renew", lambda kubeconfig, namespace, release: cluster.renewed.append(namespace))
    main = {"version": "1.9.1"}
    monkeypatch.setattr(host, "main_version", lambda path: main["version"])
    monkeypatch.setattr(host, "stage", lambda service, version: main.update(version=version))
    monkeypatch.setattr(reset, "take_issue", lambda: "ORD-110")
    monkeypatch.setattr(blip_check, "LOG", None)
    marks = []
    monkeypatch.setattr(blip_check, "open_log", lambda path: type("Log", (), {"write": staticmethod(marks.append), "close": lambda self=None: None})())
    shared = reset.shared_reset()
    seen = {"steps": [], "shared": shared}
    for name in ("cancel_changes", "github_takes", "jfrog_takes", "jira_versions"):
        monkeypatch.setattr(shared, name, lambda r, name=name: seen["steps"].append((name, shared.SERVICE, shared.BASELINE)))
    monkeypatch.setattr(shared, "stop_runs", lambda r, jobs: seen["steps"].append(("stop_runs",)))
    monkeypatch.setattr(shared, "release_jobs", lambda: [])
    monkeypatch.setattr(shared, "warm_slack_handler", lambda r, url: seen["steps"].append(("warm",)))
    monkeypatch.setattr(reset, "shared_reset", lambda: shared)
    return Namespace(cluster=cluster, main=main, marks=marks, seen=seen, shared=shared, capsys=capsys)


def args(stage="1.10.0", dry_run=False):
    return Namespace(stage=stage, dry_run=dry_run, slack_handler_url=None)


def test_it_puts_everything_back_and_ends_ready_for_a_take(world):
    assert reset.run(args()) == 0
    assert world.cluster.staging == world.cluster.production == "1.9.1"
    assert world.cluster.block is False
    assert world.cluster.deleted == [f"/apis/networking.k8s.io/v1/namespaces/staging/networkpolicies/{blip_check.POLICY}"]
    assert world.main["version"] == "1.10.0"
    assert world.marks and world.marks[0].startswith("== reset ")
    assert world.capsys.readouterr().out.rstrip().endswith("Ready for a take.")


def test_the_main_resets_steps_point_at_catalog_after_1_9_1(world):
    reset.run(args())
    assert [step[0] for step in world.seen["steps"]] == ["stop_runs", "cancel_changes", "github_takes", "jfrog_takes",
                                                         "jira_versions", "warm"]
    assert ("github_takes", "catalog", "1.9.1") in world.seen["steps"]
    assert world.shared.DEMO_ISSUES == ["ORD-110"]
    assert world.shared.after_baseline("1.10.0") and world.shared.after_baseline("1.10.1")
    assert not world.shared.after_baseline("1.9.1")


def test_stage_1_10_1_stages_take_2s_version(world):
    assert reset.run(args(stage="1.10.1")) == 0
    assert world.main["version"] == "1.10.1"


def test_a_second_run_finds_nothing_to_do(world):
    reset.run(args())
    world.capsys.readouterr()
    world.main["version"] = "1.10.0"
    assert reset.run(args()) == 0
    out = world.capsys.readouterr().out
    assert "Reset in" in out and "1 action(s)" in out  # only the log's mark: it marks each take's start
    assert "main has catalog 1.10.0" in out


def test_a_dry_run_changes_nothing(world):
    assert reset.run(args(dry_run=True)) == 0
    assert world.cluster.staging == "1.10.0" and world.cluster.block is True and world.cluster.deleted == []
    assert world.main["version"] == "1.9.1" and world.marks == []
    out = world.capsys.readouterr().out
    assert "would delete block-database" in out and "would put staging on 1.9.1" in out
    assert "would stage catalog 1.10.0 on main" in out


@pytest.mark.parametrize("status,why", [({"ready": False}, "isn't ready"), ({"ready": True, "restartCount": 1}, "reads RESTARTS 1")])
def test_it_refuses_unless_the_database_is_ready_and_at_zero_restarts(world, status, why):
    world.cluster.database = status
    assert reset.run(args()) == 1
    out = world.capsys.readouterr().out
    assert why in out and "scripts/demo.sh up database-blip" in out
    assert world.seen["steps"] == [] and world.cluster.staging == "1.10.0"


def test_used_pods_on_the_right_version_get_new_ones(world):
    world.cluster.staging = "1.9.1"
    world.cluster.restarts["staging"] = 1
    assert reset.run(args()) == 0
    assert world.cluster.renewed == ["staging"]


def test_every_stage_it_offers_exists():
    for version in reset.STAGES:
        assert (reset.ROOT / "demo-states" / reset.SERVICE / version / "database").is_dir()


def test_nothing_it_does_can_touch_slacks_messages():
    """A reset with --stage 1.10.1 leaves the failed run's message alone."""
    text = (reset.HERE / "reset.py").read_text()
    assert "chat." not in text and "notify" not in text
