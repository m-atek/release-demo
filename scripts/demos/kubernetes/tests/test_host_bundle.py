"""What host.py sends through SSM: under the limit, the same every time, no demo state
unless the command names it, and enough for every command to start on the host."""

import base64
import gzip
import io
import os
import subprocess
import sys
import tarfile
import time

import pytest
import yaml

import host
import rehearse
import reset


def members(text):
    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(text))) as archive:
        return {member.name: member for member in archive.getmembers()}


def unpack(text, folder):
    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(text))) as archive:
        archive.extractall(folder, filter="data")
    return folder


# Every command that goes through host.py today, as it calls it.
USES = {"up, down, status, presenter": {}, "reset.py": {"states": [reset.BASELINE]},
        "rehearse.py": {"states": rehearse.STATES}}


@pytest.mark.parametrize("use", USES)
def test_every_command_fits_ssms_limit(use):
    size = len(host.bundle(**USES[use]))
    assert size <= host.LIMIT, f"{use}: {size} bytes as base64 against {host.LIMIT}"


def test_no_demo_state_goes_by_default():
    assert not [name for name in members(host.bundle()) if name.startswith("demo-states/")]


def test_only_the_named_state_goes():
    names = members(host.bundle(states=["1.8.2"]))
    assert "demo-states/catalog/1.8.2/app.py" in names
    assert not [name for name in names if name.startswith(("demo-states/catalog/1.9", "demo-states/orders"))]


def test_a_command_names_what_else_it_needs():
    names = members(host.bundle(folders=["scripts/build.py"]))
    assert "scripts/build.py" in names


def test_a_state_that_doesnt_exist_stops_before_anything_is_sent(monkeypatch):
    monkeypatch.setattr(host, "aws_session", lambda: pytest.fail("no AWS call without the files"))
    with pytest.raises(SystemExit):
        host.run("true", states=["0.0.0"])


def test_tests_and_caches_never_go():
    assert not [name for name in members(host.bundle(states=["1.9.1"]))
                if "/tests/" in name or "__pycache__" in name or name.endswith(".pyc")]


def test_services_yaml_is_cut_to_catalog_and_keeps_its_whole_entry(tmp_path):
    unpack(host.bundle(), tmp_path)
    sent = yaml.safe_load((tmp_path / "services.yaml").read_text())
    whole = yaml.safe_load((host.ROOT / "services.yaml").read_text())
    assert list(sent) == ["catalog"] and sent["catalog"] == whole["catalog"]


def test_same_files_same_text_at_another_time(monkeypatch):
    monkeypatch.setattr(time, "time", lambda: 1_000_000)
    first = host.bundle(states=["1.8.2"])
    monkeypatch.setattr(time, "time", lambda: 2_000_000)
    assert host.bundle(states=["1.8.2"]) == first


def tree(root, stamp):
    """A small checkout: a services file and a folder, written at a given time."""
    folder = root / "scripts" / "demos" / "kubernetes" / "host"
    folder.mkdir(parents=True)
    (root / "services.yaml").write_text("catalog: {path: services/catalog}\nother: {path: services/other}\n")
    (folder / "helper").write_text("#!/bin/sh\n")
    (folder / "helper").chmod(0o775)
    (folder / "notes.py").write_text("x = 1\n")
    (folder / "notes.py").chmod(0o600)
    for path in (root / "services.yaml", folder / "helper", folder / "notes.py"):
        os.utime(path, (stamp, stamp))


def test_same_files_same_text_in_another_checkout(monkeypatch, tmp_path):
    """No file's time, owner or umask gets in, and a helper stays executable."""
    monkeypatch.setattr(host, "FILES", ["scripts/demos/kubernetes"])
    texts = []
    for name, stamp in (("one", 1_000_000), ("two", 2_000_000)):
        tree(tmp_path / name, stamp)
        monkeypatch.setattr(host, "ROOT", tmp_path / name)
        texts.append(host.bundle())
    assert texts[0] == texts[1]
    sent = members(texts[0])
    assert sent["scripts/demos/kubernetes/host/helper"].mode == 0o755
    assert sent["scripts/demos/kubernetes/host/notes.py"].mode == 0o644
    assert {member.mtime for member in sent.values()} == {0}
    assert {(member.uid, member.gid) for member in sent.values()} == {(0, 0)}


def test_gzip_stamps_no_time():
    assert gzip.decompress(base64.b64decode(host.bundle()))  # a plain gzip stream, as tar xz reads it
    assert base64.b64decode(host.bundle())[4:8] == b"\0\0\0\0"


def test_over_the_limit_nothing_is_sent(monkeypatch):
    monkeypatch.setattr(host, "LIMIT", 10)
    monkeypatch.setattr(host, "aws_session", lambda: pytest.fail("no AWS call over the limit"))
    with pytest.raises(SystemExit):
        host.run("true")


class FakeSsm:
    class exceptions:
        class InvocationDoesNotExist(Exception):
            pass

    def __init__(self):
        self.sent = None

    def send_command(self, **fields):
        self.sent = fields
        return {"Command": {"CommandId": "c-1"}}

    def get_command_invocation(self, **fields):
        return {"Status": "Success", "StandardOutputContent": "fine\n", "StandardErrorContent": "", "ResponseCode": 0}


def test_the_states_reach_the_command_that_named_them(monkeypatch):
    ssm = FakeSsm()

    class Session:
        def client(self, name):
            return ssm

    monkeypatch.setattr(host, "aws_session", Session)
    monkeypatch.setattr(host, "instance", lambda session: "i-1")
    monkeypatch.setattr(host.time, "sleep", lambda seconds: None)
    assert host.run("true", states=["1.9.1"]) == (0, "fine\n")
    assert host.bundle(states=["1.9.1"]) in ssm.sent["Parameters"]["commands"][0]
    assert host.run("true") == (0, "fine\n")
    assert host.bundle(states=["1.9.1"]) not in ssm.sent["Parameters"]["commands"][0]


def test_the_command_line_names_states_and_folders(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(host, "run", lambda *args: calls.append(args) or (0, "done"))
    monkeypatch.setattr(sys, "argv", ["host.py", "--state", "1.8.2", "--folder", "scripts/build.py", "python3", "x.py"])
    with pytest.raises(SystemExit) as stop:
        host.main()
    assert stop.value.code == 0
    assert calls == [("python3 x.py", 600, ["1.8.2"], ["scripts/build.py"])]


def test_the_scripts_ask_for_the_states_they_build():
    """reset.py builds the baseline, and rehearse.py the three versions of the video."""
    assert 'states=[BASELINE]' in (host.ROOT / "scripts/demos/kubernetes/reset.py").read_text()
    assert "states=STATES" in (host.ROOT / "scripts/demos/kubernetes/rehearse.py").read_text()
    versions = {path.name for path in (host.ROOT / "demo-states/catalog").iterdir()}
    assert set(rehearse.STATES) <= versions


@pytest.mark.parametrize("module", ["build", "check", "deploy", "kube", "oci", "reset", "rehearse", "common", "publish",
                                    "run_state"])
def test_what_runs_on_the_host_imports_from_the_bundle_alone(module, tmp_path):
    """The bundle is all the host has: a module left out stops the command before it starts."""
    unpack(host.bundle(), tmp_path)
    code = ("import sys; sys.path[:0] = ['scripts/demos/kubernetes', 'scripts']; "
            f"import {module}; print({module}.__file__)")
    # -P: the repository's own folders stay off the path; only the host's packages and these files.
    reply = subprocess.run([sys.executable, "-P", "-c", code], cwd=tmp_path, capture_output=True, text=True,
                           env={**os.environ, "PYTHONPATH": "", "WORKSPACE": str(tmp_path)})
    assert reply.returncode == 0, reply.stderr
    assert (tmp_path / reply.stdout.strip()).resolve().is_relative_to(tmp_path.resolve())


def test_k3s_starts_from_the_bundle_alone(tmp_path):
    """demo.sh up, down and status: k3s.py reaches its own root check, past every import."""
    unpack(host.bundle(), tmp_path)
    reply = subprocess.run([sys.executable, "scripts/demos/kubernetes/host/k3s.py", "status"], cwd=tmp_path,
                           capture_output=True, text=True, env={**os.environ, "PYTHONPATH": ""})
    assert "ModuleNotFoundError" not in reply.stderr
    assert "Run as root" in reply.stderr or os.geteuid() == 0
