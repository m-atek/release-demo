"""Stop the tools host after 30 idle minutes. Run every 5 minutes by a timer.

    idle_stop.py /run/release-demo/secrets

Idle means: no running or queued Jenkins build (a timer's own check for work,
such as pull-requests, doesn't count), and cloudflared's request
counter unchanged, so nobody used Jenkins, JCR or Splunk through the tunnel.
The host powers off; its shutdown behaviour is "stop", so the data stays.
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
# Jobs a timer starts every minute, whose builds alone aren't anyone using the
# host: video 16's pull-requests looks for a pull request to check, and mostly ends at once.
NOT_ACTIVITY = {"pull-requests"}


def jenkins_busy(secrets):
    password = (secrets / "jenkins-admin-password").read_text().strip()
    auth = {"Authorization": "Basic " + base64.b64encode(f"admin:{password}".encode()).decode()}

    def get(path):
        with urllib.request.urlopen(urllib.request.Request("http://localhost:8080" + path, headers=auth), timeout=10) as r:
            return json.loads(r.read())

    def ours(url):
        return any(f"/job/{job}/" in (url or "") for job in NOT_ACTIVITY)

    try:
        computers = get("/computer/api/json?tree=computer%5Bexecutors%5BcurrentExecutable%5Burl%5D%5D,"
                        "oneOffExecutors%5BcurrentExecutable%5Burl%5D%5D%5D")["computer"]
        running = [e["currentExecutable"]["url"] for c in computers
                   for e in (c.get("executors") or []) + (c.get("oneOffExecutors") or []) if e.get("currentExecutable")]
        queued = get("/queue/api/json?tree=items%5Btask%5Burl%5D%5D")["items"]
        return any(not ours(url) for url in running) or any(not ours(i.get("task", {}).get("url")) for i in queued)
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
        subprocess.run(["systemctl", "poweroff"], check=False)


if __name__ == "__main__":
    main()
