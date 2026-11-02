"""up, down, status and the reset: what each runs, in what order, and when it refuses."""

import base64
import json
import subprocess
import sys
from pathlib import Path

import pytest

import demo
import remote
import reset
import who

HERE = Path(__file__).resolve().parents[1]
ONE_PRODUCTION = "One catalog production at a time: scripts/demo.sh down argocd first."


class FakeSession:
    def __init__(self, running=()):
        self.running = running

    def client(self, name):
        assert name == "ec2"
        return self

    def describe_instances(self, Filters):
        name = next(f["Values"][0] for f in Filters if f["Name"] == "tag:Name")
        states = next(f["Values"] for f in Filters if f["Name"] == "instance-state-name")
        assert states == ["pending", "running"]
        return {"Reservations": [{"Instances": [{"InstanceId": "i-1"}]}] if name in self.running else []}


@pytest.fixture
def calls(monkeypatch):
    """Everything demo.py runs, in order."""
    made = []
    monkeypatch.setattr(demo, "video8", lambda action: made.append(("video8", action)) or 0)
    monkeypatch.setattr(remote, "run", lambda command, timeout=600: made.append(("host", command)) or (0, "done\n"))
    return made


def session(monkeypatch, *running):
    monkeypatch.setattr(demo, "aws_session", lambda: FakeSession(running))


def test_up_refuses_while_the_cluster_host_runs(monkeypatch, calls, capsys):
    session(monkeypatch, demo.CLUSTER_HOST, demo.TOOLS_HOST)
    with pytest.raises(SystemExit):
        demo.up()
    assert calls == [] and ONE_PRODUCTION in capsys.readouterr().err


def test_up_is_video_8s_up_otherwise(monkeypatch, calls):
    session(monkeypatch, demo.TOOLS_HOST)
    assert demo.up() == 0 and calls == [("video8", "up")]


def test_video_19s_up_still_refuses_the_other_way_round():
    text = (HERE.parent / "argocd" / "demo.py").read_text()
    assert 'ONE_PRODUCTION = "One catalog production at a time: scripts/demo.sh down kubernetes first."' in text
    assert "if demo8_running(session):\n        fail(ONE_PRODUCTION)" in text


def test_down_removes_this_demos_objects_before_stopping_k3s(monkeypatch, calls):
    session(monkeypatch, demo.TOOLS_HOST)
    assert demo.down() == 0
    assert calls == [("host", f"{remote.ON_HOST} remove"), ("video8", "down")]


def test_down_with_the_tools_host_stopped_only_runs_video_8s_down(monkeypatch, calls):
    session(monkeypatch)
    assert demo.down() == 0 and calls == [("video8", "down")]


def test_down_doesnt_stop_k3s_if_the_removal_failed(monkeypatch, calls):
    session(monkeypatch, demo.TOOLS_HOST)
    monkeypatch.setattr(remote, "run", lambda command, timeout=600: (1, "no"))
    with pytest.raises(SystemExit):
        demo.down()
    assert calls == []


def test_status_asks_the_host_and_reads_the_certificate(monkeypatch, calls, tmp_path, capsys):
    session(monkeypatch, demo.TOOLS_HOST)
    monkeypatch.setattr(demo, "KUBECONFIG", tmp_path / "sam")
    assert demo.status() == 0
    out = capsys.readouterr().out
    assert calls == [("host", f"{remote.ON_HOST} status")] and "Sam's certificate: none on this Mac" in out


def test_status_with_the_tools_host_stopped(monkeypatch, calls, tmp_path, capsys):
    session(monkeypatch)
    monkeypatch.setattr(demo, "KUBECONFIG", tmp_path / "sam")
    assert demo.status() == 0 and calls == []
    assert "k3s: stopped (the tools host is stopped)" in capsys.readouterr().out


def test_the_certificate_line_reads_its_expiry(tmp_path):
    key = tmp_path / "key"
    pem = subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-days", "1",
                          "-subj", "/CN=sam"], capture_output=True, check=True).stdout
    path = tmp_path / "sam"
    path.write_text(json.dumps({"users": [{"user": {"client-certificate-data": base64.b64encode(pem).decode()}}]}))
    assert demo.certificate_line(path).startswith("Sam's certificate: expires at ")


@pytest.fixture
def steps(monkeypatch, tmp_path):
    made = []

    def run_script(path, *arguments):
        made.append((Path(path).name, *arguments))

    monkeypatch.setattr(reset, "run_script", run_script)
    monkeypatch.setattr(remote, "run", lambda command, timeout=600: made.append(("host", command)) or (
        0, "secret/catalog-db unchanged\nbusybox:1.37 on the node\nmark 2026-11-02T13:21:50Z\n"))
    monkeypatch.setattr(who, "store_mark", lambda mark: made.append(("mark", mark)))
    monkeypatch.setattr(sys, "argv", ["reset.py"])
    return made


def test_the_reset_goes_in_the_specs_order(steps, capsys):
    reset.main()
    assert steps == [("reset.py", "--no-stage"), ("host", f"{remote.ON_HOST} prepare"),
                     ("mark", "2026-11-02T13:21:50Z"), ("stage.py", "catalog", "1.9.1")]
    assert capsys.readouterr().out.rstrip().endswith("Ready for a take")


def test_the_reset_uses_video_8s_reset():
    assert reset.VIDEO8_RESET == HERE.parent / "kubernetes" / "reset.py"


def test_a_dry_run_changes_nothing(steps, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["reset.py", "--dry-run"])
    reset.main()
    assert steps == []
    out = capsys.readouterr().out
    assert "would stage catalog 1.9.1 on main" in out and "Ready" not in out


def test_a_failed_preparation_stores_no_mark_and_stages_nothing(steps, monkeypatch, capsys):
    monkeypatch.setattr(remote, "run", lambda command, timeout=600: (1, "The audit log isn't on\n"))
    with pytest.raises(SystemExit):
        reset.main()
    assert steps == [("reset.py", "--no-stage")]
    assert "Ready" not in capsys.readouterr().out


def test_a_preparation_without_a_mark_stops():
    with pytest.raises(SystemExit):
        reset.mark_in("nothing here\n")


def test_the_mark_is_the_clusters_own_time_from_its_last_line():
    assert reset.mark_in("mark old\nbusybox\nmark 2026-11-02T13:21:50Z\n") == "2026-11-02T13:21:50Z"


def test_can_i_on_the_command_line_finds_this_folder():
    """scripts/tests/test_demo_names.py holds the dash rule that reset.py and demo.sh share."""
    assert (HERE / "reset.py").exists() and HERE.name == "can_i"
    assert not (HERE.parent / "can-i").exists()  # a folder of that name would win over the rule
