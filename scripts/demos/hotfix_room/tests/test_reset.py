"""reset.py hotfix-room against fakes: where it refuses, in what order the cluster moves, what a dry run leaves alone, and a second run."""

import contextlib
import subprocess
import urllib.error
from argparse import Namespace

import pytest

import host
import kube
import reset
import room
from fake_node import MIB


class Cluster:
    """What a take leaves, by default: forecast small, catalog on the hotfix."""

    def __init__(self):
        self.versions = {"staging": "1.12.1", "production": "1.12.1", "forecast": "2.4.1"}
        self.moves, self.postgres, self.bound = [], False, True

    def history(self, kubeconfig, release, namespace):
        return [{"revision": 1, "status": "deployed", "app_version": self.versions[namespace]}] if self.versions[namespace] else []

    def put_on(self, name, version):
        self.moves.append((name, version))
        for namespace in (["staging", "production"] if name == "catalog" else ["forecast"]):
            self.versions[namespace] = version

    def make_room(self, kubeconfig):
        self.moves.append(("uninstall", "forecast"))
        self.versions["forecast"] = None


class Api:
    def __init__(self, cluster):
        self.cluster = cluster

    def get(self, path):
        assert path.endswith(f"/pods/{reset.POSTGRES}"), path
        return {"metadata": {}} if self.cluster.postgres else None

    def pods(self, namespace, release):
        if namespace == "forecast" and not self.cluster.bound:
            raise urllib.error.HTTPError("url", 403, "Forbidden", {}, None)
        version = self.cluster.versions[namespace]
        if not version:
            return []
        return [{"metadata": {"name": f"{release}-{i}", "labels": {"app.kubernetes.io/version": version}},
                 "status": {"containerStatuses": [{"ready": True}]}} for i in range(3)]


def reading(free_mib=22, ask_mib=48):
    return room.Reading(allocatable=1180 * MIB, reserved=(1180 - free_mib) * MIB, used=256 * MIB, forecast_reserved=768 * MIB,
                        forecast_used=51 * MIB, ask=ask_mib * MIB)


@pytest.fixture
def world(monkeypatch, capsys):
    cluster = Cluster()
    monkeypatch.setattr(kube, "credentials", lambda quiet=False: contextlib.nullcontext("kubeconfig"))
    monkeypatch.setattr(kube, "Api", lambda kubeconfig: Api(cluster))
    monkeypatch.setattr(kube, "history", cluster.history)
    monkeypatch.setattr(reset, "put_on", cluster.put_on)
    monkeypatch.setattr(reset, "make_room", cluster.make_room)
    main = {"services/catalog": "1.9.1", "services/forecast": "2.4.0"}
    staged = []
    monkeypatch.setattr(host, "main_version", lambda path: main[path])

    def stage(name, version):
        staged.append((name, version))
        main[f"services/{name}"] = version

    monkeypatch.setattr(host, "stage", stage)
    monkeypatch.setattr(reset, "take_issues", lambda dry_run: {"catalog": "ORD-120", "forecast": "ORD-121"})
    node = {"reading": reading()}
    monkeypatch.setattr(reset.room, "read", lambda get: node["reading"])
    steps = []
    modules = {}

    def fake_shared(name):
        module = modules.get(name)
        if module is None:
            module = real_shared(name)
            for step in ("cancel_changes", "github_takes", "jfrog_takes", "jira_versions"):
                monkeypatch.setattr(module, step, lambda r, step=step, module=module: steps.append((step, module.SERVICE, module.BASELINE)))
            monkeypatch.setattr(module, "stop_runs", lambda r, jobs: steps.append(("stop_runs",)))
            monkeypatch.setattr(module, "release_jobs", lambda: [])
            monkeypatch.setattr(module, "warm_slack_handler", lambda r, url: steps.append(("warm",)))
            modules[name] = module
        return module

    real_shared = reset.shared_reset
    monkeypatch.setattr(reset, "shared_reset", fake_shared)
    return Namespace(cluster=cluster, main=main, staged=staged, steps=steps, node=node, modules=modules, capsys=capsys)


def args(dry_run=False):
    return Namespace(dry_run=dry_run, slack_handler_url=None)


def test_it_puts_everything_back_and_ends_ready_for_a_take(world):
    assert reset.run(args()) == 0
    assert world.cluster.versions == {"staging": "1.12.0", "production": "1.12.0", "forecast": "2.4.0"}
    assert world.main == {"services/catalog": "1.12.1", "services/forecast": "2.4.1"}
    assert world.capsys.readouterr().out.rstrip().endswith("Ready for a take.")


def test_catalog_moves_first_and_forecast_fills_the_node_last(world):
    reset.run(args())
    assert world.cluster.moves == [("catalog", "1.12.0"), ("forecast", "2.4.0")]


def test_both_fixes_are_staged_so_all_three_releases_build_from_one_commit(world):
    reset.run(args())
    assert world.staged == [("catalog", "1.12.1"), ("forecast", "2.4.1")]


def test_the_main_resets_steps_run_for_each_service_after_its_own_baseline(world):
    reset.run(args())
    assert world.steps[0] == ("stop_runs",)
    assert world.steps[-1] == ("warm",)
    for step in ("cancel_changes", "github_takes", "jfrog_takes", "jira_versions"):
        assert (step, "catalog", "1.12.0") in world.steps
        assert (step, "forecast", "2.4.0") in world.steps
    assert world.modules["catalog"].DEMO_ISSUES == ["ORD-120"]
    assert world.modules["forecast"].DEMO_ISSUES == ["ORD-121"]
    assert world.modules["catalog"].after_baseline("1.12.1") and not world.modules["catalog"].after_baseline("1.12.0")
    assert world.modules["forecast"].after_baseline("2.4.1") and not world.modules["forecast"].after_baseline("2.4.0")


def test_after_a_stopped_hotfix_nothing_on_the_cluster_moves(world):
    """Take A leaves catalog on 1.12.0, by Helm's rollback, and forecast on 2.4.0."""
    world.cluster.versions = {"staging": "1.12.0", "production": "1.12.0", "forecast": "2.4.0"}
    assert reset.run(args()) == 0
    assert world.cluster.moves == []


def test_a_second_run_finds_nothing_to_do(world):
    reset.run(args())
    world.capsys.readouterr()
    assert reset.run(args()) == 0
    out = world.capsys.readouterr().out
    assert "Reset in" in out and "0 action(s)" in out
    assert "main has catalog 1.12.1" in out and "main has forecast 2.4.1" in out


def test_when_catalog_must_move_and_forecast_holds_the_room_forecast_goes_first(world):
    world.cluster.versions = {"staging": "1.9.1", "production": "1.9.1", "forecast": "2.4.0"}
    assert reset.run(args()) == 0
    assert world.cluster.moves == [("uninstall", "forecast"), ("catalog", "1.12.0"), ("forecast", "2.4.0")]


def test_first_time_nothing_of_forecast_is_there(world):
    world.cluster.versions = {"staging": "1.9.1", "production": "1.9.1", "forecast": None}
    assert reset.run(args()) == 0
    assert world.cluster.moves == [("catalog", "1.12.0"), ("forecast", "2.4.0")]


def test_a_dry_run_changes_nothing(world):
    assert reset.run(args(dry_run=True)) == 0
    assert world.cluster.moves == [] and world.staged == []
    out = world.capsys.readouterr().out
    assert "would put staging and production on catalog 1.12.0, through Helm" in out
    assert "would put forecast on 2.4.0, through Helm, last" in out
    assert "would stage catalog 1.12.1 on main" in out and "would stage forecast 2.4.1 on main" in out


def test_it_refuses_while_videos_25s_database_is_up(world):
    world.cluster.postgres = True
    assert reset.run(args()) == 1
    out = world.capsys.readouterr().out
    assert "scripts/demo.sh down database-blip" in out
    assert world.steps == [] and world.cluster.moves == []


def test_it_refuses_before_demo_sh_up(world):
    world.cluster.bound = False
    assert reset.run(args()) == 1
    assert "scripts/demo.sh up hotfix-room" in world.capsys.readouterr().out
    assert world.steps == []


def test_a_node_with_room_to_spare_isnt_ready(world):
    """Something missing from the node: the hotfix would simply start, and that isn't a take."""
    world.node["reading"] = reading(free_mib=118)
    assert reset.run(args()) == 1
    out = world.capsys.readouterr().out
    assert "would simply start" in out and "Ready for a take" not in out


def test_an_overfull_node_isnt_ready(world):
    world.node["reading"] = reading(free_mib=-74)
    assert reset.run(args()) == 1
    assert "more reserved than the node has" in world.capsys.readouterr().out


def test_what_the_node_reads_is_printed_for_the_day(world):
    reset.run(args())
    out = world.capsys.readouterr().out
    assert "Reserved" in out and "98%" in out and "Free to reserve" in out


def test_a_node_it_may_not_read_doesnt_stop_the_reset_job(world, monkeypatch):
    """The reset job runs as the agent's user, who can't read root's kubeconfig."""
    def refused(get):
        raise RuntimeError("permission denied")

    monkeypatch.setattr(reset.room, "read", refused)
    assert reset.run(args()) == 0
    assert "not read: permission denied" in world.capsys.readouterr().out


def test_the_issues_are_made_by_the_reset_if_they_arent_there(monkeypatch):
    """The allow rule covers the reset and not setup.py, so nothing about a take waits on setup.py for its issues."""
    import jira as jira_module
    import setup

    made = []

    class Jira:
        def search(self, jql, fields="summary"):
            return []

        def create(self, kind, summary, description, labels=()):
            made.append(summary)
            return f"ORD-{120 + len(made)}"

    monkeypatch.setattr(jira_module, "Jira", Jira)
    assert reset.take_issues(False) == {"catalog": "ORD-121", "forecast": "ORD-122"}
    assert made == [setup.HOTFIX_SUMMARY, setup.ROOM_SUMMARY]


def test_a_dry_run_makes_no_issue_and_reopens_none(world, monkeypatch):
    monkeypatch.setattr(reset, "take_issues", lambda dry_run: {"catalog": "ORD-?", "forecast": "ORD-?"})
    assert reset.run(args(dry_run=True)) == 0
    assert not [step for step in world.steps if step[0] == "jira_versions"]


def test_the_cluster_step_uses_videos_8s_reset_with_the_service_and_version(monkeypatch):
    ran = []
    monkeypatch.undo()
    monkeypatch.setattr(reset.subprocess, "run", lambda command, **kwargs: ran.append(command) or subprocess.CompletedProcess(command, 0))
    monkeypatch.setattr(reset, "git", lambda *args, check=True: subprocess.CompletedProcess(args, 0, stdout="abc123\n"))
    reset.put_on("forecast", "2.4.0")
    assert ran[0][1:] == [reset.RESET, "--here", "--service", "forecast", "--baseline", "2.4.0", "--commit", "abc123"]


def test_make_room_is_helms_uninstall_in_this_namespace_only(monkeypatch):
    asked = []
    monkeypatch.setattr(kube, "helm", lambda *args, **kwargs: asked.append(args) or subprocess.CompletedProcess(args, 0, stdout="", stderr=""))
    reset.make_room("kubeconfig")
    assert asked == [("kubeconfig", "uninstall", "forecast", "--namespace", "forecast", "--wait", "--timeout", "2m")]


def test_videos_8s_reset_takes_another_service_and_passes_it_to_its_children(monkeypatch, capsys):
    """Only what video 33 adds to it: catalog's own use reads as before."""
    source = (reset.KUBERNETES / "reset.py").read_text()
    assert '"--service", SERVICE' in source
    assert 'parser.add_argument("--service", default=SERVICE' in source
    assert 'SERVICE = "catalog"' in source
