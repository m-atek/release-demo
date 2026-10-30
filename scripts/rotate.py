"""Rotate one of the platform's secrets, with the real clock running.

    python scripts/rotate.py jcr --reason "pasted in Slack"
    python scripts/rotate.py sonar --reason "yearly" [--old-name "the token you made by hand"]
    python scripts/rotate.py jcr --reason "..." --here      where you are: the tools host, or the Jenkins job rotate

One rotator per secret, in scripts/rotators/. Every step prints one line
of at most 52 columns, "HH:MM:SS  <step>  <what>" in Europe/Amsterdam time,
and tokens appear only as the first 8 characters of their id. The JFrog
rotator's last line is "Leak to useless: Xm YYs".

From the Mac it hands itself to the tools host, as reset.py does: the host
runs it as the systemd unit release-demo-rotate, which writes
/var/log/release-demo-rotate.log, and the Mac prints that log as it grows,
through a Session Manager session, so each line arrives within a second of
when it happened. Nothing else is printed. Ctrl-C stops the printing, not
the rotation.

Nothing scans Slack: a person starts this.
"""

import argparse
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import host
from rotators import Failed, Steps, rotator

UNIT, LOG = "release-demo-rotate", "/var/log/release-demo-rotate.log"
SECRETS_DIR = Path("/run/release-demo/secrets")
HOST_SECRETS = [host.PYTHON, f"{host.REPO_DIR}/tools-host/host_secrets.py", str(SECRETS_DIR)]
SESSION_NOISE = ("Starting session with SessionId", "Exiting session with sessionId", "Cannot perform start session")


def refresh_jenkins(ssm):
    """host_secrets.py rewrites the tmpfs from SSM; then Jenkins reloads its configuration as code,
    so its credentials hold the new values. As root under systemd, or through the one sudo rule
    boot.sh gives the Jenkins agent for exactly this command."""
    command = HOST_SECRETS if os.geteuid() == 0 else ["sudo", "-n", *HOST_SECRETS]
    subprocess.run(command, check=True, capture_output=True)
    from common import get_parameter
    from jcr_setup import JENKINS, Client

    jenkins = Client(JENKINS, "admin", get_parameter(ssm, "jenkins/admin-password"))
    status, crumb = jenkins.call("GET", "/crumbIssuer/api/json")
    if status != 200:
        raise Failed(f"Jenkins didn't answer ({status}): its credential still holds the old value")
    status, _ = jenkins.call("POST", "/configuration-as-code/reload",
                             headers={crumb["crumbRequestField"]: crumb["crumb"]})
    if status not in (200, 302):
        raise Failed(f"Jenkins didn't reload its configuration ({status})")


def rotate(secret, reason, old_name=None, steps=None):
    steps = steps or Steps()
    try:
        return rotator(secret).run(steps, reason, old_name=old_name)
    except Failed as error:
        steps.last(f"FAIL: {error}")
        return 1


# From the Mac

def start_on_host(the_host, args):
    """Starts the unit unless it runs already. Returns (state, log size before this run)."""
    inner = " ".join(shlex.quote(part) for part in [host.PYTHON, "-u", "scripts/rotate.py", "--here", *args])
    title = " ".join(["rotate.py", *args[:1]])
    code, out, err = the_host.shell(host.START.format(repo=host.REPO_DIR, unit=UNIT, log=LOG, title=title,
                                                      command=shlex.quote(f'{inner}; echo "{host.EXIT}$?"')))
    if code != 0 or not out.strip():
        raise SystemExit(f"The tools host didn't start rotate.py: {(err or out).strip()[:300]}")
    state, size = out.strip().splitlines()[-1].split()
    return state, int(size)


def stream(the_host, offset, popen=subprocess.Popen):
    """Prints the log from offset as it grows, through Session Manager, until the run's exit line."""
    command = f"tail -c +{offset + 1} -F {LOG}"
    process = popen(["aws", "ssm", "start-session", "--target", the_host.id,
                     "--document-name", "AWS-StartNonInteractiveCommand",
                     "--parameters", json.dumps({"command": [command]})],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
    try:
        for raw in process.stdout:
            line = raw.rstrip("\r\n")
            if line.startswith(host.EXIT):
                return int(line[len(host.EXIT):].strip() or 1)
            if line.startswith("== ") or line.startswith(SESSION_NOISE) or not line.strip():
                continue
            print(line, flush=True)
    finally:
        process.terminate()
    return None


def from_mac(args, the_host=None):
    the_host = the_host or host.Host()
    try:
        state, size = start_on_host(the_host, args)
        offset = max(0, size - 4000) if state == "running" else size
        code = stream(the_host, offset)
        if code is None:  # no Session Manager plugin, or the session ended: poll instead
            code = host.follow(the_host, UNIT, LOG, offset, interval=1)
        return code
    except KeyboardInterrupt:
        print("\nStopped following. The rotation carries on on the tools host.")
        return 130


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("secret", choices=["jcr", "sonar"])
    parser.add_argument("--reason", required=True, help="why, for the incident: pasted in Slack")
    parser.add_argument("--old-name", help="sonar: the name of the token made by hand, the first time")
    parser.add_argument("--here", action="store_true", help="run here, not on the tools host")
    args = parser.parse_args()
    if not args.here and not host.on_host():
        forwarded = [args.secret, "--reason", args.reason, *(["--old-name", args.old_name] if args.old_name else [])]
        return from_mac(forwarded)
    sys.stdout.reconfigure(line_buffering=True)
    return rotate(args.secret, args.reason, args.old_name)


if __name__ == "__main__":
    sys.exit(main())
