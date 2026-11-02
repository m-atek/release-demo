"""The Blip check (video 25), after Check staging: what does one database blip do to the service?

    python scripts/demos/database-blip/blip_check.py catalog
    python scripts/demos/database-blip/blip_check.py catalog --rehearsal     no Slack, Jira or change (rehearse.py)

On the tools host agent, with the build's 10-minute token:

1. Deletes any block-database left behind, waits until every pod is ready and
   notes each pod's restart count.
2. Calls the data route and the alive route 10 times a second each, through the
   Service's NodePort, each call on a new connection with a 1 s time-out. Each
   counts as 200, 503, refused or timeout. 5 s of that as a baseline.
3. Applies block.yaml. Zero is the first failed call to the data route, not the
   apply: a 30 s window holds exactly three ticks of a 10 s liveness probe only if
   it lasts all 30 s. No failed call within 5 s: the block didn't take.
4. At zero plus 30.0 s deletes the policy, in a finally, so an error can't leave it on.
5. Each second reads the pods and their events, and prints what changed.
6. Ends when every pod is ready and 5 s have passed with no failed call, or at 90 s.
7. Passes only if no pod restarted, the alive route never failed, every failure
   of the data route inside the block was a 503 with no data in it, and calls
   succeed within 5 s of the delete.

One line a second goes to the build's log and to /var/tmp/release-demo-blip.log,
which the video's bottom window follows. A line's TIME is when it prints, so it
reads what the recording's timecode reads: "0:12" counts the second before it.
"Back" is the first such line from which no call fails again.

.release/blip.json holds every call, the pods' restart counts before and after,
and the times of the apply, the first failure and the delete. A failed check
comments on the Jira issue. A passed one puts that comment's line and its own
into one work note on the change, with blip.json attached.
"""

import argparse
import collections
import concurrent.futures
import dataclasses
import http.client
import json
import os
import queue
import re
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parents[1]
# After the standard library: scripts/secrets.py would shadow its secrets, as it once did for pg8000 (3a2057e).
sys.path.append(str(SCRIPTS))
sys.path.append(str(SCRIPTS / "demos" / "kubernetes"))  # last: it has its own build, deploy and check

import kube  # noqa: E402
import run_state  # noqa: E402
from common import ROOT, service  # noqa: E402

NAME = "Blip check"  # also in run_state.VERSION_STAGES and releasePipeline.groovy
POLICY = "block-database"
LOG = Path("/var/tmp/release-demo-blip.log")
BASELINE_SECONDS = 5
TAKES_SECONDS = 5  # no failed call this long after the apply: the block didn't take
QUIET_SECONDS = 5  # the end: every pod ready, and no failed call for this long
BACK_WITHIN_SECONDS = 5  # calls succeed within this long of the delete
CALL_TIMEOUT_SECONDS = 1.0
LATE_SECONDS = 0.15  # a second's line waits this long for its last calls to answer
READY_TIMEOUT_SECONDS = 120
WIDTH = 18  # of a line's first route, so no line passes 44 columns
ORDER = {200: 0, 503: 1, "refused": 2, "timeout": 3}
EARLIER = re.compile(rf"^{NAME}: (.+ pods restarted, .+ Release stopped\.)$", re.M)


@dataclasses.dataclass
class Settings:
    service: str
    environment: str
    namespace: str
    release: str
    replicas: int
    port: int
    seconds: int
    calls_per_second: int
    data_route: str
    alive_route: str
    recover_seconds: int


@dataclasses.dataclass
class Call:
    route: str
    started: float
    finished: float
    result: object  # a status code, "refused" or "timeout"
    served_data: bool = False


def settings(name, environment=None):
    config = service(name)
    check = config["blip_check"]
    environment = environment or check["environment"]
    env = config["environments"][environment]
    return Settings(service=name, environment=environment, namespace=env["namespace"],
                    release=config["kubernetes"]["release"], replicas=env["replicas"], port=env["node_port"],
                    seconds=check["seconds"], calls_per_second=check["calls_per_second"],
                    data_route=check["data_route"], alive_route=check["alive_route"],
                    recover_seconds=check["recover_seconds"])


def policies(namespace):
    return f"/apis/networking.k8s.io/v1/namespaces/{namespace}/networkpolicies"


def policy_document(namespace):
    document = yaml.safe_load((HERE / "block.yaml").read_text())
    document["metadata"]["namespace"] = namespace
    return document


def http_call(port, route, timeout=CALL_TIMEOUT_SECONDS):
    """One call on a new connection: (result, whether a 503 carried data)."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request("GET", route)
        response = connection.getresponse()
        return response.status, response.status == 503 and b"products" in response.read()
    except (TimeoutError, socket.timeout):
        return "timeout", False
    except (OSError, http.client.HTTPException):  # refused, reset, no route: the connection didn't answer
        return "refused", False
    finally:
        connection.close()


def cell(counts):
    """A route's calls of one second, in at most WIDTH columns: 200 x10, or 503 x6 refused x4."""
    items = sorted(counts.items(), key=lambda item: (ORDER.get(item[0], 9), str(item[0])))
    full = " ".join(f"{result} x{count}" for result, count in items)
    if len(full) < WIDTH:  # a space between the two routes
        return full or "-"
    short = " ".join(f"{str(result)[:3]}:{count}" for result, count in items)
    return short if len(short) < WIDTH else f"mixed x{sum(counts.values())}"


def row(label, data, alive):
    return f"{label:<5} {data:<{WIDTH}}{alive}".rstrip()


def open_log(path):
    """The file the video's bottom window follows, shared by the agent's user and root.
    Opened without O_CREAT when it exists: Linux's protected_regular refuses O_CREAT
    in /var/tmp on a file someone else made, even to root."""
    try:
        try:
            handle = os.open(path, os.O_WRONLY | os.O_APPEND)
        except FileNotFoundError:
            handle = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o666)
            os.fchmod(handle, 0o666)
        return os.fdopen(handle, "a", buffering=1)
    except OSError as error:
        print(f"{path}: {error.__class__.__name__}. The lines go to this log only.", flush=True)
        return None


class Lines:
    """Each line, as it happens, in the build's log and in the file."""

    def __init__(self, path=LOG, out=None):
        self.file, self.out = open_log(path), out or sys.stdout

    def __call__(self, text):
        print(text, file=self.out, flush=True)
        if self.file:
            self.file.write(text + "\n")


class NotReady(Exception):
    pass


class Check:
    """One block, start to end. The clock, sleep, calls and pool are parameters, so a
    test can run the whole check on a fake cluster in no time."""

    def __init__(self, config, api, version, say, call=http_call, clock=time.monotonic, sleep=time.sleep, wall=time.time,
                 pool=None):
        self.config, self.api, self.version, self.say = config, api, version, say
        self.http, self.clock, self.sleep, self.wall = call, clock, sleep, wall
        self.pool = pool or concurrent.futures.ThreadPoolExecutor(max_workers=32)
        self.path = f"{policies(config.namespace)}/{POLICY}"
        self.answers = queue.SimpleQueue()
        self.calls, self.pending, self.lines = [], [], []
        self.phase = "baseline"
        self.zero = self.applied_at = self.apply_returned = self.delete_at = self.deleted_at = self.zero_wall = None
        self.last_failure = self.give_up_at = self.counts_from = 0.0
        self.problem = None  # "baseline", "no block" or "not back"
        self.removed = self.polling = False
        self.before, self.latest, self.events, self.killed, self.restarted_at = {}, {}, {}, {}, {}
        self.crash_loop, self.was_not_ready = set(), set()
        self.all_not_ready_at = self.final = self.stopped = None
        self.baseline_left = BASELINE_SECONDS
        self.line_close = self.next_line = self.next_tick = self.next_poll = 0.0

    # What the cluster says.

    def read_pods(self):
        """{name: its facts}, the pods that aren't on their way out."""
        found = {}
        for pod in self.api.pods(self.config.namespace, self.config.release):
            if not pod["metadata"].get("deletionTimestamp"):
                status = (pod.get("status", {}).get("containerStatuses") or [{}])[0]
                found[pod["metadata"]["name"]] = {
                    "ready": bool(status.get("ready")), "restarts": status.get("restartCount", 0),
                    "waiting": (status.get("state") or {}).get("waiting", {}).get("reason")}
        return found

    def read_killings(self):
        """{event name: (pod, how many so far)} for Kubernetes' own Killing events after a failed liveness probe."""
        reply = self.api.get(f"/api/v1/namespaces/{self.config.namespace}/events?fieldSelector=reason%3DKilling") or {}
        return {event["metadata"]["name"]: (event["involvedObject"]["name"],
                                            event.get("count") or (event.get("series") or {}).get("count") or 1)
                for event in reply.get("items", []) if "liveness" in event.get("message", "")}

    def prepare(self):
        left_on = self.delete_policy()
        if left_on:
            self.say(f"{POLICY} from an earlier run was still on: deleted.")
        deadline = self.clock() + READY_TIMEOUT_SECONDS
        while True:
            pods = self.read_pods()
            if len(pods) == self.config.replicas and all(pod["ready"] for pod in pods.values()) and (
                    # A policy's end takes a moment to reach the node, and the baseline must start clean.
                    not left_on or self.http(self.config.port, self.config.data_route)[0] == 200):
                break
            if self.clock() >= deadline:
                raise NotReady(f"{sum(pod['ready'] for pod in pods.values())} of {self.config.replicas} pods ready")
            self.sleep(2)
        self.before = {name: pod["restarts"] for name, pod in pods.items()}
        self.latest = pods
        self.events = self.read_killings()

    # The calls.

    def one_call(self, route):
        started = self.clock()
        result, served = self.http(self.config.port, route)
        self.answers.put(Call(route, started, self.clock(), result, served))

    def read_cluster(self):
        try:
            self.answers.put((self.read_pods(), self.read_killings()))
        except Exception as error:  # a missed read never stops the check; the next second reads again
            print(f"(no answer from the cluster: {error.__class__.__name__})", flush=True)
            self.answers.put(None)

    def drain(self):
        while True:
            try:
                answer = self.answers.get_nowait()
            except queue.Empty:
                return
            if isinstance(answer, Call):
                self.on_call(answer)
            else:
                self.polling = False
                if answer:
                    self.observe(*answer)

    def on_call(self, call):
        self.calls.append(call)
        if call.started >= self.counts_from:  # the lines count the ticks after zero; the file holds them all
            self.pending.append(call)
        if call.result == 200:
            return
        self.last_failure = max(self.last_failure, call.finished)
        if self.phase == "baseline":
            self.problem = "baseline"
        elif self.phase == "applied" and call.route == self.config.data_route and call.started >= self.applied_at:
            self.on_zero(call.finished)

    def on_zero(self, at):
        self.zero, self.zero_wall = at, self.wall()
        self.phase = "blocked"
        self.delete_at = at + self.config.seconds
        self.say("0:00  database blocked")
        self.pending = []
        self.line_close, self.next_line = at + 1, at + 1 + LATE_SECONDS
        interval = 1 / self.config.calls_per_second
        self.counts_from = at + interval / 4  # not the other route's call of the tick that found zero
        self.next_tick = at + interval / 2
        while self.next_tick <= self.clock():
            self.next_tick += interval

    def observe(self, pods, killings):
        self.latest = pods
        elapsed = self.clock() - self.zero if self.zero is not None else None
        for name, pod in pods.items():
            if pod["waiting"] == "CrashLoopBackOff":
                self.crash_loop.add(name)
            if pod["restarts"] > self.before.get(name, pod["restarts"]) and name not in self.restarted_at:
                self.restarted_at[name] = elapsed
            if not pod["ready"] and name in self.before and self.phase != "baseline":
                self.was_not_ready.add(name)
        down = [name for name in self.before if not pods.get(name, {}).get("ready")]
        if elapsed is not None and self.all_not_ready_at is None and len(down) == len(self.before):
            self.all_not_ready_at = elapsed
            self.say(f"{kube.clock(elapsed):<5} all {len(down)} pods not ready")
        for event, (pod, count) in killings.items():
            if self.events.get(event, (pod, 0))[1] < count and elapsed is not None and pod in self.before:
                self.killed.setdefault(pod, elapsed)
                self.say(f"{kube.clock(elapsed):<5} {pod.rsplit('-', 1)[-1]}: failed liveness, restarting")
        self.events = {**self.events, **killings}

    def print_line(self):
        close, data, alive = self.line_close, collections.Counter(), collections.Counter()
        late = []
        for call in self.pending:
            if call.started <= close:
                (data if call.route == self.config.data_route else alive)[call.result] += 1
            else:
                late.append(call)
        self.pending = late
        if self.zero is not None:
            at = round(close - self.zero)
            label = kube.clock(at)
        else:
            at = None
            label = f"-0:{self.baseline_left:02d}" if self.phase == "baseline" else "-0:00"
        self.say(row(label, cell(data), cell(alive)))
        self.lines.append({"at": at, "label": label, "data": {str(k): v for k, v in data.items()},
                           "alive": {str(k): v for k, v in alive.items()},
                           "failed": any(k != 200 for k in [*data, *alive])})
        self.line_close += 1
        self.next_line = self.line_close + LATE_SECONDS
        if self.phase == "baseline":
            self.baseline_left -= 1
            if not self.baseline_left and not self.problem:
                self.apply()

    # The block.

    def apply(self):
        self.applied_at = self.clock()
        self.api.post(policies(self.config.namespace), policy_document(self.config.namespace))
        self.apply_returned = self.clock()
        self.say(f"NetworkPolicy {POLICY}: no way in")
        self.phase = "applied"
        self.give_up_at = self.applied_at + TAKES_SECONDS

    def delete_policy(self):
        """True when it was there. A failed call is tried again at once: the block's end has a time."""
        for attempt in range(1, 6):
            try:
                return self.api.delete(self.path)
            except Exception as error:
                print(f"Deleting {POLICY}: {error.__class__.__name__}. Try {attempt} of 5.", flush=True)
                if attempt == 5:
                    raise
                self.sleep(0.5)

    def remove(self):
        self.delete_policy()
        self.removed = True
        self.deleted_at = self.clock()
        self.phase = "recovering"
        self.say(f"{kube.clock(self.deleted_at - self.zero):<5} database back")

    def remove_for_sure(self):
        """The finally: whatever happened, the policy goes, or the log says so."""
        if self.removed:
            return
        try:
            if self.delete_policy():
                self.say(f"{POLICY} deleted: this check stopped early.")
            self.removed = True
        except Exception:
            self.say(f"{POLICY} IS STILL ON. Delete it: kubectl -n {self.config.namespace} delete networkpolicy {POLICY}")

    # The run.

    def finished(self, now):
        if self.problem:
            return True
        if self.phase == "applied" and now >= self.give_up_at:
            self.problem = "no block"
            return True
        if self.phase != "recovering":
            return False
        if now >= self.zero + self.config.seconds + self.config.recover_seconds:
            self.problem = "not back"
            return True
        if now - max(self.last_failure, self.deleted_at) < QUIET_SECONDS:
            return False
        pods = self.latest
        ready = all(pods.get(name, {}).get("ready") for name in self.before)
        # A pod killed late may still be in its preStop while calls already answer: wait for its restart.
        killed_and_back = all(pods.get(name, {}).get("restarts", 0) > self.before[name] for name in self.killed)
        return ready and killed_and_back

    def measure(self):
        config = self.config
        interval = 1 / config.calls_per_second
        self.say(f"{config.service} {self.version}, {config.environment}, {config.calls_per_second} calls a second")
        self.say(row("TIME", config.data_route, config.alive_route))
        start = self.clock()
        # The calls go in the middle of each tenth of a second, away from the lines' edges, so a line reads x10.
        self.next_tick, self.next_poll = start + interval / 2, start
        self.line_close, self.next_line = start + 1, start + 1 + LATE_SECONDS
        while True:
            now = self.clock()
            self.drain()
            if self.phase == "blocked" and now >= self.delete_at:
                self.remove()
            if now >= self.next_tick:
                for route in (config.data_route, config.alive_route):
                    self.pool.submit(self.one_call, route)
                self.next_tick = max(self.next_tick + interval, now + interval / 2)
            if now >= self.next_poll:
                self.next_poll += 1
                if not self.polling:
                    self.polling = True
                    self.pool.submit(self.read_cluster)
            if now >= self.next_line:
                self.print_line()
            self.drain()
            if self.finished(self.clock()):
                return
            due = [self.next_tick, self.next_poll, self.next_line]
            if self.phase == "blocked":
                due.append(self.delete_at)
            self.sleep(max(0.0, min(due) - self.clock()))

    def run(self):
        self.prepare()
        try:
            self.measure()
        finally:
            self.remove_for_sure()
            self.pool.shutdown(wait=False, cancel_futures=True)
        self.pool.shutdown(wait=True)  # the last calls answer within their second
        self.drain()
        return self.result()

    # What it comes to.

    def back_at(self):
        """The first printed second from which no call fails again, or None if the end never came."""
        if self.zero is None or self.problem:
            return None
        failed = [line["at"] for line in self.lines if line["at"] is not None and line["failed"]]
        return (max(failed) + 1) if failed else 1

    def rules(self):
        """The fail rules that apply, each as (rule, what happened), the first of them Slack's."""
        config, found = self.config, []
        if self.problem == "baseline":
            return [("baseline", "failed before the block")]
        if self.zero is None:
            return [("no block", "the block didn't take")] if self.problem == "no block" else [("stopped", "stopped")]
        restarted = self.restarted()
        if restarted:
            found.append(("restarted", f"{len(restarted)} of {config.replicas} pods restarted"))
        alive = [c for c in self.calls if c.route == config.alive_route and c.result != 200]
        if alive:
            found.append(("alive", f"{config.alive_route} failed {len(alive)} times"))
        inside = [c for c in self.calls if c.route == config.data_route and self.zero <= c.started
                  and (self.deleted_at is None or c.started < self.deleted_at) and c.result not in (200, 503)]
        if inside:
            found.append(("data", f"{config.data_route} failed {len(inside)} times in the block other than with a 503"))
        if any(c.served_data for c in self.calls):
            found.append(("served data", f"{config.data_route} carried data in a 503"))
        if self.deleted_at is None:
            found.append(("not back", "the policy wasn't deleted on time"))
        else:
            answered = [c.finished for c in self.calls
                        if c.route == config.data_route and c.result == 200 and c.started >= self.deleted_at]
            if not answered or min(answered) - self.deleted_at > BACK_WITHIN_SECONDS:
                found.append(("recovery", f"{config.data_route} didn't answer within {BACK_WITHIN_SECONDS} s of the delete"))
        if self.problem == "not back":
            found.append(("not back", f"not back by {kube.clock(config.seconds + config.recover_seconds)}"))
        return found

    def restarted(self):
        """The pods that didn't stay up: more restarts than before, or gone."""
        pods = self.latest if self.final is None else self.final
        return [name for name in self.before if name not in pods or pods[name]["restarts"] > self.before[name]]

    def slack_line(self, rules):
        config = self.config
        during = f"during a {config.seconds} s database block."
        restarted = len(self.restarted())
        if not rules:
            return f"{NAME}: {restarted} of {config.replicas} pods restarted {during}"
        first = rules[0][0]
        if first == "baseline":
            return f"{NAME} failed: {config.data_route} or {config.alive_route} didn't answer before the block."
        if first == "no block":
            return f"{NAME} failed: the block didn't take."
        if first == "stopped":
            return f"{NAME} failed: it stopped before the block ended ({self.stopped})."
        if first == "restarted":
            return f"{NAME} failed: {restarted} of {config.replicas} pods restarted {during}"
        route = config.alive_route if first == "alive" else config.data_route
        return f"{NAME} failed: {route} didn't answer {during}"

    def result(self, stopped=None):
        """What blip.json holds, and what the stage decides from."""
        config = self.config
        self.stopped = stopped
        try:
            self.final = self.read_pods() if self.before else {}
        except Exception:  # the last poll stands
            self.final = self.latest
        back = self.back_at()
        rules = [("stopped", stopped)] if stopped else self.rules()
        relative = lambda at: None if at is None or self.zero is None else round(at - self.zero, 2)
        counts = {route: collections.Counter(str(c.result) for c in self.calls if c.route == route)
                  for route in (config.data_route, config.alive_route)}
        inside = {route: collections.Counter(
            str(c.result) for c in self.calls if c.route == route and self.zero is not None and self.zero <= c.started
            and (self.deleted_at is None or c.started < self.deleted_at))
            for route in (config.data_route, config.alive_route)}
        answered = [c.finished for c in self.calls if c.route == config.data_route and c.result == 200
                    and self.deleted_at is not None and c.started >= self.deleted_at]
        return {
            "service": config.service, "version": self.version, "environment": config.environment,
            "namespace": config.namespace, "seconds": config.seconds, "calls_per_second": config.calls_per_second,
            "passed": not rules, "rules": [{"rule": rule, "what": what} for rule, what in rules],
            "slack_line": self.slack_line(rules), "back_at": back, "problem": self.problem,
            "replicas": config.replicas, "restarted": len(self.restarted()),
            "pods": {name: {"restarts_before": before, "restarts_after": self.final.get(name, {}).get("restarts"),
                            "failed_liveness_at": relative_seconds(self.killed.get(name)),
                            "restarted_at": relative_seconds(self.restarted_at.get(name)),
                            "crash_loop_back_off": name in self.crash_loop, "was_not_ready": name in self.was_not_ready}
                     for name, before in self.before.items()},
            "all_not_ready_at": relative_seconds(self.all_not_ready_at),
            "times": {"first_failure_utc": run_state.iso(self.zero_wall) if self.zero_wall else None,
                      "apply": relative(self.applied_at), "apply_returned": relative(self.apply_returned),
                      "first_failure": 0 if self.zero is not None else None, "delete_returned": relative(self.deleted_at),
                      "first_success_after_delete": relative(min(answered)) if answered else None,
                      "last_failure": relative(self.last_failure) if self.last_failure else None},
            "block_seconds": round(self.deleted_at - self.zero, 2) if self.deleted_at and self.zero else None,
            "calls": {route: dict(counts[route]) for route in counts},
            "calls_inside_block": {route: dict(inside[route]) for route in inside},
            "data_in_a_503": sum(c.served_data for c in self.calls),
            "lines": self.lines,
            "every_call": [[relative(c.started) if self.zero is not None else None, c.route, c.result]
                           for c in self.calls],
        }


def relative_seconds(value):
    return None if value is None else round(value, 2)


def run_line(result, build):
    """A run's words in the Jira comment and the work note."""
    back = f"back at {kube.clock(result['back_at'])}" if result["back_at"] is not None else "not back in time"
    return (f"{result['service']} {result['version']}, build #{build}: {result['restarted']} of {result['replicas']} "
            f"pods restarted, {back}.")


def comment(result, build):
    """The Jira comment after a failed check. Only one that measured a block may reach a later work note."""
    measured = result["times"]["first_failure"] is not None and result["problem"] != "baseline"
    what = run_line(result, build) if measured else (
        f"{result['service']} {result['version']}, build #{build}: {result['rules'][0]['what']}.")
    return f"{NAME}: {what} Release stopped."


def comment_text(body):
    """A Jira comment's text, from Atlassian's document format, a line per paragraph."""
    return "\n".join("".join(node.get("text", "") for node in block.get("content", []))
                     for block in (body or {}).get("content", []))


def earlier_line(key):
    """The newest "Blip check: ... Release stopped." on the issue: run 1's words, or None."""
    from jira import Jira

    try:
        reply = Jira().call("GET", f"/rest/api/3/issue/{key}/comment?orderBy=-created&maxResults=100")
    except (Exception, SystemExit) as error:
        print(f"The Jira comments didn't load ({error.__class__.__name__}): the work note has this run's line only.")
        return None
    for comment in reply.get("comments", []):
        found = EARLIER.search(comment_text(comment.get("body")))
        if found:
            return found.group(1)
    return None


def work_note(result, build, earlier, data_route):
    lines = [f"{NAME} in {result['environment']}: database blocked for {result['seconds']} s."]
    if earlier:
        lines.append(earlier)
    lines.append(f"{run_line(result, build)} {data_route} answered 503 until the database was back.")
    return lines


def tell(text):
    """One line for the release's Slack message. Outside a release it only prints."""
    print(text, flush=True)
    if run_state.load().get("slack", {}).get("ts"):
        subprocess.run([sys.executable or "python3", str(SCRIPTS / "notify.py"), "line", text], cwd=ROOT, check=False)


def record(config, result, path):
    """Slack, and Jira or the change's note: where the result goes outside a rehearsal."""
    state = run_state.load()
    build = state.get("build", {}).get("number")
    if result["passed"]:
        state["change_work_notes"] = [*(state.get("change_work_notes") or []),
                                      *work_note(result, build, earlier_line(state["jira_key"]), config.data_route)]
        state["change_attachments"] = [*(state.get("change_attachments") or []), str(path)]
        run_state.save(state)
    tell(result["slack_line"])
    if not result["passed"]:
        subprocess.run([sys.executable or "python3", str(SCRIPTS / "jira.py"), "comment", comment(result, build)],
                       cwd=ROOT, check=False)


def end_line(result):
    pods = f"{result['restarted']} of {result['replicas']} pods restarted."
    if result["back_at"] is not None:
        return f"Back at {kube.clock(result['back_at'])}. {pods}"
    return f"Not back in time. {pods}" if result["problem"] == "not back" else None


def stop_on_sigterm():
    """Jenkins aborts with SIGTERM: this makes it an exception, so the finally deletes the policy.
    Only the first one: a second mustn't stop the delete."""
    def stop(*_):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        sys.exit(143)

    signal.signal(signal.SIGTERM, stop)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("service")
    parser.add_argument("--environment", help="by default services.yaml's blip_check.environment")
    parser.add_argument("--rehearsal", action="store_true", help="no Slack, Jira or change")
    args = parser.parse_args()

    stop_on_sigterm()
    config = settings(args.service, args.environment)
    state = run_state.load()
    say = Lines()
    path = run_state.PATH.parent / "blip.json"
    with kube.credentials() as kubeconfig:
        check = Check(config, kube.Api(kubeconfig), state.get("version"), say)
        try:
            result = check.run()
        except Exception as error:  # the policy is gone by now: say why this stopped, then fail the stage
            result = check.result(stopped=f"{error.__class__.__name__}: {error}")
    line = end_line(result)
    if line:
        say(line)
    result["build"] = state.get("build", {}).get("number")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=1) + "\n")
    print(f"Block {result['block_seconds']} s. Calls: {result['calls']}. {result['slack_line']}", flush=True)
    if not args.rehearsal:
        record(config, result, path)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
