"""How much of the node is reserved, how much is used, and what the scheduler said. Video 33's bottom window.

    .venv/bin/python scripts/demos/hotfix_room/room.py           once
    .venv/bin/python scripts/demos/hotfix_room/room.py --watch   redrawn in place every 2 seconds, for the take

Real readings, through kubectl, with connect.py's tunnels and certificate on the Mac:

- what the node can hand out is the node's own Allocatable, the figure the
  scheduler uses. On this host it is 1,180 MiB, not the 1,280 MiB k3s.py holds the
  pods to: the kubelet takes its 100 MiB eviction threshold off Allocatable too;
- reserved is the memory requests of the pods on the node, as kubectl describe
  node adds them up, with the percent it prints: rounded down;
- used is the kubelet's own reading through the API server, the pods' cgroup, the
  figure k3s.py's memory() reads, so page cache included. metrics-server stays off.
  Its percent is of the same 1,180 MiB, rounded up. Nothing here adds Jenkins,
  JFrog or Splunk, which share the machine and aren't pods.

The scheduler's words are only ever its own, with line breaks added, and only for
a pod in staging that is still Pending. Every line fits 44 columns.
"""

import argparse
import json
import math
import os
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path

NODE = "tools-host"
FORECAST = "forecast"       # the namespace of the second team's service
STAGING = "staging"         # where the hotfix goes first
CATALOG = "catalog"
WIDTH = 44
MIB = 1 << 20
SUFFIXES = {"Ki": 1 << 10, "Mi": 1 << 20, "Gi": 1 << 30, "Ti": 1 << 40, "k": 10 ** 3, "M": 10 ** 6, "G": 10 ** 9, "T": 10 ** 12}
SUMMARY = f"/api/v1/nodes/{NODE}/proxy/stats/summary"
PRESENTER = Path.home() / ".kube" / "release-demo" / "config"


def quantity(text):
    """Bytes from a Kubernetes quantity: 1208123Ki, 48Mi, 128974848, 0."""
    text = str(text or "0")
    for suffix, factor in SUFFIXES.items():
        if text.endswith(suffix):
            return float(text[:-len(suffix)]) * factor
    if text.endswith("m"):
        return float(text[:-1]) / 1000
    return float(text)


def request_of(pod_spec):
    """The memory a pod asks for, as the scheduler adds it up: its containers, or its largest
    init container if that is more, plus any overhead."""
    def of(container):
        return quantity(container.get("resources", {}).get("requests", {}).get("memory"))
    containers = sum(of(container) for container in pod_spec.get("containers", []))
    first = max((of(container) for container in pod_spec.get("initContainers", [])), default=0)
    return max(containers, first) + quantity(pod_spec.get("overhead", {}).get("memory"))


def on_the_node(pods):
    """The pods the node counts: placed on it and not finished, as kubectl describe node counts them."""
    return [pod for pod in pods if pod.get("spec", {}).get("nodeName") == NODE
            and pod.get("status", {}).get("phase") not in ("Succeeded", "Failed")]


@dataclass
class Reading:
    """Bytes, as read."""
    allocatable: float
    reserved: float
    used: float
    forecast_reserved: float
    forecast_used: float
    ask: float
    pending: str = ""       # the pod the scheduler refused, if it is still waiting
    said: str = ""          # what the scheduler said about it, whole

    @property
    def free(self):
        return self.allocatable - self.reserved

    @property
    def fits(self):
        return self.ask <= self.free

    @property
    def reserved_percent(self):
        return int(self.reserved * 100 / self.allocatable)

    @property
    def used_percent(self):
        return math.ceil(self.used * 100 / self.allocatable)


def used_by_pods(summary):
    """The pods' cgroup, from the kubelet's summary; the sum of the pods if it names none."""
    for container in summary.get("node", {}).get("systemContainers", []):
        if container.get("name") == "pods" and "usageBytes" in container.get("memory", {}):
            return float(container["memory"]["usageBytes"])
    return sum(float(pod.get("memory", {}).get("usageBytes", 0)) for pod in summary.get("pods", []))


def used_in(summary, namespace):
    return sum(float(pod.get("memory", {}).get("usageBytes", 0)) for pod in summary.get("pods", [])
               if pod.get("podRef", {}).get("namespace") == namespace)


def still_refused(events, pods):
    """(pod, what the scheduler said) for the newest FailedScheduling of a pod still Pending in this namespace."""
    waiting = {pod["metadata"]["name"] for pod in pods if pod.get("status", {}).get("phase") == "Pending"
               and pod.get("metadata", {}).get("namespace") == STAGING}
    found = [event for event in events if event.get("involvedObject", {}).get("name") in waiting]
    if not found:
        return "", ""
    newest = max(found, key=lambda event: event.get("metadata", {}).get("creationTimestamp", ""))
    return newest["involvedObject"]["name"], newest.get("message", "")


def read(get):
    """The reading. get(*kubectl arguments) answers parsed JSON."""
    node = get("get", "node", NODE, "-o", "json")
    pods = get("get", "pods", "-A", "-o", "json")["items"]
    summary = get("get", "--raw", SUMMARY)
    counted = on_the_node(pods)
    events = get("get", "events", "-n", STAGING, "--field-selector", "reason=FailedScheduling", "-o", "json")["items"]
    pending, said = still_refused(events, pods)
    template = get("get", "deployment", CATALOG, "-n", STAGING, "-o", "json")["spec"]["template"]["spec"]
    return Reading(allocatable=quantity(node["status"]["allocatable"]["memory"]),
                   reserved=sum(request_of(pod["spec"]) for pod in counted),
                   used=used_by_pods(summary),
                   forecast_reserved=sum(request_of(pod["spec"]) for pod in counted
                                         if pod["metadata"]["namespace"] == FORECAST),
                   forecast_used=used_in(summary, FORECAST),
                   ask=request_of(template), pending=pending, said=said)


def wrapped(said, width=WIDTH):
    """The scheduler's message, its words unchanged, with line breaks at its sentences and where they read best."""
    lines = []
    for sentence in said.replace("nodes are available: ", "nodes are available:\n", 1).replace(". ", ".\n").splitlines():
        lines += textwrap.wrap(sentence, width) or [""]
    return lines


def row(label, value, percent=""):
    return f"{label:<27}{percent:>3}{value:>13}"


def size(number):
    """MiB, whole, with a thousands separator, rounded down: free is never overstated."""
    return f"{int(number // MIB):,} MiB"


def size_used(number):
    """MiB used, rounded up: use is never understated."""
    return f"{math.ceil(number / MIB):,} MiB"


def frame(reading):
    """The lines on screen. The figures keep their rows whether or not a pod is waiting, so an outline doesn't move."""
    if reading.pending:
        head = [f"Scheduler on {reading.pending}:", *wrapped(reading.said)]
    else:
        head = ["Scheduler: nothing waiting."]
    head += [""] * (5 - len(head))
    return [*head, "",
            row("The node can hand out", size(reading.allocatable)),
            row("Reserved", size(reading.reserved), f"{reading.reserved_percent}%"),
            row("Used", size_used(reading.used), f"{reading.used_percent}%"),
            "",
            row(f"{FORECAST} reserved", size(reading.forecast_reserved)),
            row(f"{FORECAST} uses", size_used(reading.forecast_used)),
            "",
            row("Free to reserve", size(reading.free)),
            row(f"{CATALOG}'s next pod asks for", size(reading.ask))]


def kubectl_json(*args):
    """kubectl with the presenter's certificate on the Mac, or whatever KUBECONFIG says here."""
    env = dict(os.environ)
    if not env.get("KUBECONFIG") and PRESENTER.exists():
        env["KUBECONFIG"] = str(PRESENTER)
    result = subprocess.run(["kubectl", *args], capture_output=True, text=True, timeout=20, env=env)
    if result.returncode != 0:
        raise RuntimeError((result.stderr.strip() or f"kubectl {args[1]} failed").splitlines()[0])
    return json.loads(result.stdout)


def clip(line):
    return line[:WIDTH + 12]


def watch(get=kubectl_json, seconds=2):
    """In place, as pods.sh does, with no scrolling, until Ctrl-C."""
    sys.stdout.write("\033[2J\033[?25l")
    try:
        while True:
            try:
                lines = frame(read(get))
            except (RuntimeError, KeyError, ValueError, subprocess.TimeoutExpired) as error:
                lines = [clip(f"No reading: {error}")]
            sys.stdout.write("\033[H" + "".join(f"{line}\033[K\n" for line in lines) + "\033[J")
            sys.stdout.flush()
            time.sleep(seconds)
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write("\033[?25h\n")
        sys.stdout.flush()


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--watch", action="store_true", help="redraw every 2 seconds, until Ctrl-C")
    args = parser.parse_args()
    if args.watch:
        watch()
        return 0
    print("\n".join(frame(read(kubectl_json))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
