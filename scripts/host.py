"""Run history.py and reset.py on the tools host, and stage versions on main from there.

Both scripts need Jenkins and JFrog Container Registry, which answer on the
tools host's localhost and, from anywhere else, only through Cloudflare
Access. So they run on the host. From the Mac they hand themselves over
through SSM Run Command, as scripts/tools.sh does: the host pulls main,
starts the script as the systemd unit release-demo-<name>, which writes
/var/log/release-demo-<name>.log, and the Mac prints that log as it grows.
Ctrl-C stops the printing, not the run.

stage() runs stage.py in a fresh clone of main and pushes with the GitHub
App's token, so the host needs no git credentials of its own.
"""

import base64
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from common import aws_session, fail, github_api, github_app_token, ssm_client

HOST_ENV = Path("/etc/release-demo/host.env")
REPO_DIR = "/opt/release-demo"
PYTHON = "/opt/release-demo-venv/bin/python"
CHUNK = 16000  # log bytes per poll: SSM returns at most 24,000 characters of output
EXIT = "== exit "
DONE = ("Success", "Failed", "Cancelled", "TimedOut", "Undeliverable", "Terminated", "DeliveryTimedOut",
        "ExecutionTimedOut")

# The host has no git identity: stage commits come from the pipeline.
IDENTITY = {"GIT_AUTHOR_NAME": "Release Pipeline", "GIT_AUTHOR_EMAIL": "noreply@es.nl",
            "GIT_COMMITTER_NAME": "Release Pipeline", "GIT_COMMITTER_EMAIL": "noreply@es.nl"}
# git asks this helper for the push password: the token stays in the environment, out of files and ps.
CREDENTIALS = '!f() { echo username=x-access-token; echo "password=$STAGE_TOKEN"; }; f'

START = """set -e
cd {repo}
git pull --ff-only -q || echo "git pull failed: the host runs the code it has"
if systemctl is-active --quiet {unit}; then echo "running $(stat -c %s {log} 2>/dev/null || echo 0)"; exit 0; fi
systemctl reset-failed {unit} 2>/dev/null || true
size=$(stat -c %s {log} 2>/dev/null || echo 0)
echo "== $(date -u +%Y-%m-%dT%H:%M:%SZ) {title}" >> {log}
systemd-run --quiet --unit {unit} --working-directory={repo} --setenv=HOME=/root \\
  --property=StandardOutput=append:{log} --property=StandardError=append:{log} /bin/sh -c {command}
echo "started $size"
"""

FOLLOW = """state=$(systemctl is-active {unit} || true)
size=$(stat -c %s {log} 2>/dev/null || echo 0)
n=$((size - {offset}))
[ "$n" -gt {chunk} ] && n={chunk}
[ "$n" -lt 0 ] && n=0
echo "$state $n"
[ "$n" -gt 0 ] && tail -c +{start} {log} | head -c "$n"
true
"""


def on_host():
    """True on the EC2 tools host, in a shell or a Jenkins job alike."""
    return HOST_ENV.exists()


class Host:
    """The running tools host, through SSM Run Command with your AWS sign-in."""

    def __init__(self, session=None):
        session = session or aws_session()
        self.ssm = session.client("ssm")
        reply = session.client("ec2").describe_instances(Filters=[
            {"Name": "tag:Name", "Values": ["release-demo-tools-host"]},
            {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]}])
        instances = [i for r in reply["Reservations"] for i in r["Instances"]]
        if not instances:
            fail("There is no tools host. Apply infra/ first: scripts/tf.sh apply")
        if instances[0]["State"]["Name"] != "running":
            fail("The tools host is stopped. Start it with scripts/tools.sh up, then run this again.")
        self.id = instances[0]["InstanceId"]

    def shell(self, script, timeout=60):
        """Run a shell script on the host as root. Returns (exit code, stdout, stderr)."""
        command = self.ssm.send_command(
            InstanceIds=[self.id], DocumentName="AWS-RunShellScript", TimeoutSeconds=60,
            Parameters={"commands": [script], "executionTimeout": [str(timeout)]})["Command"]["CommandId"]
        deadline = time.monotonic() + timeout + 60
        while time.monotonic() < deadline:
            time.sleep(1)
            try:
                reply = self.ssm.get_command_invocation(CommandId=command, InstanceId=self.id)
            except self.ssm.exceptions.InvocationDoesNotExist:
                continue
            if reply["Status"] in DONE:
                return reply["ResponseCode"], reply["StandardOutputContent"], reply["StandardErrorContent"]
        fail("The tools host didn't answer through SSM in time.")


def hand_over(name, script, args, follow_only=False, interval=3, host=None):
    """From the Mac: run scripts/<script> --here <args> on the host as the unit
    release-demo-<name>, print its log until it ends, and return its exit code.
    If that unit is already running, follow it instead of starting another."""
    host = host or Host()
    unit, log = f"release-demo-{name}", f"/var/log/release-demo-{name}.log"
    try:
        if follow_only:
            code, out, _ = host.shell(f"echo \"$(systemctl is-active {unit} || true) $(stat -c %s {log} 2>/dev/null || echo 0)\"")
            state, size = out.split()[-2:]
            print(f"{script} on the tools host: {'running' if state == 'active' else 'not running'}. Its log:")
            return follow(host, unit, log, max(0, int(size) - 4000), skip_partial=True, interval=interval)
        inner = " ".join(shlex.quote(part) for part in [PYTHON, "-u", f"scripts/{script}", "--here", *args])
        title = re.sub(r"[^\w .-]", "", " ".join([script, *args]))
        code, out, err = host.shell(START.format(repo=REPO_DIR, unit=unit, log=log, title=title,
                                                 command=shlex.quote(f'{inner}; echo "{EXIT}$?"')))
        if code != 0 or not out.strip():
            fail(f"The tools host didn't start {script}: {(err or out).strip()[:300]}")
        *notes, last = out.strip().splitlines()
        for note in notes:
            print(note)
        state, size = last.split()
        if state == "running":
            print(f"{script} is already running on the tools host: following it.")
            return follow(host, unit, log, max(0, int(size) - 4000), skip_partial=True, interval=interval)
        print(f"Started {script} on the tools host ({host.id}); its log is {log} there.")
        return follow(host, unit, log, int(size), interval=interval)
    except KeyboardInterrupt:
        print(f"\nStopped following. {script} carries on on the tools host: "
              f"python scripts/{script} --follow shows it again.")
        return 130


def follow(host, unit, log, offset, skip_partial=False, interval=3):
    """Print the log from offset until the unit's exit line; return that exit code.
    skip_partial drops the first line, which an offset inside the log cuts."""
    skip_partial = skip_partial and offset > 0
    while True:
        _, out, _ = host.shell(FOLLOW.format(unit=unit, log=log, offset=offset, start=offset + 1, chunk=CHUNK))
        head, _, content = out.partition("\n")
        if len(head.split()) != 2:
            time.sleep(interval)  # SSM hiccup: ask again
            continue
        state, count = head.split()
        offset += int(count)
        if skip_partial and content:
            content = content.partition("\n")[2]
            skip_partial = False
        code = None
        for line in content.splitlines(keepends=True):
            if line.startswith(EXIT):
                code = int(line[len(EXIT):].strip() or 1)
            else:
                sys.stdout.write(line)
        sys.stdout.flush()
        if code is not None:
            return code
        if state not in ("active", "activating") and int(count) == 0:
            print(f"{unit} isn't running and its log has no exit line: it was stopped. See {log} on the host.")
            return 1
        if int(count) < CHUNK:
            time.sleep(interval)


def stage(service_name, version):
    """Put a demo state on main with stage.py, from a fresh clone of main,
    pushing with the GitHub App's token. Prints stage.py's output."""
    token, repository, _ = github_app_token(ssm_client())
    env = {**os.environ, **IDENTITY, "STAGE_TOKEN": token, "GIT_TERMINAL_PROMPT": "0"}
    with tempfile.TemporaryDirectory(prefix="release-demo-stage-") as temp:
        clone = Path(temp) / "release-demo"
        for command in (["git", "clone", "--quiet", "--depth", "1", "--branch", "main",
                         f"https://github.com/{repository}.git", str(clone)],
                        ["git", "-C", str(clone), "config", "credential.helper", CREDENTIALS]):
            result = subprocess.run(command, env=env, capture_output=True, text=True)
            if result.returncode != 0:
                fail(f"{' '.join(command[:2])} failed: {result.stderr.strip()}")
        result = subprocess.run([sys.executable, "scripts/stage.py", service_name, version], cwd=clone, env=env,
                                capture_output=True, text=True)
        for line in (result.stdout + result.stderr).strip().splitlines():
            print(f"  {line}")
        if result.returncode != 0:
            fail(f"stage.py {service_name} {version} failed.")


def main_version(service_path):
    """The VERSION of a service folder on main, through the GitHub API."""
    token, repository, _ = github_app_token(ssm_client())
    reply = github_api("GET", f"/repos/{repository}/contents/{service_path}/VERSION?ref=main", token, scheme="token")
    return base64.b64decode(reply["content"]).decode().strip()
