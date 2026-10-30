"""Video 16's acceptance checks 1, 2, 3 and 6, without a model call. One ok or FAIL line each.

    python scripts/demos/agent_ship/check.py                  1 on the Mac, then 2, 3 and 6 on the tools host
    python scripts/demos/agent_ship/check.py --no-github      leave out 2, which pushes and opens a test pull request

1. The agent's app has exactly contents write, pull requests write and
   metadata read, is installed on release-demo only, and is in no ruleset's
   bypass list (ruleset.py --check), with your gh login.
2. With an agent token, inside the container: pushes to main, feature/x and
   a tag fail; agent/check-<time> takes a push; its pull request opens; its
   own approval, its merge, a check run and a workflow change are refused.
3. Inside the container: video 9's checks 1 to 5 (the role, staging's logs,
   production refused, the deploy role and SSM refused, nothing else in the
   environment); Jenkins, JFrog and Splunk on the tools host don't answer.
6. The last agent-task build's log holds no secret: gitleaks finds nothing,
   and the API key's value isn't in it.
"""

import argparse
import datetime
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import agent  # noqa: E402
from common import get_parameter, ssm_client  # noqa: E402

GITLEAKS = "zricethezav/gitleaks:v8.30.1@sha256:c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f"


def line(ok, text):
    print(f"{'ok  ' if ok else 'FAIL'} {text}", flush=True)
    return ok


def check_app():
    ssm = ssm_client()
    found = agent.installation(ssm)
    permissions = dict(sorted(found["permissions"].items()))
    ok = [line(permissions == dict(sorted(agent.PERMISSIONS.items())), f"1. the agent app's permissions: {permissions}")]
    import urllib.request
    import json

    token, _ = agent.token(ssm)
    request = urllib.request.Request("https://api.github.com/installation/repositories", headers={
        "Authorization": f"token {token}", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        names = [r["full_name"] for r in json.loads(response.read())["repositories"]]
    repository = get_parameter(ssm, "github/repository")
    ok.append(line(found["repository_selection"] == "selected" and names == [repository],
                   f"1. installed on {', '.join(names) or 'nothing'} ({found['repository_selection']})"))
    ruleset = subprocess.run([sys.executable, str(HERE / "ruleset.py"), "--check"])
    ok.append(ruleset.returncode == 0)
    return all(ok)


def on_host(with_github):
    """2, 3 and 6, here on the tools host."""
    import tempfile

    access = agent_access()
    ssm = ssm_client()
    repository = get_parameter(ssm, "github/repository")
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S")
    image = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", access.image_tag()],
                           capture_output=True, text=True).stdout.strip()
    if not image:
        return line(False, "2. no coding-agent image on the host: scripts/demo.sh up agent-ship builds it")
    blocked = [f"{ip}:{port}" for name, ports in (("jenkins", (8080,)), ("jcr", (8082,)), ("splunk", (8000, 8088)))
               for ip in [container_ip(f"release-demo-{name}-1")] if ip for port in ports]
    private = subprocess.run(["hostname", "-I"], capture_output=True, text=True).stdout.split()
    blocked += [f"{private[0]}:8082"] if private else []
    with tempfile.TemporaryDirectory(prefix="agent-check-") as folder:
        clone = Path(folder) / "release-demo"
        import agent_task

        author, email = agent.bot_identity(get_parameter(ssm, "github/agent-app-slug"))
        agent_task.clone(repository, clone, f"agent/check-{stamp}", author, email)
        github_token, _ = agent.token(ssm)
        credentials, _ = access.assume()
        env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": "/tmp", "GH_TOKEN": github_token, **credentials,
               "AWS_REGION": access.REGION, "ANTHROPIC_API_KEY": "not-a-key-checks-make-no-model-call"}
        names = ["GH_TOKEN", *access.PASSED]
        base = ["docker", "run", "--rm", "--network", agent_task.NETWORK, "--user", "agent",
                *[f for n in names for f in ("-e", n)], "-v", f"{clone}:/work/release-demo",
                "-v", f"{HERE / 'container_checks.py'}:/checks/container_checks.py:ro",
                "-v", f"{access.HERE / 'checks.py'}:/checks/checks.py:ro", "-w", "/work/release-demo", image]
        account = subprocess.run(["aws", "sts", "get-caller-identity", "--query", "Account", "--output", "text"],
                                 capture_output=True, text=True).stdout.strip()
        allowed = sorted({*names, *access.RUNTIME, *access.image_env_names(image)})
        nine = subprocess.run([*base, "python3", "/checks/checks.py", "--account", account,
                               "--allowed-env", ",".join(allowed)], env=env)
        sixteen = subprocess.run([*base, "python3", "/checks/container_checks.py", "--repository", repository,
                                  "--stamp", stamp, "--blocked", ",".join(blocked),
                                  *([] if with_github else ["--skip-github"])], env=env)
    logs = check_logs(ssm)
    return nine.returncode == 0 and sixteen.returncode == 0 and logs


def container_ip(name):
    found = subprocess.run(["docker", "inspect", "--format", "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}",
                            name], capture_output=True, text=True)
    return found.stdout.split()[0] if found.returncode == 0 and found.stdout.split() else None


def check_logs(ssm):
    """6: gitleaks over the last agent-task build's console log, and the API key's value not in it."""
    import base64
    import tempfile
    import urllib.request

    auth = base64.b64encode(f"admin:{get_parameter(ssm, 'jenkins/admin-password')}".encode()).decode()
    try:
        request = urllib.request.Request("http://localhost:8080/job/agent-task/lastBuild/consoleText",
                                         headers={"Authorization": f"Basic {auth}"})
        with urllib.request.urlopen(request, timeout=30) as response:
            text = response.read().decode(errors="replace")
    except OSError as error:
        return line(False, f"6. no agent-task log to read ({error.__class__.__name__}): run one first")
    key = get_parameter(ssm, "anthropic/api-key")
    with tempfile.TemporaryDirectory(prefix="agent-log-") as folder:
        (Path(folder) / "agent-task.log").write_text(text)
        scan = subprocess.run(["docker", "run", "--rm", "-v", f"{folder}:/scan:ro", GITLEAKS, "dir", "/scan",
                               "--no-banner", "--redact"], capture_output=True, text=True)
    return line(scan.returncode == 0 and key not in text,
                "6. the last agent-task log: gitleaks finds nothing, and the API key isn't in it")


def agent_access():
    import agent_task

    return agent_task.agent_access()


def main():
    import host

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--no-github", action="store_true", help="leave out check 2, which pushes and opens a pull request")
    parser.add_argument("--here", action="store_true")
    args = parser.parse_args()
    if args.here or host.on_host():
        return 0 if on_host(not args.no_github) else 1
    first = check_app()
    rest = host.hand_over("agent-ship-check", "demos/agent_ship/check.py",
                          ["--no-github"] if args.no_github else [], title="check.py agent-ship")
    return 0 if first and rest == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
