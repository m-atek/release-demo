"""scripts/demo.sh up, down and status for demo 22, Azure DevOps. From the Mac.

    python scripts/demos/azure_devops/demo.py up        the agent on, then /release on Azure Pipelines
    python scripts/demos/azure_devops/demo.py down      /release back on Jenkins, then the agent off
    python scripts/demos/azure_devops/demo.py status    the switch and the agent

up runs tools-host/azure-agent.sh on the tools host (it installs and
registers the agent once, as boot.sh does), starts the agent, waits until it
listens for jobs, and only then sets ci/orders-service to azure-pipelines.
down sets it back to jenkins first, so no /release reaches an agent that is
stopping, then stops the agent. The tools host must be running: scripts/tools.sh up.
"""

import argparse
import sys
import time

from release_bot import SSM_PREFIX, SWITCH, UNIT, fail, put_parameter, ssm_client

import host  # noqa: E402  (scripts/, which release_bot puts on the path)


def shell(script, timeout=60):
    code, out, err = host.Host().shell(script, timeout=timeout)
    return code, (out or err).strip()


def listening():
    _, out = shell(f"journalctl -u {UNIT} -n 50 --no-pager -o cat | grep -c 'Listening for Jobs' || true")
    return out.splitlines()[-1].strip() not in ("", "0") if out else False


def switch(ssm, value=None):
    if value:
        put_parameter(ssm, SWITCH, value, secure=False)
        return value
    current = ssm.get_parameter(Name=SSM_PREFIX + SWITCH)["Parameter"]["Value"]
    return "jenkins" if current == "unset" else current


def up():
    code, out = shell(f"cd {host.REPO_DIR} && (git pull --ff-only -q || true) && {host.REPO_DIR}/tools-host/azure-agent.sh",
                      timeout=600)
    if code != 0:
        fail(f"azure-agent.sh failed on the tools host: {out[-300:]}")
    shell(f"systemctl start {UNIT}")
    deadline = time.monotonic() + 120
    while not listening():
        if time.monotonic() > deadline:
            fail(f"The agent doesn't listen for jobs after two minutes. On the host: journalctl -u {UNIT}")
        time.sleep(5)
    print("agent tools-host: listening for jobs")
    print(f"/release orders-service now runs on: {switch(ssm_client(), 'azure-pipelines')}")
    print("Ready")
    return 0


def down():
    print(f"/release orders-service now runs on: {switch(ssm_client(), 'jenkins')}")
    _, out = shell(f"systemctl stop {UNIT}; systemctl is-active {UNIT} || true")
    print(f"agent tools-host: {out.splitlines()[-1] if out else 'stopped'}")
    return 0


def status():
    print(f"/release orders-service runs on: {switch(ssm_client())}")
    try:
        _, out = shell(f"systemctl is-active {UNIT} || true")
        print(f"agent tools-host: {out.splitlines()[-1] if out else 'unknown'}")
    except SystemExit:
        print("agent tools-host: the tools host is stopped")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=["up", "down", "status"])
    args = parser.parse_args()
    return {"up": up, "down": down, "status": status}[args.action]()


if __name__ == "__main__":
    sys.exit(main())
