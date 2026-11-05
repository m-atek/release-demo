"""What video 33 adds around the releases: the namespace, its rights, the bundle that carries it, and the windows the take films."""

import base64
import importlib.util
import io
import os
import stat
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import pytest
import yaml

from common import ROOT

HERE = Path(__file__).resolve().parents[1]
STATES = ROOT / "demo-states" / "forecast"


def documents(path):
    return [document for document in yaml.safe_load_all(Path(path).read_text()) if document]


def by_path(name, path):
    """A module that scripts/ has a namesake of, or that must load fresh."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def kube_host():
    return by_path("kube_host", ROOT / "scripts/demos/kubernetes/host.py")


# The namespace.

def test_the_namespace_is_held_to_pod_security_as_staging_and_production_are():
    namespace = next(document for document in documents(HERE / "namespace.yaml") if document["kind"] == "Namespace")
    assert namespace["metadata"]["name"] == "forecast"
    labels = namespace["metadata"]["labels"]
    assert labels["pod-security.kubernetes.io/enforce"] == "baseline"
    production = next(document for document in documents(ROOT / "scripts/demos/kubernetes/host/cluster.yaml")
                      if document["kind"] == "Namespace" and document["metadata"]["name"] == "production")
    assert labels["pod-security.kubernetes.io/enforce-version"] == production["metadata"]["labels"]["pod-security.kubernetes.io/enforce-version"]


def test_the_pipeline_gets_the_rights_it_has_elsewhere_in_this_namespace_only():
    binding = next(document for document in documents(HERE / "namespace.yaml") if document["kind"] == "RoleBinding")
    assert binding["metadata"]["namespace"] == "forecast"
    assert binding["roleRef"] == {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": "release-pipeline"}
    assert binding["subjects"] == [{"kind": "ServiceAccount", "name": "release-pipeline", "namespace": "jenkins"}]
    assert [document["kind"] for document in documents(HERE / "namespace.yaml")] == ["Namespace", "RoleBinding"]


def test_the_pipelines_role_can_deploy_there_and_cant_resize_a_pod_by_hand():
    role = next(document for document in documents(ROOT / "scripts/demos/kubernetes/host/cluster.yaml")
                if document["kind"] == "ClusterRole" and document["metadata"]["name"] == "release-pipeline")
    pods = next(rule for rule in role["rules"] if "pods" in rule["resources"])
    assert set(pods["verbs"]) == {"get", "list", "watch"}
    assert not any("pods/resize" in rule["resources"] or "nodes" in rule["resources"] for rule in role["rules"])


# The bundle.

@pytest.mark.parametrize("folders", [["scripts/demos/hotfix_room/on_host.py", "scripts/demos/hotfix_room/room.py",
                                      "scripts/demos/hotfix_room/namespace.yaml"],
                                     ["scripts/demos/hotfix_room/on_host.py", "scripts/demos/hotfix_room/room.py"]])
def test_up_and_down_fit_ssms_limit(folders):
    host = kube_host()
    assert len(host.bundle(folders=folders)) <= host.LIMIT


def test_every_script_names_what_exists(tmp_path):
    for name in ("up.sh", "down.sh", "status.sh"):
        text = (HERE / name).read_text()
        for word in text.replace("\\\n", " ").split():
            if word.startswith("scripts/demos/hotfix_room/") and not word.endswith(":"):
                assert (ROOT / word).exists(), word


def test_on_host_starts_from_the_bundle_alone(tmp_path):
    """demo.sh up, down and status: on_host.py reaches its own root check, past every import."""
    host = kube_host()
    data = base64.b64decode(host.bundle(folders=["scripts/demos/hotfix_room/on_host.py", "scripts/demos/hotfix_room/room.py",
                                                 "scripts/demos/hotfix_room/namespace.yaml"]))
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        archive.extractall(tmp_path, filter="data")
    reply = subprocess.run([sys.executable, "-P", "scripts/demos/hotfix_room/on_host.py", "status"], cwd=tmp_path, capture_output=True,
                           text=True, env={**os.environ, "PYTHONPATH": ""})
    assert "ModuleNotFoundError" not in reply.stderr
    assert "Run as root" in reply.stderr or os.geteuid() == 0


# What goes up and what comes down.

class Cluster:
    """on_host.py's calls to k3s.py's kubectl, recorded."""

    def __init__(self, namespace=True):
        self.calls, self.namespace = [], namespace

    def kubectl(self, *args, check=True, stdin=None):
        self.calls.append(args)
        if args[:2] == ("get", "namespace"):
            return "namespace/forecast" if self.namespace else ""
        return "applied"


@pytest.fixture
def on_host(monkeypatch):
    module = by_path("on_host_fresh", HERE / "on_host.py")
    cluster = Cluster()
    monkeypatch.setattr(module.k3s, "kubectl", cluster.kubectl)
    monkeypatch.setattr(module, "running", lambda: True)
    module.cluster = cluster
    return module


def test_up_applies_this_file_and_says_ready(on_host, capsys):
    on_host.up()
    assert on_host.cluster.calls == [("apply", "-f", str(HERE / "namespace.yaml"))]
    assert capsys.readouterr().out.rstrip().endswith("Ready")


def test_up_refuses_without_the_cluster(on_host, monkeypatch):
    monkeypatch.setattr(on_host, "running", lambda: False)
    with pytest.raises(SystemExit, match="scripts/demo.sh up kubernetes"):
        on_host.up()
    assert on_host.cluster.calls == []


def test_down_deletes_this_namespace_and_nothing_else(on_host):
    on_host.down()
    deletes = [call for call in on_host.cluster.calls if call[0] == "delete"]
    assert deletes == [("delete", "namespace", "forecast", "--wait=true", "--timeout=120s")]


def test_down_with_nothing_there_deletes_nothing(on_host, capsys):
    on_host.cluster.namespace = False
    on_host.down()
    assert not [call for call in on_host.cluster.calls if call[0] == "delete"]
    assert "nothing to remove" in capsys.readouterr().out


def test_down_asks_first():
    text = (HERE / "down.sh").read_text()
    assert 'read -r -p "Remove the namespace forecast' in text and "--yes" in text


# The windows.

def fake_bin(folder, name, text):
    path = folder / name
    path.write_text(f"#!/bin/sh\n{text}\n")
    path.chmod(0o755)


def test_the_scripts_are_executable_and_parse():
    for name in ("up.sh", "down.sh", "status.sh", "pods.sh", "reveal.sh"):
        assert (HERE / name).stat().st_mode & stat.S_IXUSR, name
        subprocess.run(["bash", "-n", str(HERE / name)], check=True)


def test_the_top_window_is_real_kubectl_redrawn_in_place(tmp_path):
    fake_bin(tmp_path, "kubectl", f'echo "$@" > {tmp_path}/asked\nprintf "POD  VERSION  STATUS\\ncatalog-new  1.12.1  Pending\\n"')
    fake_bin(tmp_path, "clear", "true")
    process = subprocess.Popen(["bash", str(HERE / "pods.sh")], stdout=subprocess.PIPE, text=True,
                               env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "KUBECONFIG": "/dev/null"})
    time.sleep(0.5)
    process.terminate()
    output = process.communicate(timeout=5)[0]
    asked = (tmp_path / "asked").read_text()
    assert asked.strip() == ("-n staging get pods -o custom-columns=POD:.metadata.name,"
                             "VERSION:.metadata.labels.app\\.kubernetes\\.io/version,STATUS:.status.phase "
                             "--sort-by=.metadata.creationTimestamp")
    assert "\033[HPOD  VERSION  STATUS\033[K\ncatalog-new  1.12.1  Pending\033[K\033[J" in output


@pytest.fixture
def stage_repository(tmp_path):
    """A clone with forecast staged twice and a copy of reveal.sh where it expects to live."""
    origin, work = tmp_path / "origin.git", tmp_path / "work"
    run = lambda *args, cwd=work: subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)
    subprocess.run(["git", "init", "--quiet", "--bare", str(origin)], check=True)
    subprocess.run(["git", "clone", "--quiet", str(origin), str(work)], check=True, capture_output=True)
    run("config", "user.email", "test@example.com")
    run("config", "user.name", "Test")
    script = work / "scripts/demos/hotfix_room/reveal.sh"
    script.parent.mkdir(parents=True)
    script.write_text((HERE / "reveal.sh").read_text())
    script.chmod(0o755)
    target = work / "services/forecast"
    target.parent.mkdir()
    for version in ("2.4.0", "2.4.1"):
        if target.exists():
            subprocess.run(["rm", "-rf", str(target)], check=True)
        subprocess.run(["cp", "-R", str(STATES / version), str(target)], check=True)
        run("add", "--all")
        run("commit", "--quiet", "-m", f"demo: stage forecast {version} [skip ci]")
    run("push", "--quiet", "origin", "HEAD")
    branch = subprocess.run(["git", "branch", "--show-current"], cwd=work, capture_output=True, text=True).stdout.strip()
    run("branch", "--set-upstream-to", f"origin/{branch}")
    return work


def reveal(work, enters, tmp_path):
    fake_bin(tmp_path, "clear", "true")
    return subprocess.run(["bash", str(work / "scripts/demos/hotfix_room/reveal.sh")], input="\n" * enters, text=True,
                          capture_output=True, env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"})


def test_reveal_shows_the_four_lines_as_the_script_outlines_them(stage_repository, tmp_path):
    reply = reveal(stage_repository, 2, tmp_path)
    assert reply.returncode == 0, reply.stderr
    shown = reply.stdout.splitlines()
    assert shown[0] == "$ git diff $BEFORE -U0 -- $VALUES | memory"
    assert shown[1:5] == ["-  requests: { cpu: 10m, memory: 256Mi }", "-  limits: { cpu: 250m, memory: 256Mi }",
                          "+  requests: { cpu: 10m, memory: 48Mi }", "+  limits: { cpu: 250m, memory: 96Mi }"]
    assert "demo-states" not in reply.stdout
    assert max(len(line) for line in shown) <= 44


def test_reveal_on_the_wrong_version_says_so_before_it_clears_the_window(stage_repository, tmp_path):
    run = lambda *args: subprocess.run(["git", *args], cwd=stage_repository, check=True, capture_output=True)
    run("reset", "--quiet", "--hard", "HEAD~1")
    run("push", "--quiet", "--force", "origin", "HEAD")
    reply = reveal(stage_repository, 2, tmp_path)
    assert reply.returncode == 1 and reply.stdout == ""
    assert "main has forecast 2.4.0, not 2.4.1" in reply.stderr


# Shared files.

def test_the_idle_stop_counts_the_first_day_check_as_use(monkeypatch):
    idle = by_path("idle_stop_fresh", ROOT / "tools-host/idle_stop.py")
    assert "release-demo-hotfix-rehearse" in idle.LONG_RUNS
    running = {"release-demo-hotfix-rehearse"}
    monkeypatch.setattr(idle.subprocess, "run", lambda command, **_: idle.subprocess.CompletedProcess(command, 0 if command[-1] in running else 3))
    assert idle.long_run_busy() is True
    running.clear()
    assert idle.long_run_busy() is False


def test_the_rehearsal_hands_over_under_the_unit_the_idle_stop_counts():
    assert '"hotfix-rehearse"' in (HERE / "rehearse.py").read_text()


def test_the_readme_has_this_demo():
    readme = (ROOT / "README.md").read_text()
    assert "## Demo on a call: the hotfix the cluster had no room for (video 33)" in readme


def test_no_em_or_en_dash_in_anything_this_demo_adds():
    folders = [HERE, ROOT / "demo-states/catalog/1.12.0", ROOT / "demo-states/catalog/1.12.1", STATES, ROOT / "services/forecast"]
    paths = [path for folder in folders for path in folder.rglob("*") if path.is_file() and path.suffix not in (".pyc",) and path.name != ".coverage"]
    assert paths
    for path in paths:
        text = path.read_text()
        assert chr(0x2014) not in text and chr(0x2013) not in text, path
