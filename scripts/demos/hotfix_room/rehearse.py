"""Video 33's first-day measurement: does a real Pending come out of the reservation, and what do the numbers read?

    .venv/bin/python scripts/demos/hotfix_room/rehearse.py            from the Mac, about 12 minutes
    .venv/bin/python scripts/demos/hotfix_room/rehearse.py --follow   its log again, if the Mac stopped following

Nothing here has run on a cluster until this does, and its verdict decides whether
the video exists. It runs on the tools host, handed over like reset.py, so it needs
this on main. First: scripts/tools.sh up, scripts/demo.sh up kubernetes and
scripts/demo.sh up hotfix-room.

With the pipeline's own build, deploy and check scripts, and no Slack, ServiceNow or
Jira, it puts catalog on 1.12.0 and forecast on 2.4.0, reads the node, then deploys
catalog 1.12.1 to staging: the new pod must stay Pending on a real scheduler event,
Helm must roll back after 3 minutes, and the old pods must keep answering. Then
forecast 2.4.1, then catalog 1.12.1 to staging and production. It prints one PASS or
FAIL per check, what fills the script's brackets, and the verdict, and ends with the
cluster where a take starts, 1.12.0 and 2.4.0. Exit code 0: the video stands. 1: it
doesn't, as things are. 2: it stands, and another check must be fixed before a take.
"""

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parents[1]
KUBERNETES = SCRIPTS / "demos" / "kubernetes"
os.environ.setdefault("WORKSPACE", tempfile.mkdtemp(prefix="hotfix-rehearsal-"))
os.environ.setdefault("RELEASE_DEMO_CACHE", "/tmp/release-demo-cache")
os.environ.setdefault("OCI_CACHE", "/tmp/release-demo-cache/oci")
sys.path.insert(0, str(HERE))
sys.path.insert(1, str(KUBERNETES))  # before scripts/: its build.py and deploy.py share names with the Lambda ones
sys.path.append(str(SCRIPTS))

import build  # noqa: E402
import deploy  # noqa: E402
import kube  # noqa: E402
import room  # noqa: E402
import run_state  # noqa: E402
from common import ROOT, get_parameter, git, service, ssm_client  # noqa: E402

CATALOG, FORECAST = "catalog", "forecast"
BASELINE, HOTFIX = "1.12.0", "1.12.1"
PADDED, SMALL = "2.4.0", "2.4.1"
ADMIN = "/etc/rancher/k3s/k3s.yaml"
ALLOCATABLE_MIB = 1180                 # what k3s.py's settings give the scheduler: 1,280 less the 100 MiB eviction threshold
USED_STANDS = 40                       # percent of the node: above it the hook loses its contrast
USED_NO_VIDEO = 60                     # above it there is no contrast to film
SAMPLE_SECONDS = 3
results = []


def result(ok, what, gate=False):
    """One line per check. A gate's FAIL decides the video; any other must be fixed before a take."""
    results.append((bool(ok), what, gate))
    print(f"{'PASS' if ok else 'FAIL'} {what}", flush=True)


def loaded(name, path):
    """A script from another folder under its own name: several here share one."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def admin(*args, check=True):
    reply = subprocess.run(["kubectl", *args], capture_output=True, text=True, env={**os.environ, "KUBECONFIG": ADMIN})
    if check and reply.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args[:3])}: {reply.stderr.strip()[:300]}")
    return reply.stdout


def admin_json(*args):
    return json.loads(admin(*args))


def mib(number):
    return int(number // room.MIB)


# What the readings come to. Pure, so the tests hold them to the spec's rules.

def described_memory(text):
    """(requests in MiB, its percent) from kubectl describe node's Allocated resources. None if it isn't there."""
    found = re.search(r"^\s+memory\s+(\d+)(Mi|Ki|Gi)?\s+\((\d+)%\)", text, re.M)
    if not found:
        return None
    value = room.quantity(found.group(1) + (found.group(2) or ""))
    return int(value // room.MIB), int(found.group(3))


def scheduler_words_hold(message):
    return message.startswith("0/1 nodes are available") and "Insufficient memory" in message


def used_share_line(reading):
    """Where the hook stands on the day's reading."""
    return (f"{mib(reading.used)} MiB used of {mib(reading.allocatable)} is {reading.used_percent}%, "
            f"{mib(reading.reserved)} MiB reserved is {reading.reserved_percent}%")


def verdict(pending, rolled_back, reading, second_try):
    """(stands, lines). pending: the scheduler's message on the hotfix's pod, or None."""
    if not pending or not scheduler_words_hold(pending):
        return False, ["THE VIDEO ISN'T MADE: no real Pending on Insufficient memory came out of the reservation.",
                       f"The scheduler said: {pending or 'nothing'}.",
                       "Set forecast's requests from the free room this run printed, in demo-states/forecast/2.4.0/chart/values.yaml, "
                       "and run this again. If the node can't be filled by its requests, say so in REVIEW."]
    if not rolled_back:
        return False, ["NO VIDEO UNTIL FIXED: the pod stayed Pending, but Helm didn't roll back as video 8's path does."]
    if reading.used_percent > USED_NO_VIDEO:
        return False, [f"THE VIDEO ISN'T MADE: {used_share_line(reading)}. Above {USED_NO_VIDEO}% there's no contrast to film.",
                       "H takes the slot, as the research says."]
    if not second_try:
        return False, ["NO VIDEO UNTIL FIXED: with the memory given back, the same hotfix didn't go through."]
    lines = ["THE VIDEO STANDS."]
    if reading.used_percent > USED_STANDS:
        lines.append(f"But {used_share_line(reading)}: over {USED_STANDS}%. Decide: the hook's line says the number as read, or "
                     "it says forecast's own share instead (open question 2).")
    return True, lines


def brackets(reading, after, seconds, peak_mib):
    return [f"[{reading.used_percent}], percent used, the hook: {used_share_line(reading)}",
            f"Reserved {mib(reading.reserved)} MiB, free to reserve {mib(reading.free)} MiB, catalog's next pod asks for {mib(reading.ask)} MiB",
            f"forecast reserved {mib(reading.forecast_reserved)} MiB, uses {mib(reading.forecast_used)} MiB, [{mib(reading.forecast_used)}] in 33.7",
            f"After forecast {SMALL}: reserved {mib(after.reserved)} MiB, {after.reserved_percent}%, free {mib(after.free)} MiB, "
            f"so [{mib(reading.forecast_reserved) - mib(after.forecast_reserved)}] come back",
            f"Helm's wait: {kube.clock(seconds)}. The pods' highest reading through all of this: {peak_mib} MiB"]


# On the cluster.

class Prober(threading.Thread):
    """GET /health five times a second: which versions answer, and how many calls fail."""

    def __init__(self, url):
        super().__init__(daemon=True)
        self.url, self.versions, self.failures = url, {}, 0
        self.stop = threading.Event()

    def run(self):
        while not self.stop.wait(0.2):
            try:
                with urllib.request.urlopen(f"{self.url}/health", timeout=2) as response:
                    version = json.loads(response.read())["version"]
                    self.versions[version] = self.versions.get(version, 0) + 1
            except Exception:
                self.failures += 1

    def done(self):
        self.stop.set()
        self.join(3)
        return self.versions, self.failures


class Watch(threading.Thread):
    """While Helm waits: the hotfix's pod, what the scheduler said about it, and the node's reading then."""

    def __init__(self, read=None):
        super().__init__(daemon=True)
        self.read = read or (lambda: room.read(admin_json))
        self.reading, self.pod, self.said, self.pending_seen = None, "", "", 0
        self.stop = threading.Event()

    def run(self):
        while not self.stop.wait(SAMPLE_SECONDS):
            try:
                reading = self.read()
            except Exception as error:  # a missed read isn't a verdict
                print(f"     no reading: {error}", flush=True)
                continue
            if reading.pending:
                if not self.pod:
                    print(f"     {reading.pending} is Pending: {reading.said}", flush=True)
                self.pod, self.said, self.reading = reading.pending, reading.said, reading
                self.pending_seen += 1

    def done(self):
        self.stop.set()
        self.join(SAMPLE_SECONDS + 20)
        return self


class Machine:
    """What runs on the tools host: the pipeline's scripts against the real cluster."""

    def __init__(self, commit):
        self.commit = commit
        self.configs = {name: service(name) for name in (CATALOG, FORECAST)}
        self.token = get_parameter(ssm_client(), "jcr/token")
        self.rehearsal = loaded("kube_rehearse", KUBERNETES / "rehearse.py")
        self.peak = 0
        self.sampling = threading.Event()

    def url(self, name, environment):
        return f"http://127.0.0.1:{self.configs[name]['environments'][environment]['node_port']}"

    def preflight(self):
        problems = []
        if not admin("get", "namespace", room.FORECAST, "--ignore-not-found", "-o", "name", check=False).strip():
            problems.append(f"no namespace {room.FORECAST}: scripts/demo.sh up hotfix-room")
        for namespace in ("staging", "production"):
            if admin("get", "pod", "catalog-postgres-0", "-n", namespace, "--ignore-not-found", "-o", "name", check=False).strip():
                problems.append(f"catalog-postgres runs in {namespace}: scripts/demo.sh down database-blip")
        return problems

    def only_cluster_to_baseline(self):
        """The reset's cluster step alone: no Jira, ServiceNow or GitHub in a rehearsal."""
        reset = loaded("hotfix_reset", HERE / "reset.py")
        plain = type("Plain", (), {"act": staticmethod(lambda what, do: (print(f"  {what}", flush=True), do()))})
        with kube.credentials(quiet=True) as kubeconfig:
            reset.cluster(plain, kubeconfig, kube.Api(kubeconfig))

    def package(self, name, version):
        out = tempfile.mkdtemp(prefix=f"build-{version}-")
        info = build.build(name, version_dir=ROOT / "demo-states" / name / version, out=out, commit=self.commit)
        package = (Path(out) / name / info["package"]).read_bytes()
        state = {"service": name, "version": version, "build": {"number": "rehearsal"},
                 "package": {"path": f"(rehearsal) {info['package']}", "sha256": info["sha256"]}}
        run_state.save(state)
        print(f"== {name} {version}: {info['image']}", flush=True)
        return package, state, info

    def deploy(self, name, environment, package, state):
        return deploy.deploy(self.configs[name], environment, package, state, self.token) == 0

    def check(self, name, environment, version):
        state = run_state.load()
        url = state["deploys"][environment]["url"]
        return subprocess.run([sys.executable, str(KUBERNETES / "check.py"), name, environment, "--version", version,
                               "--url", url]).returncode == 0

    def go_back(self, name, environment):
        return subprocess.run([sys.executable, str(KUBERNETES / "deploy.py"), name, environment, "--back"]).returncode == 0

    def sample_memory(self):
        def current(path):
            file = Path("/sys/fs/cgroup") / path / "memory.current"
            return int(file.read_text()) >> 20 if file.exists() else 0
        while not self.sampling.wait(SAMPLE_SECONDS):
            self.peak = max(self.peak, current("kubepods.slice"), current("kubepods"))

    def check_the_settings(self, reading):
        result(mib(reading.allocatable) == ALLOCATABLE_MIB,
               f"1. the node can hand out {mib(reading.allocatable)} MiB; k3s.py's 1,280 less the eviction threshold is {ALLOCATABLE_MIB}")
        described = described_memory(admin("describe", "node", room.NODE))
        result(described == (mib(reading.reserved), reading.reserved_percent),
               f"2. kubectl describe node says {described}; this reading says ({mib(reading.reserved)}, {reading.reserved_percent})")
        limit = Path("/sys/fs/cgroup/kubepods.slice/memory.max")
        limit = limit if limit.exists() else Path("/sys/fs/cgroup/kubepods/memory.max")
        print(f"     the pods' cgroup is held to {int(limit.read_text()) >> 20 if limit.exists() else 'unknown'} MiB", flush=True)
        forecast = admin_json("get", "pods", "-n", room.FORECAST, "-o", "json")["items"]
        result(len(forecast) == 3 and all(pod["status"].get("phase") == "Running" for pod in forecast),
               f"2. every forecast pod has its place: {[pod['status'].get('phase') for pod in forecast]}")
        result(not reading.fits and reading.free >= 0,
               f"2. free to reserve {mib(reading.free)} MiB, and catalog's next pod asks for {mib(reading.ask)} MiB")

    def check_used(self, reading):
        """The kubelet's reading against the cgroup k3s.py reads, and the working set for the record."""
        summary = admin_json("get", "--raw", room.SUMMARY)
        pods_container = next((item for item in summary["node"].get("systemContainers", []) if item["name"] == "pods"), {})
        working = pods_container.get("memory", {}).get("workingSetBytes")
        print(f"     working set of the pods' cgroup: {mib(float(working)) if working else 'not read'} MiB, for the record only", flush=True)
        path = Path("/sys/fs/cgroup/kubepods.slice/memory.current")
        path = path if path.exists() else Path("/sys/fs/cgroup/kubepods/memory.current")
        cgroup = int(path.read_text()) >> 20 if path.exists() else None
        result(cgroup is not None and abs(cgroup - mib(reading.used)) <= max(20, cgroup // 10),
               f"5. the kubelet's used, {mib(reading.used)} MiB, against the pods' cgroup, {cgroup} MiB (within 10%)")

    def can_i(self):
        with kube.credentials(quiet=True) as kubeconfig:
            env = {**os.environ, "KUBECONFIG": str(kubeconfig)}
            ask = lambda *args: subprocess.run(["kubectl", "auth", "can-i", *args], env=env, capture_output=True,
                                               text=True).stdout.strip()
            yes = [ask("patch", "deploy", "-n", room.FORECAST), ask("get", "events", "-n", "staging")]
            no = [ask("get", "nodes"), ask("get", "nodes/proxy"), ask("get", "pods", "-A"), ask("patch", "deploy", "-n", "kube-system"),
                  ask("patch", "pods", "--subresource=resize", "-n", room.FORECAST), ask("patch", "pods", "-n", room.FORECAST)]
        result(set(yes) == {"yes"} and set(no) == {"no"},
               f"9. Jenkins may patch forecast's Deployment and read staging's events ({', '.join(yes)}), not the node, "
               f"other namespaces or a pod by hand ({', '.join(no)})")
        before = len(self.rehearsal.results)
        self.rehearsal.can_i()
        result(all(self.rehearsal.results[before:]), "9. video 8's can-i answers still hold")

    def no_hand_edit(self, version):
        """Check 16: the pods ask for what the chart says, and no kubectl wrote the Deployment."""
        deployment = admin_json("get", "deployment", FORECAST, "-n", room.FORECAST, "-o", "json")
        managers = {field.get("manager") or "" for field in deployment["metadata"].get("managedFields", [])}
        pods = admin_json("get", "pods", "-n", room.FORECAST, "-o", "json")["items"]
        template = room.request_of(deployment["spec"]["template"]["spec"])
        same = all(room.request_of(pod["spec"]) == template for pod in pods)
        result(same and not any(manager.startswith("kubectl") for manager in managers),
               f"16. forecast's {len(pods)} pods all ask for what {version}'s chart says, and the Deployment's writers are {sorted(managers)}")

    def fits_the_cgroup(self):
        kills = admin("get", "events", "-A", "--field-selector", "reason=OOMKilling", "-o", "name", check=False).strip()
        evicted = admin("get", "events", "-A", "--field-selector", "reason=Evicted", "-o", "name", check=False).strip()
        result(self.peak < 1280 and not kills and not evicted,
               f"10. peak {self.peak} MiB in pods (under 1,280), OOM kills {len(kills.splitlines())}, evictions {len(evicted.splitlines())}")


def measure(machine):
    problems = machine.preflight()
    if problems:
        for problem in problems:
            result(False, f"Not started: {problem}", gate=True)
        return 1
    sampler = threading.Thread(target=machine.sample_memory, daemon=True)
    sampler.start()

    print("== The stage: catalog 1.12.0, forecast 2.4.0", flush=True)
    machine.only_cluster_to_baseline()
    before = room.read(admin_json)
    print("\n".join(room.frame(before)), flush=True)
    machine.check_the_settings(before)

    print("== The hotfix: 1.12.1 to staging, and Helm's 3 minutes", flush=True)
    package, state, first = machine.package(CATALOG, HOTFIX)
    prober = Prober(machine.url(CATALOG, "staging"))
    prober.start()
    watch = Watch()
    watch.start()
    deployed = machine.deploy(CATALOG, "staging", package, state)
    watch.done()
    versions, failures = prober.done()
    record = run_state.load()["deploys"]["staging"]
    helm_seconds = record.get("helm_seconds", 0)
    rolled_back = deployed and record.get("helm_rolled_back_to") == BASELINE
    result(scheduler_words_hold(watch.said), f"3. a real Pending: {watch.pod or 'none'}: {watch.said or 'no event'}", gate=True)
    result(watch.pending_seen >= 10, f"3. Pending seen {watch.pending_seen} times, every {SAMPLE_SECONDS} s, through Helm's wait")
    result(rolled_back, f"4. Helm rolled back to {record.get('helm_rolled_back_to')} after {kube.clock(record.get('helm_seconds', 0))}: "
                        f"{record.get('helm_rollback_reason')}", gate=True)
    result(record.get("scheduler_said") == "0/1 nodes are available: 1 Insufficient memory.",
           f"4. the line for Slack names it: {record.get('scheduler_said')}")
    result(not machine.check(CATALOG, "staging", HOTFIX), "4. Check staging fails at once, as the pipeline needs")
    result(machine.go_back(CATALOG, "staging") and run_state.load()["deploys"]["staging"].get("rolled_back_by") == "Helm",
           "4. the pipeline's rollback (deploy.py --back) confirms Helm's, without a second one")
    result(set(versions) == {BASELINE} and failures == 0,
           f"3. the old pods kept answering: {versions}, {failures} failed calls; ready pods never fewer than {record.get('lowest_ready')} of 3")
    reading = watch.reading or room.read(admin_json)
    machine.check_used(reading)
    print(f"     {used_share_line(reading)}", flush=True)

    print("== The memory given back: forecast 2.4.1", flush=True)
    package, state, _ = machine.package(FORECAST, SMALL)
    given_back = machine.deploy(FORECAST, "production", package, state) and machine.check(FORECAST, "production", SMALL)
    record = run_state.load()["deploys"]["production"]
    result(given_back, f"6. forecast {SMALL} rolls out on the full node in {kube.clock(record.get('helm_seconds', 0))}, "
                       f"ready pods never fewer than {record.get('lowest_ready')} of 3", gate=True)
    after = room.read(admin_json)
    result(after.fits and after.reserved < before.reserved,
           f"6. reserved {mib(after.reserved)} MiB, {after.reserved_percent}%; free {mib(after.free)} MiB, room for {mib(after.ask)} MiB")
    machine.no_hand_edit(SMALL)

    print("== The same hotfix again", flush=True)
    package, state, second = machine.package(CATALOG, HOTFIX)
    result(first["sha256"] == second["sha256"] and first["image_digest"] == second["image_digest"],
           f"8. the same source gives the same SHA-256 and image digest twice: {second['image_digest'][:19]}...")
    prober = Prober(machine.url(CATALOG, "staging"))
    prober.start()
    in_staging = machine.deploy(CATALOG, "staging", package, state) and machine.check(CATALOG, "staging", HOTFIX)
    versions, failures = prober.done()
    in_production = machine.deploy(CATALOG, "production", package, run_state.load()) and machine.check(CATALOG, "production", HOTFIX)
    record = run_state.load()["deploys"]
    second_try = in_staging and in_production
    result(second_try, f"7. 1.12.1 passes staging and production, ready pods never fewer than "
                       f"{record['staging'].get('lowest_ready')} and {record['production'].get('lowest_ready')} of 3", gate=True)
    result(failures == 0, f"7. no failed calls through the rollout: {versions}")

    machine.can_i()
    machine.sampling.set()
    sampler.join(5)
    machine.fits_the_cgroup()
    return finish(watch.said, rolled_back, reading, second_try, after, helm_seconds, machine.peak)


def finish(said, rolled_back, reading, second_try, after, helm_seconds, peak):
    """Prints the brackets and the verdict. Returns the exit code."""
    stands, lines = verdict(said, rolled_back, reading, second_try)
    print("== What fills the script's brackets", flush=True)
    for line in brackets(reading, after, helm_seconds, peak):
        print(f"   {line}", flush=True)
    others = [what for ok, what, gate in results if not ok and not gate]
    passed = sum(ok for ok, _, _ in results)
    print(f"== Verdict, {passed} PASS and {len(results) - passed} FAIL", flush=True)
    for line in lines:
        print(f"   {line}", flush=True)
    if stands and others:
        print(f"   It stands, but fix these before a take: {len(others)} FAIL above, first: {others[0][:80]}", flush=True)
    return (0 if not others else 2) if stands else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--here", action="store_true", help="run here: the tools host")
    parser.add_argument("--follow", action="store_true", help="from the Mac: show the run on the tools host")
    args = parser.parse_args()
    shared_host = loaded("shared_host", SCRIPTS / "host.py")
    if not args.here and not shared_host.on_host():
        return shared_host.hand_over("hotfix-rehearse", "demos/hotfix_room/rehearse.py", [], follow_only=args.follow,
                                     title="video 33 first-day measurement")
    sys.stdout.reconfigure(line_buffering=True)
    started = time.monotonic()
    commit = git("rev-parse", "HEAD", check=False).stdout.strip() or "rehearsal"
    machine = Machine(commit)
    try:
        code = measure(machine)
    except RuntimeError as error:   # kubectl's own words: a read the check needs, refused or not answered
        print(f"NO VERDICT: {error}", flush=True)
        code = 1
    finally:
        machine.sampling.set()
    print("== Back where a take starts", flush=True)
    machine.only_cluster_to_baseline()
    print(f"Done in {kube.clock(time.monotonic() - started)}. Exit code {code}.", flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
