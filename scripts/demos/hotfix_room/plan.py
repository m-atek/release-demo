"""The Plan stage of forecast's release: the numbers Alex decides on, read from the node.

    python scripts/demos/hotfix_room/plan.py forecast

Runs on the tools host agent, with the build's 10-minute token, before the change
opens. Reserved comes from the pods' own requests, used and peak from their cgroups, what this
release asks for from the chart it is about to deploy, and the last line from the
scheduler's own event in staging, if it refused a pod in the last hour. The lines go
in the change's description (change_details), one short row in Slack (rows). The
role may read pods and events in staging, production and this namespace, and no
cgroup needs a right: they are world-readable. A reading that fails says so on the
change, and never stops the release: Alex then decides without that number.
"""

import datetime
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(1, str(HERE.parent / "kubernetes"))
sys.path.append(str(HERE.parents[1]))

import kube  # noqa: E402
import room  # noqa: E402
import run_state  # noqa: E402
from common import service  # noqa: E402

CGROUPS = Path("/sys/fs/cgroup")
AMSTERDAM = ZoneInfo("Europe/Amsterdam")
WITHIN_MINUTES = 60


def pod_cgroup(uid, root=None):
    """A pod's cgroup folder, under the systemd or the cgroupfs driver, Burstable or Guaranteed."""
    root = root or CGROUPS
    for name in (uid.replace("-", "_"), uid):
        for pattern in (f"kubepods*/*pod{name}*", f"kubepods*/*/*pod{name}*"):
            found = sorted(root.glob(pattern))
            if found:
                return found[0]
    return None


def memory_of(pod, root=None):
    """(used now, peak since it started), in bytes. Peak is used now where the kernel has no memory.peak."""
    folder = pod_cgroup(pod["metadata"]["uid"], root)
    if folder is None:
        raise FileNotFoundError(f"no cgroup for {pod['metadata']['name']}")
    current = int((folder / "memory.current").read_text())
    peak_file = folder / "memory.peak"
    return current, int(peak_file.read_text()) if peak_file.exists() else current


def moment(text):
    return datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))


def minutes_since(text, now):
    return int((now - moment(text)).total_seconds() // 60)


def scheduler_line(api, namespace, now):
    """The scheduler's last refusal in that namespace, in its own words, if it is recent enough to be why."""
    reply = api.get(f"/api/v1/namespaces/{namespace}/events?fieldSelector=reason%3DFailedScheduling") or {}
    recent = [event for event in reply.get("items", [])
              if minutes_since(event["metadata"]["creationTimestamp"], now) <= WITHIN_MINUTES]
    if not recent:
        return None
    newest = max(recent, key=lambda event: event["metadata"]["creationTimestamp"])
    said = newest.get("message", "").split(". ")[0].rstrip(".") + "."
    at = moment(newest["metadata"]["creationTimestamp"]).astimezone(AMSTERDAM).strftime("%H:%M")
    return f"The scheduler, in {namespace} at {at}: {said}"


def asked_by_chart(config, replicas):
    """What this release will reserve: the chart it deploys, times its replicas."""
    values = yaml.safe_load((Path(config["dir"]) / "chart" / "values.yaml").read_text())
    return room.quantity(values["resources"]["requests"]["memory"]) * replicas


def lines_for(namespace, pods, asks, replicas, now, memory=None):
    """(lines for the change, one row for Slack). Pure but for the cgroups it reads."""
    memory = memory or memory_of
    at = now.astimezone(AMSTERDAM).strftime("%H:%M")
    reserved = sum(room.request_of(pod["spec"]) for pod in pods)
    head = f"Reservation in {namespace}, read from the node at {at}:"
    gives = f"This release reserves {room.size(asks)} in {replicas} pods and gives back {room.size(reserved - asks)}."
    try:
        seen = [memory(pod) for pod in pods]
    except (OSError, ValueError) as error:
        return ([head, f"Reserved {room.size(reserved)} in {len(pods)} pods. Used: not read ({error}).", gives],
                f"Reserved {room.size(reserved)}. Asks for {room.size(asks)}.")
    used, peak = sum(item[0] for item in seen), sum(item[1] for item in seen)
    age = min(minutes_since(pod["status"]["startTime"], now) for pod in pods)
    return ([head, f"Reserved {room.size(reserved)} in {len(pods)} pods. Used {room.size_used(used)} now, "
                   f"{room.size_used(peak)} at its peak since these pods started, {age} minutes ago.", gives],
            f"Reserved {room.size(reserved)}, used {room.size_used(used)}. Asks for {room.size(asks)}.")


def main():
    name = sys.argv[1]
    config = service(name)
    environment = config["environments"]["production"]
    namespace, replicas = environment["namespace"], environment["replicas"]
    now = datetime.datetime.now(datetime.timezone.utc)
    state = run_state.load()
    try:
        with kube.credentials(quiet=True) as kubeconfig:
            api = kube.Api(kubeconfig)
            pods = [pod for pod in api.pods(namespace, config["kubernetes"]["release"])
                    if not pod["metadata"].get("deletionTimestamp")]
            refused = scheduler_line(api, room.STAGING, now)
        if not pods:
            raise RuntimeError(f"no {name} pods in {namespace}")
        details, row = lines_for(namespace, pods, asked_by_chart(config, replicas), replicas, now)
        if refused:
            details.append(refused)
    except (Exception, SystemExit) as error:  # the numbers help Alex decide; without them he still can
        details, row = [f"Reservation in {namespace}: not read ({error.__class__.__name__}: {error})."], "Reservation not read."
    for line in details:
        print(line)
    state["change_details"] = details
    state["rows"] = [row]
    run_state.save(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
