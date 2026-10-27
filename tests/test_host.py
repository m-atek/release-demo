"""Handing a script to the tools host through SSM, following its log, and
staging on main with the GitHub App's token, with SSM and git faked."""

import subprocess

import pytest

import host


class FakeHost:
    id = "i-0123"

    def __init__(self, replies):
        self.replies = list(replies)
        self.scripts = []

    def shell(self, script, timeout=60):
        self.scripts.append(script)
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return 0, reply, ""


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(host.time, "sleep", lambda seconds: None)


def test_hand_over_starts_a_unit_and_prints_its_log_to_the_exit_line(capsys):
    fake = FakeHost(["started 120\n", "active 0\n", "active 30\nStep one\nStep two\n",
                     "inactive 12\n== exit 3\n"])
    assert host.hand_over("reset", "reset.py", ["main", "--dry-run"], host=fake) == 3
    start = fake.scripts[0]
    assert "systemd-run --quiet --unit release-demo-reset" in start
    assert "append:/var/log/release-demo-reset.log" in start
    assert "/opt/release-demo-venv/bin/python -u scripts/reset.py --here main --dry-run" in start
    assert "git pull --ff-only" in start
    assert "tail -c +121 " in fake.scripts[1] and "tail -c +151 " in fake.scripts[3]
    out = capsys.readouterr().out
    assert "Step one\nStep two\n" in out and "== exit" not in out


def test_hand_over_follows_a_run_already_going_from_its_last_lines(capsys):
    fake = FakeHost(["running 10000\n", "inactive 40\npartial line\nlast line\n== exit 0\n"])
    assert host.hand_over("history", "history.py", [], host=fake) == 0
    assert "tail -c +6001 " in fake.scripts[1]
    out = capsys.readouterr().out
    assert "already running" in out and "last line" in out and "partial line" not in out


def test_ctrl_c_stops_following_not_the_run(capsys):
    fake = FakeHost(["started 0\n", KeyboardInterrupt()])
    assert host.hand_over("history", "history.py", [], host=fake) == 130
    assert "history.py --follow" in capsys.readouterr().out


def test_a_unit_that_stopped_without_an_exit_line_fails(capsys):
    fake = FakeHost(["started 0\n", "failed 0\n"])
    assert host.hand_over("history", "history.py", [], host=fake) == 1


def test_follow_only_reads_the_end_of_the_log(capsys):
    fake = FakeHost(["inactive 500\n", "inactive 500\nold\nDone in 52m 10s.\n== exit 0\n"])
    assert host.hand_over("history", "history.py", [], follow_only=True, host=fake) == 0
    assert "tail -c +1 " in fake.scripts[1]
    assert "Done in 52m 10s." in capsys.readouterr().out


def test_a_stopped_tools_host_says_how_to_start_it():
    class Session:
        def client(self, name):
            return self

        def describe_instances(self, Filters):
            return {"Reservations": [{"Instances": [{"InstanceId": "i-1", "State": {"Name": "stopped"}}]}]}

    with pytest.raises(SystemExit):
        host.Host(Session())


def test_stage_pushes_from_a_fresh_clone_with_the_token_only_in_the_environment(monkeypatch, capsys):
    runs = []

    def run(command, cwd=None, env=None, capture_output=False, text=False):
        runs.append((command, cwd, env))
        return subprocess.CompletedProcess(command, 0, "Committed abc1234\nPushed to main\n", "")

    monkeypatch.setattr(host, "github_app_token", lambda ssm: ("ghs_secret", "owner/release-demo", {}))
    monkeypatch.setattr(host, "ssm_client", lambda: None)
    monkeypatch.setattr(host.subprocess, "run", run)
    host.stage("orders-service", "1.2.0")
    clone, helper, stage = runs
    assert clone[0][:2] == ["git", "clone"] and "https://github.com/owner/release-demo.git" in clone[0]
    assert helper[0][-2:] == ["credential.helper", host.CREDENTIALS]
    assert stage[0][1:] == ["scripts/stage.py", "orders-service", "1.2.0"] and str(stage[1]).endswith("release-demo")
    assert all("ghs_secret" not in " ".join(map(str, command)) for command, _, _ in runs)
    assert stage[2]["STAGE_TOKEN"] == "ghs_secret" and stage[2]["GIT_AUTHOR_NAME"] == "Release Pipeline"
    assert "  Pushed to main" in capsys.readouterr().out


def test_stage_stops_when_stage_py_fails(monkeypatch):
    monkeypatch.setattr(host, "github_app_token", lambda ssm: ("t", "owner/release-demo", {}))
    monkeypatch.setattr(host, "ssm_client", lambda: None)
    monkeypatch.setattr(host.subprocess, "run", lambda command, **kwargs: subprocess.CompletedProcess(
        command, 1 if "scripts/stage.py" in command else 0, "", "push rejected"))
    with pytest.raises(SystemExit):
        host.stage("orders-service", "1.2.0")


def test_the_credential_helper_answers_with_the_token_from_the_environment(tmp_path):
    """The helper as git runs it: through the shell, with the token from the environment."""
    script = host.CREDENTIALS.removeprefix("!")
    result = subprocess.run(["sh", "-c", script], env={"STAGE_TOKEN": "ghs_abc", "PATH": "/usr/bin:/bin"},
                            capture_output=True, text=True, check=True)
    assert result.stdout == "username=x-access-token\npassword=ghs_abc\n"


def test_stage_commits_one_stage_commit_and_pushes_it(tmp_path, monkeypatch, capsys):
    """The real thing against a bare copy of this repository: a shallow clone, stage.py, a push."""
    root = host.Path(host.__file__).resolve().parents[1]
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "--quiet", "--bare", str(root), str(bare)], check=True)
    head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    subprocess.run(["git", "-C", str(bare), "update-ref", "refs/heads/main", head], check=True)
    monkeypatch.setattr(host, "REMOTE", f"file://{bare}")
    monkeypatch.setattr(host, "github_app_token", lambda ssm: ("unused", "owner/release-demo", {}))
    monkeypatch.setattr(host, "ssm_client", lambda: None)

    host.stage("orders-service", "1.2.4")
    log = subprocess.run(["git", "-C", str(bare), "log", "-1", "--format=%an <%ae>|%s", "main"],
                         capture_output=True, text=True).stdout.strip()
    assert log == "Release Pipeline <noreply@es.nl>|demo: stage orders-service 1.2.4 [skip ci]"
    version = subprocess.run(["git", "-C", str(bare), "show", "main:services/orders-service/VERSION"],
                             capture_output=True, text=True).stdout.strip()
    assert version == "1.2.4"
    host.stage("orders-service", "1.2.4")  # again: nothing to commit
    assert "Nothing changed: main already has this version" in capsys.readouterr().out
