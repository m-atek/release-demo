"""Stop the tools host after 30 idle minutes. Run every 5 minutes by a timer.

    idle_stop.py /run/release-demo/secrets

Idle means: no running or queued Jenkins build, and cloudflared's request
counter unchanged, so nobody used Jenkins, JCR or Splunk through the tunnel.
Before it powers off, each demo's scripts/demos/<demo>/on_idle.sh runs. The
host powers off; its shutdown behaviour is "stop", so the data stays.
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
    if jenkins_busy(secrets) or requests != state["requests"]:
        state["last_active"] = now
    state["requests"] = requests
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state))

    idle = now - state["last_active"]
    print(f"idle for {idle / 60:.0f} minutes")
    if idle >= IDLE_SECONDS:
        print("30 minutes idle: stopping the tools host")
        before_poweroff()
        subprocess.run(["systemctl", "poweroff"], check=False)


def before_poweroff():
    """Each demo's on_idle.sh, if it has one: what must not run on once the host is off
    (demo 23: its search endpoint, which bills by the hour). Never stops the power-off."""
    for hook in sorted(Path(__file__).resolve().parents[1].glob("scripts/demos/*/on_idle.sh")):
        try:
            result = subprocess.run([str(hook)], capture_output=True, text=True, timeout=360)
            print(f"{hook.parent.name}: {(result.stdout + result.stderr).strip()[-500:]}")
        except (OSError, subprocess.TimeoutExpired) as error:
            print(f"{hook.parent.name}: {error.__class__.__name__}")


if __name__ == "__main__":
    main()
