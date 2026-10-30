"""Stop the tools host after 30 idle minutes. Run every 5 minutes by a timer.

    idle_stop.py /run/release-demo/secrets

Idle means: no running or queued Jenkins build, no job on the GitLab runner
(demo 17), no job on the Azure Pipelines agent and no release run in progress
(demo 22), and cloudflared's request counter unchanged, so nobody used
Jenkins, JCR or Splunk through the tunnel. The host powers off; its shutdown
behaviour is "stop", so the data stays.
"""

import base64
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

STATE = Path("/var/lib/release-demo/idle.json")
IDLE_SECONDS = 30 * 60


def jenkins_busy(secrets):
    password = (secrets / "jenkins-admin-password").read_text().strip()
    auth = {"Authorization": "Basic " + base64.b64encode(f"admin:{password}".encode()).decode()}

    def get(path):
        with urllib.request.urlopen(urllib.request.Request("http://localhost:8080" + path, headers=auth), timeout=10) as r:
            return json.loads(r.read())

    try:
        busy = get("/computer/api/json?tree=busyExecutors")["busyExecutors"]
        queued = len(get("/queue/api/json?tree=items%5Bid%5D")["items"])
        return busy > 0 or queued > 0
    except Exception:
        return False  # Jenkins down counts as idle: a broken host shouldn't run all night


def gitlab_busy():
    """A job on the GitLab runner, from its metrics on localhost: the Approval
    job waiting for Alex keeps the host up. No runner running counts as idle."""
    try:
        with urllib.request.urlopen("http://127.0.0.1:9252/metrics", timeout=5) as response:
            lines = response.read().decode().splitlines()
    except Exception:
        return False
    return sum(float(line.rsplit(" ", 1)[1]) for line in lines if line.startswith("gitlab_runner_jobs{")) > 0


def azure_busy():
    """A job on the Azure Pipelines agent, or a release run in progress: a
    stage waiting on its check runs nothing on the agent, so Azure DevOps is
    asked, as Release Bot. Only while the agent runs, that is, while demo 22
    is up; a question that fails counts as idle."""
    if subprocess.run(["pgrep", "-f", "Agent.Worker"], capture_output=True).returncode == 0:
        return True
    if subprocess.run(["systemctl", "is-active", "--quiet", "release-demo-azure-agent"]).returncode != 0:
        return False
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "demos" / "azure_devops"))
        import release_bot

        return release_bot.run_in_progress()
    except (Exception, SystemExit) as error:
        print(f"Azure DevOps not asked: {error.__class__.__name__}")
        return False


def tunnel_requests():
    try:
        with urllib.request.urlopen("http://127.0.0.1:2000/metrics", timeout=5) as response:
            lines = response.read().decode().splitlines()
    except Exception:
        return None
    return sum(float(line.rsplit(" ", 1)[1]) for line in lines
               if line.startswith("cloudflared_tunnel_total_requests"))


def main():
    secrets = Path(sys.argv[1])
    now = time.time()
    state = json.loads(STATE.read_text()) if STATE.exists() else {"last_active": now, "requests": None}
    # Idle time counts from this boot at the earliest: a state file from before
    # a stop would otherwise power the host off 10 minutes after every start.
    booted = now - float(Path("/proc/uptime").read_text().split()[0])
    state["last_active"] = max(state["last_active"], booted)

    requests = tunnel_requests()
    if jenkins_busy(secrets) or gitlab_busy() or azure_busy() or requests != state["requests"]:
        state["last_active"] = now
    state["requests"] = requests
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state))

    idle = now - state["last_active"]
    print(f"idle for {idle / 60:.0f} minutes")
    if idle >= IDLE_SECONDS:
        print("30 minutes idle: stopping the tools host")
        subprocess.run(["systemctl", "poweroff"], check=False)


if __name__ == "__main__":
    main()
