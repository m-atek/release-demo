"""Video 9: the agent's workspace, what reaches its container, the checks
run inside it, the reset, and the role's policy and image as written. No
AWS, Docker or model call: those are faked or read from the files.

    .venv/bin/python -m pytest -q scripts/demos/agent-access/tests
"""

import datetime
import json
import os
import re
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

import agent_access as agent

HERE = Path(__file__).resolve().parents[1]


def load_reset():
    """agent-access/reset.py by its path: scripts/reset.py and other demos' share the name."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("agent_access_reset", HERE / "reset.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


reset = load_reset()

ROOT = agent.ROOT
CHECKS = Path(agent.__file__).with_name("checks.py")


def test_the_workspace_holds_orders_service_1_4_0_and_its_package_only(tmp_path):
    folder = agent.workspace(base=tmp_path)
    assert sorted(p.name for p in folder.iterdir()) == ["dist", "orders-service"]
    assert (folder / "orders-service" / "VERSION").read_text().strip() == "1.4.0"
    assert "Delivery estimate" in (folder / "orders-service" / "handler.py").read_text()
    dist = sorted(p.name for p in (folder / "dist").iterdir())
    assert dist == ["build-info.json", "orders-service-1.4.0.zip"]
    info = json.loads((folder / "dist" / "build-info.json").read_text())
    package = folder / "dist" / info["package"]
    assert info["sha256"] == agent.hashlib.sha256(package.read_bytes()).hexdigest()
    names = agent.zip_names(package)
    assert "handler.py" in names and "VERSION" in names and "build_info.json" in names
    assert not [n for n in names if n.startswith("tests/")], "tests stay out of the package, as in make build"
    for unwanted in ("README.md", "Jenkinsfile", "services.yaml", ".git"):
        assert not list(folder.rglob(unwanted))


def test_the_package_is_the_same_every_time(tmp_path):
    first = json.loads((agent.workspace(base=tmp_path) / "dist" / "build-info.json").read_text())
    second = json.loads((agent.workspace(base=tmp_path) / "dist" / "build-info.json").read_text())
    assert first["sha256"] == second["sha256"]


def test_the_role_line_gives_the_session_end_in_amsterdam_time():
    expires = datetime.datetime(2026, 11, 2, 13, 5, tzinfo=datetime.timezone.utc)
    line = "AWS role: release-demo-coding-agent, session coding-agent, until 14:05"
    assert agent.role_line(expires, width=120) == line
    # The take's terminal has 56 columns: the line breaks after a comma, never inside a word.
    assert agent.role_line(expires, width=56) == "AWS role: release-demo-coding-agent,\nsession coding-agent, until 14:05"
    summer = datetime.datetime(2026, 7, 2, 13, 5, tzinfo=datetime.timezone.utc)
    assert agent.role_line(summer, width=120).endswith("until 15:05")


def test_values_reach_the_container_by_name_only(monkeypatch):
    monkeypatch.setenv("AWS_PROFILE", "release-demo")
    monkeypatch.setenv("GH_TOKEN", "never-passed")
    credentials = {"AWS_ACCESS_KEY_ID": "AKIA-test", "AWS_SECRET_ACCESS_KEY": "secret-test", "AWS_SESSION_TOKEN": "t"}
    env = agent.container_env(credentials, "key-test")
    assert set(env) - {"PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG"} == set(agent.PASSED)
    assert "AWS_PROFILE" not in env and "GH_TOKEN" not in env
    flags = agent.env_flags()
    assert flags[::2] == ["-e"] * len(agent.PASSED) and flags[1::2] == agent.PASSED
    assert not [f for f in flags if "=" in f], "no value on the docker command line"


def test_run_starts_claude_code_with_two_mounts_and_records_production(tmp_path, monkeypatch, capsys):
    started = {}
    folder = agent.workspace(base=tmp_path)
    monkeypatch.setattr(agent, "required_image", lambda: "release-demo/coding-agent:abc")
    monkeypatch.setattr(agent, "workspace", lambda: folder)
    monkeypatch.setattr(agent, "production_live", lambda: {"function": "orders-service-production", "version": "41",
                                                           "app_version": "1.3.0"})
    expires = datetime.datetime(2026, 11, 2, 9, 30, tzinfo=datetime.timezone.utc)
    monkeypatch.setattr(agent, "assume", lambda: ({"AWS_ACCESS_KEY_ID": "a", "AWS_SECRET_ACCESS_KEY": "b",
                                                   "AWS_SESSION_TOKEN": "c"}, expires))
    monkeypatch.setattr(agent, "ssm_client", lambda: None)
    monkeypatch.setattr(agent, "get_parameter", lambda ssm, name: "sk-ant-test" if name == "anthropic/api-key" else None)
    monkeypatch.setattr(agent.os, "execvpe", lambda file, args, env: started.update(args=args, env=env))
    monkeypatch.setattr(agent.shutil, "get_terminal_size", lambda fallback: os.terminal_size((120, 40)))
    agent.run()
    out = capsys.readouterr().out
    assert out == "AWS role: release-demo-coding-agent, session coding-agent, until 10:30\n"
    args = started["args"]
    volumes = [args[i + 1] for i, a in enumerate(args) if a == "-v"]
    assert volumes == [f"{folder / 'orders-service'}:/work/orders-service", f"{folder / 'dist'}:/work/dist"]
    assert args[-2:] == ["release-demo/coding-agent:abc", "claude"] and "-it" in args and "--rm" in args
    assert "sk-ant-test" not in " ".join(args) and started["env"]["ANTHROPIC_API_KEY"] == "sk-ant-test"
    take = json.loads((folder / "take.json").read_text())
    assert take["production"]["version"] == "41"
    assert "sk-ant" not in json.dumps(take)


def fake_aws(tmp_path, monkeypatch, answers):
    """An aws executable on PATH that answers each command by its first two words."""
    script = tmp_path / "bin" / "aws"
    script.parent.mkdir()
    script.write_text("#!/usr/bin/env python3\n"
                      "import json, sys\n"
                      f"answers = json.loads({json.dumps(json.dumps(answers))})\n"
                      "import os, pathlib\n"
                      "args = sys.argv[1:]\n"
                      "open(" + repr(str(tmp_path / "calls.log")) + ", 'a').write(' '.join(args) + '\\n')\n"
                      "# As AWS CLI v2 does on every call: its own session cache, never a credential.\n"
                      "cache = pathlib.Path(os.environ['HOME'], '.aws', 'cli', 'cache')\n"
                      "cache.mkdir(parents=True, exist_ok=True); (cache / 'session.db').touch()\n"
                      "code, out, err = answers.get(' '.join(args[:2]), [0, '', ''])\n"
                      "sys.stdout.write(out); sys.stderr.write(err); sys.exit(code)\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{script.parent}{os.pathsep}{os.environ['PATH']}")


def deny(operation, action, resource, code="AccessDeniedException"):
    return [254, "", f"\nAn error occurred ({code}) when calling the {operation} operation: User: "
                     "arn:aws:sts::123456789012:assumed-role/release-demo-coding-agent/coding-agent is not "
                     f"authorized to perform: {action} on resource: {resource} with an explicit deny in an "
                     "identity-based policy\n"]


PRODUCTION = "arn:aws:lambda:eu-west-1:123456789012:function:orders-service-production"
REFUSING = {
    "sts get-caller-identity": [0, "arn:aws:sts::123456789012:assumed-role/release-demo-coding-agent/coding-agent\n", ""],
    "logs filter-log-events": [0, "5\n", ""],
    "lambda update-function-code": deny("UpdateFunctionCode", "lambda:UpdateFunctionCode", PRODUCTION),
    "lambda publish-version": deny("PublishVersion", "lambda:PublishVersion", PRODUCTION),
    "lambda update-alias": deny("UpdateAlias", "lambda:UpdateAlias", PRODUCTION + ":live"),
    "lambda get-function-configuration": deny("GetFunctionConfiguration", "lambda:GetFunctionConfiguration", PRODUCTION),
    "lambda invoke": deny("Invoke", "lambda:InvokeFunction", PRODUCTION),
    "sts assume-role": deny("AssumeRole", "sts:AssumeRole",
                            "arn:aws:iam::123456789012:role/release-demo-deploy-orders-service", "AccessDenied"),
    "ssm get-parameter": deny("GetParameter", "ssm:GetParameter",
                              "arn:aws:ssm:eu-west-1:123456789012:parameter/release-demo/jcr/token"),
}


def run_checks(tmp_path, allowed):
    env = {name: os.environ[name] for name in ("PATH", "HOME")}
    return subprocess.run([sys.executable, str(CHECKS), "--account", "123456789012", "--allowed-env", allowed],
                          capture_output=True, text=True, env={**env, "HOME": str(tmp_path / "home")})


def test_the_checks_pass_when_aws_refuses_everything_but_staging(tmp_path, monkeypatch):
    fake_aws(tmp_path, monkeypatch, REFUSING)
    (tmp_path / "home").mkdir()
    # The Mac's Python adds two variables of its own; the container's runs without them.
    result = run_checks(tmp_path, "PATH,HOME,LC_CTYPE,__CF_USER_TEXT_ENCODING")
    lines = result.stdout.splitlines()
    assert result.returncode == 0, result.stdout
    assert [l[:4] for l in lines[:-1]] == ["ok  "] * 11
    assert "1. sts get-caller-identity names assumed-role/release-demo-coding-agent/coding-agent" in lines[0]
    assert json.loads(lines[-1]) == {"passed": 5, "of": 5}


def test_every_production_call_in_the_checks_changes_nothing_even_if_allowed(tmp_path, monkeypatch):
    fake_aws(tmp_path, monkeypatch, REFUSING)
    (tmp_path / "home").mkdir()
    run_checks(tmp_path, "PATH,HOME")
    calls = (tmp_path / "calls.log").read_text().splitlines()
    production = [c for c in calls if "orders-service-production" in c]
    assert len(production) == 5
    assert "--dry-run" in next(c for c in production if c.startswith("lambda update-function-code"))
    assert "--code-sha256 " + "0" * 64 in next(c for c in production if c.startswith("lambda publish-version"))
    assert "--revision-id" in next(c for c in production if c.startswith("lambda update-alias"))
    assert "--invocation-type DryRun" in next(c for c in production if c.startswith("lambda invoke"))
    assert "--query Parameter.Name" in next(c for c in calls if c.startswith("ssm get-parameter"))
    assert "--with-decryption" not in " ".join(calls)


def test_a_production_call_that_isnt_refused_fails_its_check(tmp_path, monkeypatch):
    allowed = dict(REFUSING, **{"lambda update-alias": [0, "{}", ""]})
    fake_aws(tmp_path, monkeypatch, allowed)
    (tmp_path / "home").mkdir()
    result = run_checks(tmp_path, "PATH,HOME")
    assert result.returncode == 1
    assert "FAIL 3. update-alias on orders-service-production: allowed" in result.stdout


def test_an_implicit_deny_is_not_enough_for_check_3(tmp_path, monkeypatch):
    implicit = [254, "", "An error occurred (AccessDeniedException) when calling the UpdateFunctionCode operation: "
                         "User: x is not authorized to perform: lambda:UpdateFunctionCode on resource: y because no "
                         "identity-based policy allows the lambda:UpdateFunctionCode action\n"]
    fake_aws(tmp_path, monkeypatch, dict(REFUSING, **{"lambda update-function-code": implicit}))
    (tmp_path / "home").mkdir()
    assert "FAIL 3. update-function-code" in run_checks(tmp_path, "PATH,HOME").stdout


def test_check_5_finds_credentials_and_unlisted_variables(tmp_path, monkeypatch):
    fake_aws(tmp_path, monkeypatch, REFUSING)
    (tmp_path / "home" / ".aws").mkdir(parents=True)
    (tmp_path / "home" / ".gitconfig").write_text("[credential]\n\thelper = store\n")
    result = run_checks(tmp_path, "PATH")
    assert "FAIL 5. no ~/.aws, SSH keys or git credentials" in result.stdout
    assert ".gitconfig (a credential helper)" in result.stdout
    assert "FAIL 5. environment holds only the listed variables, not HOME" in result.stdout


def test_reset_tidies_a_take_that_left_production_alone(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(reset.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
    folder = tmp_path / "release-demo-agent-abc"
    folder.mkdir()
    (folder / "take.json").write_text(json.dumps({"production": {"version": "41", "app_version": "1.3.0"}}))
    live = {"function": "orders-service-production", "version": "41", "app_version": "1.3.0"}
    assert reset.reset(base=tmp_path, live=live) == 0
    assert not folder.exists()
    assert reset.reset(base=tmp_path, live=live) == 0, "safe to run twice"
    assert "nothing to tidy" in capsys.readouterr().out


def test_reset_keeps_the_evidence_when_production_moved(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(reset.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
    folder = tmp_path / "release-demo-agent-abc"
    folder.mkdir()
    (folder / "take.json").write_text(json.dumps({"production": {"version": "41", "app_version": "1.3.0"}}))
    live = {"function": "orders-service-production", "version": "42", "app_version": "1.4.0"}
    assert reset.reset(base=tmp_path, live=live) == 1
    assert folder.exists()
    assert "moved from version 41 (1.3.0) to 42 (1.4.0)" in capsys.readouterr().out


def test_the_role_denies_everything_the_video_tries_explicitly():
    policy = (ROOT / "infra" / "demos" / "9-agent-access" / "role.tf").read_text()
    denies = re.findall(r'effect\s+=\s+"Deny"\s+actions\s+=\s+\[([^\]]+)\]\s+(?:#[^\n]*\n\s+)?resources\s+=\s+\[([^\]]+)\]',
                        policy)
    pairs = [(actions, resources) for actions, resources in denies]
    assert any('"lambda:*"' in a and "*-production" in r and "*-production:*" in r for a, r in pairs)
    assert any('"logs:*"' in a and "/aws/lambda/*-production" in r for a, r in pairs)
    assert any("ssm:GetParametersByPath" in a and "parameter/release-demo/*" in r for a, r in pairs)
    assert any('"sts:AssumeRole"' in a and "role/release-demo-*" in r for a, r in pairs)
    assert any('"iam:*"' in a and '"*"' in r for a, r in pairs)
    allows = policy.split("What it may never do")[0]
    assert "lambda:GetFunctionConfiguration" in allows and "logs:FilterLogEvents" in allows
    assert "UpdateFunctionCode" not in allows and '"*"' not in allows.split("principals")[1]
    assert "max_session_duration = 3600" in policy
    assert "AWSReservedSSO_AdministratorAccess_*" in policy and "role/release-demo-tools-host" in policy


def test_the_image_pins_every_download_and_holds_no_credential():
    dockerfile = agent.DOCKERFILE.read_text()
    assert re.search(r"^FROM [^\s]+@sha256:[0-9a-f]{64}$", dockerfile, re.M)
    assert re.search(r"CLAUDE_CODE_VERSION=\d+\.\d+\.\d+$", dockerfile, re.M)
    assert re.search(r"AWS_CLI_VERSION=\d+\.\d+\.\d+$", dockerfile, re.M)
    claude = dockerfile.split("# Claude Code's native binary")[1].split("\nRUN ")[1]
    assert len(re.findall(r"sha256=[0-9a-f]{64}", claude)) == 2, "Claude Code for amd64 and arm64"
    assert "gpgv --keyring" in dockerfile and "USER agent" in dockerfile
    settings = json.loads((agent.CONTEXT / "managed-settings.json").read_text())
    for command in ("aws", "ls", "cat", "unzip", "python3"):
        assert f"Bash({command}:*)" in settings["permissions"]["allow"]
    assert "Read" in settings["permissions"]["allow"]
    assert settings["permissions"]["defaultMode"] == "default"
    for path in agent.CONTEXT.rglob("*"):
        text = path.read_text(errors="replace")
        assert not re.search(r"sk-ant-|AKIA[0-9A-Z]{16}|BEGIN (RSA |OPENSSH )?PRIVATE KEY", text), path


def test_the_image_tag_follows_its_files():
    assert agent.image_tag() == agent.image_tag()
    assert re.fullmatch(r"release-demo/coding-agent:[0-9a-f]{12}", agent.image_tag())


def test_the_entrypoint_records_the_key_answer_outside_the_image(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").write_text(json.dumps({"hasCompletedOnboarding": True}))
    script = agent.CONTEXT / "entrypoint.sh"
    result = subprocess.run(["sh", str(script), "true"], capture_output=True, text=True,
                            env={"PATH": os.environ["PATH"], "HOME": str(home),
                                 "ANTHROPIC_API_KEY": "sk-ant-api03-" + "x" * 60 + "ABCDEFGHIJKLMNOPQRST"})
    assert result.returncode == 0, result.stderr
    config = json.loads((home / ".claude.json").read_text())
    assert config["hasCompletedOnboarding"] is True
    assert config["customApiKeyResponses"] == {"approved": ["ABCDEFGHIJKLMNOPQRST"], "rejected": []}
