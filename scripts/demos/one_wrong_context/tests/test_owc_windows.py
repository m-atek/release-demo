"""The two windows and the kubeconfig the take films: real bash with a fake kubectl, a fake session."""

import json
import os
import stat
import subprocess
import time
from pathlib import Path

import pytest

import contexts
import window

HERE = Path(__file__).resolve().parents[1]


def fake_bin(folder, name, body):
    path = folder / name
    path.write_text(f"#!/usr/bin/env bash\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def one_frame(tmp_path, kubectl_body, seconds=1.2):
    """pods.sh for a moment, in real bash, with kubectl faked. What it printed, escape codes and all."""
    fake_bin(tmp_path, "kubectl", kubectl_body)
    fake_bin(tmp_path, "clear", "true")
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "KUBECONFIG": str(tmp_path / "kubeconfig")}
    process = subprocess.Popen(["bash", str(HERE / "pods.sh")], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(seconds)
    process.terminate()
    out, _ = process.communicate(timeout=5)
    return out


def test_pods_sh_shows_real_kubectl_cut_to_three_columns(tmp_path):
    out = one_frame(tmp_path, f'''printf '%s ' "$@" > {tmp_path}/asked
printf 'NAME                       READY   STATUS        RESTARTS   AGE\\ncatalog-6c9f7d5b8-abcde    1/1     Running       0          2m\\ncatalog-6c9f7d5b8-fghij    1/1     Terminating   0          2m\\n' ''')
    assert (tmp_path / "asked").read_text().strip() == "-n production get pods"
    assert "Pods in production" in out
    assert "catalog-6c9f7d5b8-abcde" in out and "Running" in out and "Terminating" in out
    assert "RESTARTS" not in out and "AGE" not in out


def test_pods_sh_shows_kubectls_own_words_when_there_are_none(tmp_path):
    out = one_frame(tmp_path, 'echo "No resources found in production namespace." >&2')
    assert "No resources found in production namespace." in out


def test_pods_sh_asks_for_every_pod_with_no_selector():
    text = (HERE / "pods.sh").read_text()
    assert "-l " not in text and "--selector" not in text


def test_pods_sh_shows_the_error_and_keeps_going_when_the_tunnel_drops(tmp_path):
    out = one_frame(tmp_path, 'echo "Unable to connect to the server" >&2; exit 1', seconds=2.2)
    assert out.count("Unable to connect to the server") >= 2


def test_its_rows_fit_the_window(tmp_path):
    out = one_frame(tmp_path, '''printf 'NAME READY STATUS RESTARTS AGE\\ncatalog-6c9f7d5b8-abcde 1/1 Terminating 0 2m\\n' ''')
    row = next(line for line in out.splitlines() if "catalog-" in line)
    assert len(row.split("\033")[0]) <= 56


class Session:
    """What subprocess.Popen returns for aws ssm start-session: session noise, then the host's lines."""

    def __init__(self, lines):
        self.stdout = iter(lines)
        self.pid = 999999
        self.terminated = False

    def terminate(self):
        self.terminated = True


class FakeHost:
    id = "i-0000000000"


@pytest.fixture
def env(monkeypatch):
    import common

    monkeypatch.setattr(common, "aws_cli_env", lambda: {"AWS_DEFAULT_REGION": "eu-west-1"})


def test_the_window_relays_the_hosts_lines_and_hides_the_sessions_own(env, capsys):
    session = Session(["\n", "Starting session with SessionId: x\r\n", "TIME      ANSWERED  FAILED\r\n", "14:02:11       120       0\r\n",
                       "Exiting session with sessionId: x.\r\n"])
    asked = {}

    def popen(command, **options):
        asked["command"], asked["options"] = command, options
        return session

    assert window.stream(FakeHost(), popen=popen) is True
    out = capsys.readouterr().out
    assert "TIME      ANSWERED  FAILED\n14:02:11       120       0\n" in out
    assert "session" not in out.lower()
    assert session.terminated


def test_the_session_runs_the_caller_from_the_hosts_checkout(env):
    asked = {}

    def popen(command, **options):
        asked["command"] = command
        return Session([])

    assert window.stream(FakeHost(), popen=popen) is False
    parameters = json.loads(asked["command"][asked["command"].index("--parameters") + 1])
    assert parameters == {"command": ["/opt/release-demo-venv/bin/python /opt/release-demo/scripts/demos/one_wrong_context/calls.py"]}
    assert "AWS-StartInteractiveCommand" in asked["command"]
    assert (Path(window.__file__).parent / "calls.py").exists()


def config():
    return {"apiVersion": "v1", "kind": "Config", "clusters": [{"name": "release-demo", "cluster": {"server": "https://127.0.0.1:16443"}}],
            "users": [{"name": "presenter", "user": {"client-key-data": "not-a-key"}}],
            "contexts": [{"name": "release-demo", "context": {"cluster": "release-demo", "user": "presenter", "namespace": "production"}}],
            "current-context": "release-demo"}


def test_two_contexts_one_cluster_one_certificate_on_staging_first(tmp_path):
    source, copy = tmp_path / "config", tmp_path / "copy"
    source.write_text(json.dumps(config()))
    assert contexts.write(source, copy) == copy
    written = json.loads(copy.read_text())
    assert {c["name"]: c["context"] for c in written["contexts"]} == {
        "staging": {"cluster": "release-demo", "user": "presenter", "namespace": "staging"},
        "production": {"cluster": "release-demo", "user": "presenter", "namespace": "production"}}
    assert written["current-context"] == "staging"
    assert written["users"] == config()["users"] and written["clusters"] == config()["clusters"]


def test_the_copy_is_private_even_if_it_was_not(tmp_path):
    source, copy = tmp_path / "config", tmp_path / "copy"
    source.write_text(json.dumps(config()))
    copy.write_text("old")
    copy.chmod(0o644)
    contexts.write(source, copy)
    assert stat.S_IMODE(copy.stat().st_mode) == 0o600


def test_a_later_write_starts_on_staging_again(tmp_path):
    source, copy = tmp_path / "config", tmp_path / "copy"
    source.write_text(json.dumps(config()))
    contexts.write(source, copy)
    moved = json.loads(copy.read_text())
    moved["current-context"] = "production"
    copy.write_text(json.dumps(moved))
    contexts.write(source, copy)
    assert json.loads(copy.read_text())["current-context"] == "staging"


def test_without_connect_py_it_says_what_to_run(tmp_path):
    with pytest.raises(SystemExit) as stopped:
        contexts.write(tmp_path / "none", tmp_path / "copy")
    assert "connect.py" in str(stopped.value)
    assert not (tmp_path / "copy").exists()


def test_it_prints_no_key(tmp_path, capsys, monkeypatch):
    source, copy = tmp_path / "config", tmp_path / "copy"
    source.write_text(json.dumps(config()))
    monkeypatch.setattr(contexts, "SOURCE", source)
    monkeypatch.setattr(contexts, "COPY", copy)
    monkeypatch.setattr(contexts.write, "__defaults__", (source, copy))
    assert contexts.main() == 0
    assert "not-a-key" not in capsys.readouterr().out
