"""What reaches the tools host, that each script starts on its own, and that gitleaks is clean."""

import base64
import io
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

import remote

HERE = Path(__file__).resolve().parents[1]
ROOT = HERE.parents[2]
# This demo's own files add little to the bundle, whose room other videos need too. As base64.
OWN_BYTES = 8_000


def unpack(files, folder):
    tarfile.open(fileobj=io.BytesIO(base64.b64decode(files))).extractall(folder, filter="data")


def own_bundle():
    """The bundle one of this demo's commands sends, through the real remote.run."""
    sent = {}

    def run(command, timeout):
        sent["files"] = remote.host.bundle()
        return 0, ""

    original = remote.host.run
    remote.host.run = run
    try:
        remote.run("true")
    finally:
        remote.host.run = original
    return sent["files"]


def run_unpacked(folder, *command):
    """As the host runs it: cd into the unpacked files, with python3 from the environment."""
    env = {**os.environ, "PATH": f"{Path(sys.executable).parent}:{os.environ['PATH']}"}
    return subprocess.run(command, cwd=folder, env=env, capture_output=True, text=True)


def test_the_extra_files_go_with_this_demos_commands_only():
    usual = list(remote.host.FILES)
    names = tarfile.open(fileobj=io.BytesIO(base64.b64decode(own_bundle()))).getnames()
    assert {"scripts/demos/can_i/on_host.py", "scripts/demos/can_i/cluster/developer.yaml",
            "scripts/demos/can_i/cluster/secrets.yaml", "scripts/demos/can_i/take/before.yaml",
            "scripts/ci_env.py"} <= set(names)
    assert not any(name.startswith("scripts/demos/can_i/tests") for name in names)
    assert "scripts/demos/can_i/connect.py" not in names  # the Mac's scripts stay on the Mac
    assert remote.host.FILES == usual  # video 8's own commands send what they always sent


def test_a_file_host_py_already_sends_doesnt_go_twice(monkeypatch):
    usual = remote.host.FILES
    monkeypatch.setattr(remote.host, "FILES", [*usual, *(n for n in ["scripts/ci_env.py"] if n not in usual)])
    names = tarfile.open(fileobj=io.BytesIO(base64.b64decode(own_bundle()))).getnames()
    assert names.count("scripts/ci_env.py") == 1


def test_the_host_can_run_this_demos_commands_from_the_bundle(tmp_path):
    """The bundle is all the host has: every import must be in it."""
    unpack(own_bundle(), tmp_path)
    k3s = run_unpacked(tmp_path, "python3", "scripts/demos/kubernetes/host/k3s.py")
    assert "Run as root" in k3s.stderr + k3s.stdout, k3s.stderr
    step = run_unpacked(tmp_path, "python3", "scripts/demos/can_i/on_host.py", "--help")
    assert step.returncode == 0 and "prepare" in step.stdout, step.stderr


def test_this_demo_adds_little_to_the_bundle():
    assert len(own_bundle()) - len(remote.host.bundle()) < OWN_BYTES


def test_the_bundle_stays_under_ssms_limit():
    """Over the limit today, on main too: video 25's branch owns that fix. From its merge on, this holds it."""
    if len(remote.host.bundle()) > remote.host.LIMIT:
        pytest.skip("video 8's bundle is over its limit until video 25's fix is merged")
    assert len(own_bundle()) <= remote.host.LIMIT


def test_video_8s_own_bundle_can_run_on_the_host(tmp_path):
    """scripts/common.py imports scripts/ci_env.py, which host.py's FILES doesn't name on main. This
    tells the merge: once the bundle is under its limit, it must also start on the host."""
    if len(remote.host.bundle()) > remote.host.LIMIT:
        pytest.skip("video 8's bundle is over its limit until video 25's fix is merged")
    unpack(remote.host.bundle(), tmp_path)
    k3s = run_unpacked(tmp_path, "python3", "scripts/demos/kubernetes/host/k3s.py")
    assert "Run as root" in k3s.stderr + k3s.stdout, f"add scripts/ci_env.py to host.py's FILES: {k3s.stderr}"


@pytest.mark.parametrize("script", ["who.py", "check.py", "connect.py", "reset.py", "demo.py", "on_host.py"])
def test_the_script_starts(script):
    result = subprocess.run([sys.executable, str(HERE / script), "--help"], capture_output=True, text=True, cwd=ROOT)
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout


@pytest.mark.parametrize("action", ["up", "down", "status"])
def test_demo_sh_finds_each_step(action):
    assert os.access(HERE / f"{action}.sh", os.X_OK)
    text = (HERE / f"{action}.sh").read_text()
    assert f'scripts/demos/can_i/demo.py" {action} "$@"' in text


@pytest.mark.skipif(not shutil.which("gitleaks"), reason="gitleaks isn't installed")
@pytest.mark.parametrize("folder", ["scripts/demos/can_i", "scripts/demos/kubernetes/host"])
def test_gitleaks_is_clean(folder):
    result = subprocess.run(["gitleaks", "dir", str(ROOT / folder), "--no-banner", "--redact",
                             "--config", str(ROOT / ".gitleaks.toml")], capture_output=True, text=True, cwd=ROOT)
    assert result.returncode == 0, result.stdout + result.stderr


def test_no_em_or_en_dash_in_this_demo():
    dashes = {chr(0x2014), chr(0x2013)}
    found = [str(path) for path in HERE.rglob("*") if path.is_file() and path.suffix in (".py", ".yaml", ".sh")
             and dashes & set(path.read_text())]
    assert found == []
