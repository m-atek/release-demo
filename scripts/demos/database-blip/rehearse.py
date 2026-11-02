"""Video 25's first-day measurement: does 1.10.0 restart every pod, and does 1.10.1 stop that?

    .venv/bin/python scripts/demos/database-blip/rehearse.py              from the Mac, about 20 minutes
    .venv/bin/python scripts/demos/database-blip/rehearse.py --follow     its log again, if the Mac stopped following
    .venv/bin/python scripts/demos/database-blip/rehearse.py --liveness-period 5    1.10.0 with that period at once

The spec's times are predictions until this has run, and its verdict decides
whether the video exists. It runs on the tools host, handed over like reset.py,
so it needs this on main. First: scripts/tools.sh up, scripts/demo.sh up kubernetes
(which applies the new Role) and scripts/demo.sh up database-blip.

With the pipeline's own build, deploy, check and blip_check scripts, and no
Slack, ServiceNow or Jira, it deploys 1.10.0 to staging and blocks the database
three times, with new pods between, then once more on the same pods to measure the
back-off. If fewer than 3 of 3 restart it follows the spec's ladder: the block's
real length, then liveness at periodSeconds 5. Then 1.10.1 in staging and
production, three blocks, and acceptance checks 1, 2, 3, 9, 13 and 15. It ends with
the cluster on 1.9.1, one PASS or FAIL line per check, what fills the script's
brackets, and the verdict. Exit code 0: the video stands. 1: it doesn't, as things
are. 2: it stands, and another check must be fixed before a take.
"""

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parents[1]
KUBERNETES = SCRIPTS / "demos" / "kubernetes"
os.environ.setdefault("WORKSPACE", tempfile.mkdtemp(prefix="blip-rehearsal-"))
os.environ.setdefault("RELEASE_DEMO_CACHE", "/tmp/release-demo-cache")
os.environ.setdefault("OCI_CACHE", "/tmp/release-demo-cache/oci")
sys.path.insert(1, str(KUBERNETES))  # before scripts/: its build.py and deploy.py share names with the Lambda ones
sys.path.append(str(SCRIPTS))

import build  # noqa: E402
import deploy  # noqa: E402
import kube  # noqa: E402
import run_state  # noqa: E402
from common import ROOT, get_parameter, git, http_get, service, ssm_client  # noqa: E402
import blip_check  # noqa: E402  (after build and deploy: it puts scripts/ first on the path)

SERVICE = "catalog"
BASELINE, FAILING, FIXED = "1.9.1", "1.10.0", "1.10.1"
ADMIN = "/etc/rancher/k3s/k3s.yaml"
POSTGRES = "catalog-postgres-0"
BLOCKS = 3
BACK_WITHIN_SECONDS = 2  # check 5
APPLY_WITHIN_SECONDS = 3  # check 2
SCRIPT_RUNS_SECONDS = 6  # what 25.6 is worth, if the pods aren't all down at once
results = []


def result(ok, what, gate=False):
    """One line per check. A gate's FAIL may be saved by the ladder; any other must be fixed before a take."""
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
    """kubectl as the cluster admin: the psql, exec and logs the build's token may not do."""
    reply = subprocess.run(["kubectl", *args], capture_output=True, text=True, env={**os.environ, "KUBECONFIG": ADMIN})
    if check and reply.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args[:3])}: {reply.stderr.strip()[:300]}")
    return reply.stdout


def clock(seconds):
    return "none" if seconds is None else kube.clock(seconds)


def agent_may_write(path, user="jenkins-agent"):
    """Take 1's bottom window follows this log, which the pipeline's Blip check writes as the agent's
    user. This measurement runs as root, so nothing else here would find out it can't."""
    return subprocess.run(["runuser", "-u", user, "--", "test", "-w", str(path)], capture_output=True).returncode == 0


def cpu_line(before, after, seconds):
    """What the pods used through a block, from {uid: (microseconds used, periods, periods throttled)}."""
    kept = [uid for uid in after if uid in before]
    if not kept:
        return "CPU: no pod lasted the block"
    busiest = max((after[uid][0] - before[uid][0]) / seconds / 1000 for uid in kept)
    periods = sum(after[uid][1] - before[uid][1] for uid in kept)
    throttled = sum(after[uid][2] - before[uid][2] for uid in kept)
    return f"CPU: the busiest pod used {busiest:.0f}m on average; the limit held the pods in {throttled} of {periods} periods"


def clocks(values):
    return ", ".join(clock(value) for value in sorted(value for value in values if value is not None)) or "none"


# What each block comes to. Pure, so the tests hold them to the spec's rules.

def failing_block(blip):
    """Check 4 for one block of 1.10.0. (it restarted every pod, once; all not ready at once; its line)"""
    pods, replicas = blip["pods"].values(), blip["replicas"]
    every_once = blip["restarted"] == replicas == len(pods) and all(
        pod["restarts_after"] == pod["restarts_before"] + 1 for pod in pods)
    no_loop = not any(pod["crash_loop_back_off"] for pod in pods)
    line = (f"{blip['restarted']} of {replicas} restarted, each once: {every_once}; CrashLoopBackOff: {not no_loop}; "
            f"all not ready at {clock(blip['all_not_ready_at'])}; failed liveness at "
            f"{clocks(pod['failed_liveness_at'] for pod in pods)}; restarts seen at "
            f"{clocks(pod['restarted_at'] for pod in pods)}; back at {clock(blip['back_at'])}; "
            f"block {blip['block_seconds']} s; calls {blip['calls']}")
    return every_once and no_loop, blip["all_not_ready_at"] is not None, line


def sentence_holds(blip, data_route, alive_route):
    """"The pods stay up and answer errors on database routes": without it there is no video until it's fixed."""
    pods = blip["pods"].values()
    data, alive = blip["calls"][data_route], blip["calls"][alive_route]
    kept_up = blip["restarted"] == 0 and not any(pod["was_not_ready"] for pod in pods)
    alive_clean = set(alive) <= {"200"}
    only_answers = set(data) <= {"200", "503"}
    in_block_only_503 = set(blip["calls_inside_block"][data_route]) <= {"503"}
    return kept_up and alive_clean and only_answers and in_block_only_503 and blip["data_in_a_503"] == 0 and blip["passed"]


def fixed_block(blip, data_route, alive_route):
    """Check 5 for one block of 1.10.1. (it passes, its line) A pass also wants the calls back within 2 s
    of the delete, the spec's prediction. Later than that, the check itself still passes up to its 5 s, so
    the line fails with the time in it and the sentence stands: a slow way back isn't a broken fix."""
    pods = blip["pods"].values()
    data, alive = blip["calls"][data_route], blip["calls"][alive_route]
    back = blip["back_at"]
    # From the calls, not the line: a line counts its whole second, so "Back at" can read up to 1 s later.
    times = blip["times"]
    back_in_time = back is not None and times["last_failure"] - times["delete_returned"] <= BACK_WITHIN_SECONDS
    ok = sentence_holds(blip, data_route, alive_route) and back_in_time
    line = (f"{blip['restarted']} of {blip['replicas']} restarted; READY true throughout: "
            f"{not any(pod['was_not_ready'] for pod in pods)}; {alive_route} {alive}; {data_route} {data}, in the block "
            f"{blip['calls_inside_block'][data_route]}; a 503 with data: {blip['data_in_a_503']}; back at {clock(back)}, "
            f"last failed call {times['last_failure']} s; block {blip['block_seconds']} s")
    return ok, line


def verdict(failing, ladder, fixed, liveness_period=None):
    """(stands, lines) from the blocks: each list a block's (restarted every pod, all down at once, ...).
    failing: 1.10.0's checks. ladder: the same with periodSeconds 5, or None. fixed: 1.10.1's oks, or None."""
    every = bool(failing) and all(ok for ok, *_ in failing)
    with_period = bool(ladder) and all(ok for ok, *_ in ladder)
    if not every and not with_period:
        return False, ["THE VIDEO ISN'T MADE: 1.10.0 didn't restart every pod in every block"
                       + (", and periodSeconds 5 didn't help." if ladder is not None else "."),
                       "No longer block presented as a blip, no failureThreshold: 1, no extra failure to force it.",
                       "Say so in REVIEW, and spend the slot on another video from the gap list."]
    lines = []
    if with_period and not every:
        lines += ["1.10.0 restarts every pod only with liveness at periodSeconds: 5: a common setting, not a forced one.",
                  "Adopt it: that line in demo-states/catalog/1.10.0/chart/templates/deployment.yaml and in its test_chart.py.",
                  "The script's \"One line\" becomes \"Two lines\", and reveal.sh a shows both (grep -A3)."]
    elif liveness_period:
        lines += [f"1.10.0 restarts every pod with liveness at periodSeconds: {liveness_period}."]
    if fixed is None:
        return False, [*lines, "NOT DECIDED: 1.10.1 wasn't measured."]
    if not all(fixed):
        return False, [*lines, "NO VIDEO UNTIL FIXED: \"the pods stay up and answer errors\" doesn't hold on 1.10.1.",
                       "First suspect: CPU throttling at the pods' limit, in the CPU lines above. Lower calls_per_second to 5 in "
                       "services.yaml, or raise the limit in both versions alike, and run this again."]
    used = failing if every else ladder
    if not all(down for _, down, *_ in used):
        lines.append(f"All 3 restart, but they weren't all not ready at once: \"every\" still holds. 25.6 goes, "
                     f"and the video runs about {SCRIPT_RUNS_SECONDS} s shorter.")
    return True, ["THE VIDEO STANDS.", *lines]


def brackets(failing_blips, fixed_blips):
    """What fills the script's brackets, and which of its fallbacks apply."""
    seconds = failing_blips[0]["seconds"] if failing_blips else 30
    down = [blip["all_not_ready_at"] for blip in failing_blips]
    backs = [blip["back_at"] for blip in failing_blips if blip["back_at"] is not None]
    fixed_backs = [blip["back_at"] for blip in fixed_blips if blip["back_at"] is not None]
    lines = [f"[Ten]: all not ready at {clocks(down)}. Said as the take prints it.",
             f"[thirty-eight], 1.10.0 back at: {clocks(backs)}. Predicted 0:31 to 0:42.",
             f"[thirty-one], 1.10.1 back at: {clocks(fixed_backs)}. Predicted 0:30 to 0:32."]
    if backs and max(backs) - seconds <= 3:
        lines.append("25.9's fallback: 1.10.0 is back within 3 s of the database: "
                     "\"Database back at thirty seconds, and the catalog [N] seconds later.\"")
    if backs and fixed_backs and abs(min(backs) - max(fixed_backs)) <= 2:
        lines.append("Fallback: the two back times are within 2 s: 25.9 says only \"Database back at thirty seconds.\", "
                     "25.15 only \"Restarts: zero.\", and the work note's back times stay out of the outline.")
    return lines


# On the cluster.

class Machine:
    """What runs on the tools host: the pipeline's scripts against the real cluster."""

    def __init__(self, commit, blocks=BLOCKS):
        self.commit, self.blocks = commit, blocks
        self.config = service(SERVICE)
        self.blip = blip_check.settings(SERVICE)
        self.token = get_parameter(ssm_client(), "jcr/token")
        self.staging = f"http://127.0.0.1:{self.config['environments']['staging']['node_port']}"
        self.production = f"http://127.0.0.1:{self.config['environments']['production']['node_port']}"
        self.path = run_state.PATH.parent / "blip.json"
        self.rehearsal = loaded("kube_rehearse", KUBERNETES / "rehearse.py")
        self.peak_pods = self.peak_total = 0
        self.sampling = threading.Event()
        self.changed_the_cluster = False

    def preflight(self):
        problems = []
        for environment in self.config["environments"].values():
            status = admin("get", "pod", POSTGRES, "-n", environment["namespace"], "--ignore-not-found", "-o",
                           "jsonpath={.status.containerStatuses[0].ready}", check=False).strip()
            if status != "true":
                problems.append(f"{POSTGRES} isn't ready in {environment['namespace']}: scripts/demo.sh up database-blip")
        with kube.credentials(quiet=True) as kubeconfig:
            allowed = subprocess.run(["kubectl", "auth", "can-i", "create", "networkpolicies", "-n", self.blip.namespace],
                                     capture_output=True, text=True, env={**os.environ, "KUBECONFIG": str(kubeconfig)}).stdout
        if allowed.strip() != "yes":
            problems.append("Jenkins' account may not create the policy: scripts/demo.sh up kubernetes applies the Role")
        return problems

    def put_on_baseline(self):
        self.changed_the_cluster = True
        subprocess.run([sys.executable, str(KUBERNETES / "reset.py"), "--here", "--baseline", BASELINE, "--commit", self.commit],
                       check=True)

    def release(self, version, folder=None):
        """A demo state, built and given a run state, as run_state.py init and publish.py would."""
        out = tempfile.mkdtemp(prefix=f"build-{version}-")
        info = build.build(SERVICE, version_dir=folder or ROOT / "demo-states" / SERVICE / version, out=out, commit=self.commit)
        package = (Path(out) / SERVICE / info["package"]).read_bytes()
        state = {"service": SERVICE, "version": version, "build": {"number": "rehearsal"},
                 "package": {"path": f"(rehearsal) {info['package']}", "sha256": info["sha256"]}}
        run_state.save(state)
        print(f"== {SERVICE} {version}: {info['image']}", flush=True)
        return package, state, info

    def deploy(self, environment, package, state, version):
        code = deploy.deploy(self.config, environment, package, state, self.token)
        return code == 0 and self.rehearsal.check(environment, version)

    def one_block(self):
        """blip_check.py as the pipeline runs it, without Slack, Jira or the change: what it wrote."""
        self.path.unlink(missing_ok=True)
        before, started = self.pods_cpu(), time.monotonic()
        subprocess.run([sys.executable, str(HERE / "blip_check.py"), SERVICE, "--rehearsal"])
        print(f"     {cpu_line(before, self.pods_cpu(), time.monotonic() - started)}", flush=True)
        blip = json.loads(self.path.read_text())
        return blip

    def pods_cpu(self):
        """Each catalog pod's CPU so far, from its cgroup, which outlives a restarted container."""
        found = {}
        reply = json.loads(admin("get", "pods", "-n", self.blip.namespace, "-l", "app.kubernetes.io/instance=catalog", "-o", "json"))
        for pod in reply["items"]:
            uid = pod["metadata"]["uid"].replace("-", "_")
            for file in Path("/sys/fs/cgroup").glob(f"kubepods*/*/*pod{uid}*/cpu.stat"):
                stats = dict(line.split() for line in file.read_text().splitlines())
                found[uid] = (int(stats["usage_usec"]), int(stats["nr_periods"]), int(stats["nr_throttled"]))
        return found

    def new_pods(self):
        with kube.credentials(quiet=True) as kubeconfig:
            env = {**os.environ, "KUBECONFIG": str(kubeconfig)}
            for args in (["rollout", "restart", "deployment/catalog"], ["rollout", "status", "deployment/catalog", "--timeout=180s"]):
                subprocess.run(["kubectl", *args, "-n", self.blip.namespace], env=env, check=True, capture_output=True)

    def blocks_on_fresh_pods(self, label, count=None, after_a_block=False):
        """Three blocks, each on new pods: the deploy's first, then a rollout restart between."""
        found = []
        for number in range(1, (count or self.blocks) + 1):
            if number > 1 or after_a_block:
                self.new_pods()
            print(f"-- {label}, block {number} of {count or self.blocks}", flush=True)
            found.append(self.one_block())
        return found

    def database_log_starts(self):
        return admin("logs", POSTGRES, "-n", self.blip.namespace, check=False).count("database system is ready to accept")

    def database_restarts(self):
        return admin("get", "pod", POSTGRES, "-n", self.blip.namespace, "-o",
                     "jsonpath={.status.containerStatuses[0].restartCount}", check=False).strip()

    def psql(self, sql):
        return admin("exec", POSTGRES, "-n", self.blip.namespace, "--", "psql", "-U", "catalog", "-d", "catalog", "-Atc", sql)

    def check_real_database(self):
        """Check 1: 24 products from Postgres; a name changed with psql shows, and goes back."""
        status, body = http_get(f"{self.staging}/api/products")
        count = json.loads(body).get("count") if status == 200 else None
        original = self.psql("SELECT name FROM products WHERE id = 'P-1001'").strip()
        try:
            self.psql("UPDATE products SET name = 'Changed by the rehearsal' WHERE id = 'P-1001'")
            shown = http_get(f"{self.staging}/api/products?q=Changed")[1]
        finally:
            self.psql(f"UPDATE products SET name = '{original}' WHERE id = 'P-1001'")
        result(count == 24 and "Changed by the rehearsal" in shown,
               f"1. /api/products answers {count} products from Postgres, and a name changed with psql shows")

    def can_i(self):
        """Check 3: yes only for the policy, in staging; video 8's answers still hold."""
        with kube.credentials(quiet=True) as kubeconfig:
            env = {**os.environ, "KUBECONFIG": str(kubeconfig)}
            ask = lambda *args: subprocess.run(["kubectl", "auth", "can-i", *args], env=env, capture_output=True,
                                               text=True).stdout.strip()
            yes = [ask("create", "networkpolicies", "-n", "staging"),
                   ask("delete", "networkpolicies/block-database", "-n", "staging"),
                   ask("get", "networkpolicies/block-database", "-n", "staging")]
            no = [ask("create", "networkpolicies", "-n", "production"), ask("create", "networkpolicies", "-n", "default"),
                  ask("create", "networkpolicies", "-A"), ask("delete", "networkpolicies/block-database", "-n", "production"),
                  ask("delete", "networkpolicies/other", "-n", "staging"), ask("delete", "networkpolicies", "-n", "staging"),
                  ask("patch", "networkpolicies/block-database", "-n", "staging")]
        result(set(yes) == {"yes"} and set(no) == {"no"},
               f"3. Jenkins may create and delete block-database in staging ({', '.join(yes)}), nothing else ({', '.join(no)})")
        before = len(self.rehearsal.results)
        self.rehearsal.can_i()
        result(all(self.rehearsal.results[before:]), "3. video 8's can-i answers still hold")
        result(agent_may_write(blip_check.LOG), f"3. the Jenkins agent's user may write {blip_check.LOG}")

    def during_a_block(self):
        """Check 2's parts that need the policy on: from a catalog pod, and production's own route."""
        report = {"connection": None, "production": [], "seen": False}
        stop = threading.Event()

        def from_a_pod():
            while not stop.is_set():
                if admin("get", "networkpolicy", blip_check.POLICY, "-n", self.blip.namespace, "--ignore-not-found", "-o", "name", check=False):
                    report["seen"] = True
                    time.sleep(5)  # past the first failed call, in the block
                    pod = admin("get", "pods", "-n", self.blip.namespace, "-l", "app.kubernetes.io/instance=catalog", "-o",
                                "jsonpath={.items[0].metadata.name}")
                    report["connection"] = admin("exec", pod, "-n", self.blip.namespace, "--", "python3", "-c", CONNECT, check=False).strip()
                    return
                time.sleep(0.5)

        def production():
            while not stop.wait(0.2):
                if report["seen"]:
                    # The pipeline's own 5 s for a page: a slow answer is still an answer (video 18's first view).
                    report["production"].append(http_get(f"{self.production}/api/products")[0])

        threads = [threading.Thread(target=from_a_pod, daemon=True), threading.Thread(target=production, daemon=True)]
        for thread in threads:
            thread.start()
        return report, stop, threads

    def abort_mid_block(self):
        """Check 13: Jenkins' abort (SIGTERM) deletes the policy; a killed process's policy goes at the next run's first step."""
        def wait_for_policy():
            for _ in range(60):
                if admin("get", "networkpolicy", blip_check.POLICY, "-n", self.blip.namespace, "--ignore-not-found", "-o", "name", check=False):
                    return True
                time.sleep(0.5)
            return False

        policy_there = lambda: admin("get", "networkpolicy", blip_check.POLICY, "-n", self.blip.namespace, "--ignore-not-found", "-o", "name", check=False)
        command = [sys.executable, str(HERE / "blip_check.py"), SERVICE, "--rehearsal"]
        run = subprocess.Popen(command, stdout=subprocess.DEVNULL)
        took = wait_for_policy()
        time.sleep(4)
        run.terminate()
        run.wait(30)
        gone = not policy_there()
        time.sleep(3)
        status = http_get(f"{self.staging}/api/products")[0]
        result(took and gone and status == 200, f"13. an aborted run deleted its policy; {self.blip.data_route} answers {status}")
        run = subprocess.Popen(command, stdout=subprocess.DEVNULL)
        took = wait_for_policy()
        run.kill()
        run.wait(30)
        left = bool(policy_there())
        blip = self.one_block()  # its first step deletes what the killed run left
        result(took and left and not policy_there() and blip["passed"],
               "13. a killed run's policy stayed on, and the next run's first step deleted it")

    def sample_memory(self):
        def current(path):
            file = Path("/sys/fs/cgroup") / path / "memory.current"
            return int(file.read_text()) >> 20 if file.exists() else 0

        while not self.sampling.wait(2):
            pods = max(current("kubepods.slice"), current("kubepods"))
            self.peak_pods = max(self.peak_pods, pods)
            self.peak_total = max(self.peak_total, pods + current("system.slice/k3s.service"))

    def catalog_peak_mib(self):
        """The highest memory any catalog pod has used, from its cgroup, or None."""
        peaks = []
        reply = json.loads(admin("get", "pods", "-n", self.blip.namespace, "-l", "app.kubernetes.io/instance=catalog", "-o", "json"))
        for pod in reply["items"]:
            uid = pod["metadata"]["uid"].replace("-", "_")
            for file in Path("/sys/fs/cgroup").glob(f"kubepods*/*/*pod{uid}*/memory.peak"):
                peaks.append(int(file.read_text()) >> 20)
        return max(peaks) if peaks else None

    def check_fits(self):
        """Check 9: the peak, from the sampler that ran through every rollout and block."""
        pods = json.loads(admin("get", "pods", "-A", "-o", "json"))["items"]
        killed = [pod["metadata"]["name"] for pod in pods for status in pod["status"].get("containerStatuses") or []
                  if (status.get("lastState", {}).get("terminated") or {}).get("reason") == "OOMKilled"]
        events = admin("get", "events", "-A", "--field-selector", "reason=Evicted", "-o", "name", check=False).split()
        events += admin("get", "events", "-A", "--field-selector", "reason=OOMKilling", "-o", "name", check=False).split()
        result(self.peak_pods < 1280 and self.peak_total < 2048 and not killed and not events and len(pods) <= 40,
               f"9. peak {self.peak_pods} MiB in pods (under 1,280), {self.peak_total} MiB with k3s (under 2,048), "
               f"{len(pods)} pods now (at most 40), OOM kills {killed or 'none'}, evictions and OOM events {len(events)}")
        peak = self.catalog_peak_mib()
        print(f"     a catalog pod's peak: {peak} MiB against the 96Mi limit. "
              f"{'If 64Mi holds, the spec keeps 64Mi: that is two lines in each chart values.yaml.' if peak and peak < 56 else 'It stays at 96Mi.'}",
              flush=True)

    def check_reproducible(self):
        """Check 15: the real pip, twice."""
        builds = []
        for _ in range(2):
            out = tempfile.mkdtemp(prefix="reproducible-")
            builds.append(build.build(SERVICE, version_dir=ROOT / "demo-states" / SERVICE / FAILING, out=out, commit=self.commit))
        result(builds[0]["sha256"] == builds[1]["sha256"] and builds[0]["image_digest"] == builds[1]["image_digest"],
               f"15. the same source builds the same SHA-256 and image digest twice, pg8000 included: {builds[0]['image_digest'][:19]}...")

    # The ladder.

    def with_liveness_period(self, seconds):
        """1.10.0 with another liveness period, in a copy of the state: the one line the spec's ladder changes."""
        folder = Path(tempfile.mkdtemp(prefix="period-")) / FAILING
        shutil.copytree(ROOT / "demo-states" / SERVICE / FAILING, folder)
        template = folder / "chart" / "templates" / "deployment.yaml"
        text, changed = re.subn(r"periodSeconds: 10\b", f"periodSeconds: {seconds}", template.read_text())
        if changed != 1:
            raise RuntimeError(f"Expected one periodSeconds: 10 in the chart, found {changed}.")
        template.write_text(text)
        # So it never takes the version's tag in JFrog, which the pipeline's chart will.
        os.environ["KUBERNETES_CHART_REPOSITORY"] = self.config["kubernetes"]["chart"] + "-rehearsal"
        try:
            package, state, _ = self.release(FAILING, folder)
            ok = self.deploy("staging", package, state, FAILING)
        finally:
            del os.environ["KUBERNETES_CHART_REPOSITORY"]
        result(ok, f"   1.10.0 with periodSeconds: {seconds} passes staging")
        return self.blocks_on_fresh_pods(f"1.10.0, liveness every {seconds} s")


# Run from a catalog pod: a new connection to the database, with its 1 s, and how long it took.
CONNECT = ("import socket, time; started = time.monotonic()\n"
           "try:\n    socket.create_connection(('catalog-postgres', 5432), 1).close(); print('connected', round(time.monotonic() - started, 2))\n"
           "except OSError as error: print('failed', round(time.monotonic() - started, 2), error.__class__.__name__)")


def measure(machine, args):
    print("== The first-day measurement: what decides whether video 25 exists", flush=True)
    problems = machine.preflight()
    if problems:
        for problem in problems:
            print(f"FAIL Not started: {problem}.", flush=True)
        return 1
    sampler = threading.Thread(target=machine.sample_memory, daemon=True)
    sampler.start()
    machine.put_on_baseline()
    data, alive = machine.blip.data_route, machine.blip.alive_route

    # The gate.
    ladder_blips = None
    if args.liveness_period:
        failing_blips = machine.with_liveness_period(args.liveness_period)
    else:
        package, state, _ = machine.release(FAILING)
        result(machine.deploy("staging", package, state, FAILING), "   1.10.0 passes staging, with the database up")
        machine.check_real_database()
        starts = machine.database_log_starts()
        failing_blips = machine.blocks_on_fresh_pods("1.10.0")
    failing = []
    print("== 4. The gate: 1.10.0, three blocks on fresh pods", flush=True)
    for number, blip in enumerate(failing_blips, 1):
        every_once, down, line = failing_block(blip)
        failing.append((every_once, down))
        result(every_once, f"4. block {number}: {line}", gate=True)
    if not args.liveness_period:
        print("-- 1.10.0, one more block on the same pods: the back-off, once", flush=True)
        again = machine.one_block()
        print(f"     the second block on used pods: {failing_block(again)[2]}", flush=True)
        result(machine.database_restarts() == "0" and machine.database_log_starts() == starts,
               f"1. through every block {POSTGRES}'s RESTARTS stayed {machine.database_restarts()} and its log shows no restart")
    if not all(ok for ok, _ in failing):
        never = [number for number, blip in enumerate(failing_blips, 1) if blip.get("problem") == "baseline"]
        if never:
            print(f"== Block {never} never started: a call failed in the 5 s before it, so this says nothing about the pods. "
                  "First suspect: the pods' CPU limit, in the CPU lines above. Fix that and run this again.", flush=True)
            return 1
        short = [blip["block_seconds"] for blip in failing_blips if (blip["block_seconds"] or 0) < blip["seconds"]]
        if short:
            print(f"== The ladder's first step: a block lasted {short} s, under 30: the arithmetic needs the whole window. Fix the timing and run this again.", flush=True)
            return 1
        print("== The ladder: every block lasted its 30 s, so 1.10.0 at periodSeconds: 5", flush=True)
        ladder_blips = machine.with_liveness_period(5)
    ladder = None
    if ladder_blips is not None:
        ladder = [failing_block(blip)[:2] for blip in ladder_blips]
        for number, blip in enumerate(ladder_blips, 1):
            result(failing_block(blip)[0], f"4. periodSeconds 5, block {number}: {failing_block(blip)[2]}", gate=True)
    stands_so_far = all(ok for ok, _ in failing) or (ladder and all(ok for ok, _ in ladder))
    if not stands_so_far:
        return finish(failing, ladder, None, failing_blips, [], args)

    # The fix.
    package, state, _ = machine.release(FIXED)
    result(machine.deploy("staging", package, state, FIXED), "   1.10.1 passes staging")
    result(machine.deploy("production", package, run_state.load(), FIXED), "   1.10.1 passes production, on its own database")
    report, stop, threads = machine.during_a_block()
    print("== 5. 1.10.1, three blocks on fresh pods", flush=True)
    first = machine.one_block()
    stop.set()
    for thread in threads:
        thread.join(10)
    fixed_blips = [first, *machine.blocks_on_fresh_pods("1.10.1", machine.blocks - 1, after_a_block=True)
                   ] if machine.blocks > 1 else [first]
    fixed = []
    for number, blip in enumerate(fixed_blips, 1):
        ok, line = fixed_block(blip, data, alive)
        fixed.append(sentence_holds(blip, data, alive))
        result(ok, f"5. block {number}: {line}")

    # Is the block real?
    apply_to_failure = -first["times"]["apply"] if first["times"]["apply"] is not None else None
    delete_to_success = None
    if first["times"]["first_success_after_delete"] is not None:
        delete_to_success = round(first["times"]["first_success_after_delete"] - first["times"]["delete_returned"], 2)
    connection = report["connection"] or "not tried"
    result(connection.startswith("failed") and float(connection.split()[1]) <= 1.1,
           f"2. the policy is enforced on this node: a new connection from a catalog pod {connection}")
    result(apply_to_failure is not None and apply_to_failure <= APPLY_WITHIN_SECONDS
           and delete_to_success is not None and delete_to_success <= BACK_WITHIN_SECONDS,
           f"2. first failed call {apply_to_failure} s after the apply (at most {APPLY_WITHIN_SECONDS}), "
           f"first success {delete_to_success} s after the delete (at most {BACK_WITHIN_SECONDS})")
    production = report["production"]
    result(bool(production) and set(production) == {200},
           f"2. production's {data} answered 200 through staging's block: {len(production)} calls, others {set(production) - {200} or 'none'}")

    # The rest of what a rehearsal can check.
    machine.can_i()
    machine.abort_mid_block()
    machine.check_reproducible()
    machine.sampling.set()
    sampler.join(5)
    machine.check_fits()
    return finish(failing, ladder, fixed, failing_blips, fixed_blips, args)


def finish(failing, ladder, fixed, failing_blips, fixed_blips, args):
    """Prints the brackets and the verdict. Returns the exit code."""
    stands, lines = verdict(failing, ladder, fixed, args.liveness_period)
    print("== What fills the script's brackets", flush=True)
    for line in brackets(failing_blips, fixed_blips):
        print(f"   {line}", flush=True)
    others = [what for ok, what, gate in results if not ok and not gate]
    passed = sum(ok for ok, _, _ in results)
    print(f"== Verdict, {passed} PASS and {len(results) - passed} FAIL", flush=True)
    for line in lines:
        print(f"   {line}", flush=True)
    if stands and others:
        print(f"   The block's result stands, but fix these before a take: {len(others)} FAIL above, first: {others[0][:80]}", flush=True)
    return (0 if not others else 2) if stands else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--here", action="store_true", help="run here: the tools host")
    parser.add_argument("--follow", action="store_true", help="from the Mac: show the run on the tools host")
    parser.add_argument("--blocks", type=int, default=BLOCKS, help="blocks per version")
    parser.add_argument("--liveness-period", type=int, help="measure 1.10.0 with this liveness period at once")
    args = parser.parse_args()
    shared_host = loaded("shared_host", SCRIPTS / "host.py")
    if not args.here and not shared_host.on_host():
        forwarded = ["--blocks", str(args.blocks), *(["--liveness-period", str(args.liveness_period)] if args.liveness_period else [])]
        return shared_host.hand_over("blip-rehearse", "demos/database-blip/rehearse.py", forwarded, follow_only=args.follow,
                                     title="video 25 first-day measurement")
    sys.stdout.reconfigure(line_buffering=True)
    started = time.monotonic()
    commit = git("rev-parse", "HEAD", check=False).stdout.strip() or "rehearsal"
    machine = Machine(commit, args.blocks)
    try:
        code = measure(machine, args)
    finally:
        machine.sampling.set()
        if machine.changed_the_cluster:
            print(f"== Back on {BASELINE}", flush=True)
            machine.put_on_baseline()
    print(f"Done in {kube.clock(time.monotonic() - started)}. Exit code {code}.", flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
