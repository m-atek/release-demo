"""Stop the cluster host after 30 idle minutes. Run every 5 minutes by a timer.

    idle.py

The tools host's idle stop (tools-host/idle_stop.py), with this host's signs
of use: a connection to Argo CD's NodePort, which the UI holds open while a
page shows the application and the pipeline uses while it syncs; an SSM
session, such as break_glass.py's tunnel to the Kubernetes API; or a drift
the recorder sent in the last 30 minutes. The host powers off; its shutdown
behaviour is "stop", so the cluster and Argo CD keep their state.
"""

import json
import subprocess
import time
from pathlib import Path

STATE = Path("/var/lib/release-demo/idle.json")
RECORDER_STATE = Path("/var/lib/release-demo/recorder-last.json")
CONNTRACK = Path("/proc/net/nf_conntrack")
IDLE_SECONDS = 30 * 60
ARGOCD_PORT = 30080


def argocd_connections(text=None):
    """Connections to Argo CD's NodePort that are open now or closed within the
    last two minutes, from the kernel's connection tracking."""
    if text is None:
        try:
            text = CONNTRACK.read_text()
        except OSError:
            return 0
    return sum(1 for line in text.splitlines() if f"dport={ARGOCD_PORT} " in line and "src=127." not in line)


def ssm_sessions():
    found = subprocess.run(["pgrep", "-f", "ssm-session-worker"], capture_output=True, text=True).stdout.split()
    return len(found)


def recorder_busy(now):
    try:
        last = json.loads(RECORDER_STATE.read_text())["at"]
    except (OSError, ValueError, KeyError):
        return False
    return now - last < IDLE_SECONDS


def main():
    now = time.time()
    state = json.loads(STATE.read_text()) if STATE.exists() else {"last_active": now}
    # Idle time counts from this boot at the earliest, as on the tools host.
    booted = now - float(Path("/proc/uptime").read_text().split()[0])
    state["last_active"] = max(state["last_active"], booted)
    signs = {"argocd": argocd_connections(), "ssm": ssm_sessions(), "recorder": recorder_busy(now)}
    if any(signs.values()):
        state["last_active"] = now
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state))

    idle = now - state["last_active"]
    print(f"idle for {idle / 60:.0f} minutes ({', '.join(f'{k} {v}' for k, v in signs.items())})")
    if idle >= IDLE_SECONDS:
        print("30 minutes idle: stopping the cluster host")
        subprocess.run(["systemctl", "poweroff"], check=False)


if __name__ == "__main__":
    main()
