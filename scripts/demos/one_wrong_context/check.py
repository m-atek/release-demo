"""Video 32's first-day check: does the policy really refuse kubectl scale on this cluster?

    ~/Code/release-demo/.venv/bin/python scripts/demos/one_wrong_context/check.py    about 5 minutes

From the Mac, with the same kubectl, certificate and tunnel the video films: start
video 8's connect.py in another terminal first. It needs no merge: it runs from a
worktree, and sends its two small files through video 8's host.py. It needs no
Slack, Jira, ServiceNow or Jenkins.

It reads Kubernetes' source at v1.36.5 into two predictions, and tests both:

- A rule that lists only `deployments` doesn't match `kubectl scale`, which goes
  through the scale subresource (rules.go). The rule has to list `deployments/scale`.
- A scale to zero reaches the policy as an autoscaling/v1 Scale, whose `replicas`
  is omitempty, so at zero the field isn't there and `object.spec.replicas > 0`
  stops with "no such key" (types.go, converter.go). The file's expression says
  what a missing field means.

What it does, dry runs wherever it can (a server-side dry run goes through admission
and writes nothing):

1. Without a policy: the video's command passes, as a dry run.
2. Two wrong versions, each as no-zero-check and each gone again: the rule without
   /scale, and the expression without the guard. They fill the Rules, and decide nothing.
3. no-zero.yaml applied. The dry run refused, in the policy's own words. Then the
   video's real command, no dry run: refused, the same three pods. The audit log's row
   says patch on deployments/scale, 403, by presenter: the subresource, and not a guess.
4. What it doesn't refuse, and who it stops: staging, a scale to one, a delete of the
   Deployment and of the policy itself pass as dry runs; a patch to zero and the same
   command as k3s's own admin are refused.
5. Off camera, with the policy off, a real scale to zero under real calls, as take 1
   will do it: when calls fail, when the pods are gone, what the events say, the real
   scale back, when calls answer again. It also asks the API, by dry run, whether an
   apply of 3 replicas conflicts with what kubectl did, at zero and after.

Whatever happens it deletes what it made, the policy with it, and scales catalog
back to 3 pods. Those deletions are all it deletes. It prints one PASS or FAIL line
per check, what fills the script's brackets, and a verdict:

    0  THE VIDEO STANDS.
    1  THE VIDEO DOESN'T STAND. Don't film another command in its place.
    2  NOT YET. It says what to fix, and the check runs again.
"""

import collections
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.append(str(HERE.parents[1]))
import contexts  # noqa: E402
import remote  # noqa: E402

POLICY_FILE = HERE / "no-zero.yaml"
POLICY = "no-zero"
CHECK_POLICY = "no-zero-check"
DEPLOYMENT = "catalog"
REPLICAS = 3
OWN_WORDS = "denied request: Production doesn't go to zero."
THE_COMMAND = ("scale", "deploy", "--all", "--replicas=0")
ZERO_PATCH = '{"spec":{"replicas":0}}'
ADMIN = "/etc/rancher/k3s/k3s.yaml"
LIVE_SECONDS = 20       # the API server reloads policies every second; this is well past it
CALLS_SECONDS = 100     # calls during the real run: the baseline, the outage, the way back, the quiet after
BASELINE_SECONDS = 10
GONE_SECONDS = 45
READY_SECONDS = 90
AUDIT_SECONDS = 15
EVENT = re.compile(r"Scaled down replica set \S+ from (\d+) to 0")
APPLY = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": DEPLOYMENT, "namespace": "production"},
         "spec": {"replicas": REPLICAS}}

# text holds stdout and stderr, for a refusal's words. out holds stdout alone, for JSON: kubectl version warns on stderr.
Reply = collections.namedtuple("Reply", "code text out", defaults=(None,))


def parsed(reply):
    return json.loads(reply.text if reply.out is None else reply.out)


def seconds(value):
    return "not read" if value is None else f"{value:.1f} s"


class Cluster:
    """kubectl on the Mac with the contexts' kubeconfig, as the video's terminal has it, and the host through SSM."""

    def __init__(self, kubeconfig=None):
        self.kubeconfig = kubeconfig

    def kubectl(self, *args, stdin=None, timeout=60):
        env = {**os.environ, "KUBECONFIG": str(self.kubeconfig)}
        try:
            result = subprocess.run(["kubectl", *args], env=env, input=stdin, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return Reply(124, f"no answer after {timeout}s")
        return Reply(result.returncode, (result.stdout + result.stderr).strip(), result.stdout)

    def host(self, command, timeout=120):
        return Reply(*remote.run(command, timeout))

    def calls(self, seconds):
        """Starts calls.py on the host. The handle's result() waits for its end: SSM returns the output then."""
        return Calls(self, seconds)


class Calls:
    def __init__(self, cluster, seconds):
        self.reply = None

        def work():
            self.reply = cluster.host(f"{remote.CALLS} --seconds {seconds}", timeout=seconds + 60)

        self.thread = threading.Thread(target=work)
        self.thread.start()

    def result(self):
        self.thread.join()
        return self.reply


class Check:
    def __init__(self, cluster, out=print, clock=time.time, sleep=time.sleep):
        self.cluster, self.out, self.clock, self.sleep = cluster, out, clock, sleep
        self.findings = {}
        self.fails = []
        self.notes = []

    def line(self, ok, what):
        self.out(f"{'PASS' if ok else 'FAIL'} {what}")
        if not ok:
            self.fails.append(what)
        return ok

    def kubectl(self, *args, **options):
        return self.cluster.kubectl(*args, **options)

    def production(self, *args, **options):
        return self.kubectl("--context", "production", *args, **options)

    def staging(self, *args, **options):
        return self.kubectl("--context", "staging", *args, **options)

    # What the cluster reads.

    def deployment(self):
        reply = self.production("get", "deploy", DEPLOYMENT, "-o", "json")
        return parsed(reply) if reply.code == 0 else {}

    def pods(self):
        """{pod: restarts} of what's in production."""
        reply = self.production("get", "pods", "-o", "json")
        items = parsed(reply)["items"] if reply.code == 0 else []
        return {item["metadata"]["name"]: sum(status.get("restartCount", 0) for status in
                                              item.get("status", {}).get("containerStatuses") or [])
                for item in items}

    def wanted_and_ready(self):
        document = self.deployment()
        return document.get("spec", {}).get("replicas"), document.get("status", {}).get("readyReplicas", 0)

    def dry_scale(self, *where):
        return self.production(*THE_COMMAND, "--dry-run=server", *where)

    def dry_patch(self):
        return self.production("patch", "deploy", DEPLOYMENT, "-p", ZERO_PATCH, "--dry-run=server")

    # Policies.

    def apply(self, documents):
        return self.kubectl("--context", "production", "apply", "-f", "-", stdin=yaml.safe_dump_all(documents))

    def delete_policy(self, name):
        for kind in ("validatingadmissionpolicybinding", "validatingadmissionpolicy"):
            self.kubectl("--context", "production", "delete", kind, name, "--ignore-not-found")

    def wait_until(self, test, seconds):
        """Seconds until test passes, or None."""
        started = self.clock()
        while self.clock() - started < seconds:
            if test():
                return self.clock() - started
            self.sleep(0.5)
        return None

    def variant(self, change):
        """no-zero.yaml under another name, with one thing changed."""
        policy, binding = yaml.safe_load_all(POLICY_FILE.read_text())
        policy["metadata"]["name"] = binding["metadata"]["name"] = binding["spec"]["policyName"] = CHECK_POLICY
        change(policy)
        return [policy, binding]

    def preflight(self):
        reply = self.production("version", "-o", "json")
        if reply.code != 0:
            self.findings["no_answer"] = reply.text
            self.line(False, f"The cluster doesn't answer: {reply.text[:120]}. Is connect.py running?")
            return False
        version = parsed(reply)
        self.findings["kubectl"] = version.get("clientVersion", {}).get("gitVersion", "?")
        self.findings["server"] = version.get("serverVersion", {}).get("gitVersion", "?")
        self.out(f"     kubectl {self.findings['kubectl']}, server {self.findings['server']}")
        apis = self.production("api-resources", "--api-group=admissionregistration.k8s.io", "-o", "name")
        if not self.line("validatingadmissionpolicies" in apis.text, "the cluster serves ValidatingAdmissionPolicy"):
            self.findings["no_api"] = True
            return False
        others = [name for name in self.production("get", "deploy,statefulset,daemonset", "-o", "name").text.split()
                  if name != f"deployment.apps/{DEPLOYMENT}"]
        wanted, ready = self.wanted_and_ready()
        ok = self.line(not others and wanted == REPLICAS and ready == REPLICAS,
                       f"production holds catalog alone, {ready} of {wanted} ready, others: {others or 'none'}")
        if not ok:
            self.findings["not_ready"] = True
            self.out("     Run scripts/reset.py one-wrong-context, or bring the other demo down.")
        return ok

    def control(self):
        reply = self.dry_scale()
        self.findings["control"] = reply.code == 0
        return self.line(reply.code == 0, f"1. no policy: the video's command passes as a dry run ({reply.text[:60]})")

    def wrong_versions(self):
        """Neither decides the video. Each fills a sentence in the Rules, or takes it away."""
        def only_deployments(policy):
            policy["spec"]["matchConstraints"]["resourceRules"][0]["resources"] = ["deployments"]

        def no_guard(policy):
            policy["spec"]["validations"][0]["expression"] = "object.spec.replicas > 0"

        self.apply(self.variant(only_deployments))
        live = self.wait_until(lambda: self.dry_patch().code != 0, LIVE_SECONDS)
        passes = self.dry_scale().code == 0
        self.findings["only_deployments_walks_past"] = bool(live is not None and passes)
        self.out(f"     2. a rule with only deployments: a patch to zero {'refused' if live is not None else 'NOT refused'}, "
                 f"kubectl scale {'passes' if passes else 'refused'}")
        self.delete_policy(CHECK_POLICY)
        self.wait_until(lambda: self.dry_patch().code == 0, LIVE_SECONDS)

        self.apply(self.variant(no_guard))
        self.wait_until(lambda: self.dry_scale().code != 0, LIVE_SECONDS)
        words = self.dry_scale().text
        self.findings["no_guard_says"] = words
        self.out(f"     2. an expression with no guard: {words}")
        self.delete_policy(CHECK_POLICY)
        self.wait_until(lambda: self.dry_scale().code == 0, LIVE_SECONDS)

    def the_file(self):
        """The check that decides the video. Returns False if nothing further can tell us more."""
        applied = self.production("apply", "-f", str(POLICY_FILE))
        if not self.line(applied.code == 0, f"3. no-zero.yaml applied ({applied.text[:300]})"):
            self.findings["not_accepted"] = applied.text
            return False
        live = self.wait_until(lambda: self.dry_scale().code != 0, LIVE_SECONDS)
        self.findings["caught"] = live is not None
        if live is None:
            self.line(False, f"3. kubectl scale is NOT refused, {LIVE_SECONDS} s after the apply")
            self.delete_policy(POLICY)
            self.star()
            return False
        self.findings["live_after"] = live
        words = self.dry_scale().text
        self.findings["words_dry"] = words
        own = OWN_WORDS in words and "(Forbidden)" in words and "resulted in error" not in words
        self.findings["own_words"] = own
        self.line(own, f"3. refused in its own words, as a dry run: {words}")

        before = self.pods()
        self.kubectl("config", "use-context", "production")
        started = self.clock()
        real = self.kubectl(*THE_COMMAND)
        self.findings["refusal_seconds"] = self.clock() - started
        self.findings["real_refused"] = real.code != 0
        self.findings["words_real"] = real.text
        self.line(real.code != 0 and OWN_WORDS in real.text, f"3. the real command, no dry run: {real.text}")
        wanted, _ = self.wanted_and_ready()
        after = self.pods()
        unmoved = wanted == REPLICAS and after == before
        self.findings["unmoved"] = unmoved
        self.line(unmoved, f"3. no pod moved: {wanted} wanted, the same {len(after)} pods, the same restarts")
        self.audit(started)
        return True

    def star(self):
        """The rule's other documented form, once. If this catches it, the file changes by one word."""
        def every_scale(policy):
            policy["spec"]["matchConstraints"]["resourceRules"][0]["resources"] = ["deployments", "*/scale"]

        self.apply(self.variant(every_scale))
        live = self.wait_until(lambda: self.dry_scale().code != 0, LIVE_SECONDS)
        self.findings["star_catches"] = live is not None
        self.out(f"     3. with */scale in the rule instead: kubectl scale "
                 f"{'refused' if live is not None else 'still NOT refused'}")
        self.delete_policy(CHECK_POLICY)

    def audit(self, since):
        """The API server's own record of what kubectl's request was, and what it got."""
        moment = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(since - 2))
        rows, started = [], self.clock()
        while not rows and self.clock() - started < AUDIT_SECONDS:
            reply = self.cluster.host(f"{remote.K3S} audit --since {moment} --namespace production --name {DEPLOYMENT}",
                                      timeout=60)
            rows = [json.loads(line) for line in reply.text.splitlines() if line.startswith("{")]
            rows = [row for row in rows if row.get("subresource") == "scale" and row.get("code") != 200]
            if not rows:
                self.sleep(2)
        row = rows[-1] if rows else {}
        self.findings["audit"] = row
        self.line(bool(row) and row.get("verb") == "patch" and row.get("resource") == "deployments"
                  and row.get("code") == 403 and row.get("user") == "presenter",
                  f"3. the audit log: {row.get('code')} {row.get('verb')} {row.get('resource')}/{row.get('subresource')} "
                  f"{row.get('name')} by {row.get('user')}")

    def scope(self):
        """Only zero, only production. And what it doesn't stop, said plainly."""
        checks = [
            ("the same command in staging passes", self.staging(*THE_COMMAND, "--dry-run=server").code == 0),
            ("a scale to one in production passes",
             self.production("scale", "deploy", "--all", "--replicas=1", "--dry-run=server").code == 0),
            ("a patch to zero in production is refused too", self.dry_patch().code != 0),
            ("k3s's own admin, system:masters, is refused too",
             self.cluster.host(f"KUBECONFIG={ADMIN} kubectl -n production {' '.join(THE_COMMAND)} --dry-run=server",
                               timeout=60).code != 0),
            ("the limit: a delete of the Deployment passes",
             self.production("delete", "deploy", DEPLOYMENT, "--dry-run=server").code == 0),
            ("the limit: the admin can delete the policy",
             self.production("delete", "validatingadmissionpolicy", POLICY, "--dry-run=server").code == 0),
        ]
        for what, ok in checks:
            self.line(ok, f"4. {what}")
        self.findings["scope"] = all(ok for _, ok in checks)

    def conflict(self):
        """What an apply of the chart's count meets now: Helm 4 applies server-side, without --force-conflicts."""
        reply = self.production("apply", "--server-side", "--field-manager=replicas-check", "--dry-run=server", "-f", "-",
                                stdin=yaml.safe_dump(APPLY))
        return reply.text if reply.code != 0 else ""

    def real_run(self):
        """Take 1, off camera. Policy off."""
        self.delete_policy(POLICY)
        self.wait_until(lambda: self.dry_scale().code == 0, LIVE_SECONDS)
        wanted, ready = self.wanted_and_ready()
        self.line(wanted == REPLICAS and ready == REPLICAS, f"5. before the real run: {ready} of {wanted} ready, policy off")

        calls = self.cluster.calls(CALLS_SECONDS)
        self.sleep(BASELINE_SECONDS)
        self.kubectl("config", "use-context", "production")
        enter = self.clock()
        scaled = self.kubectl(*THE_COMMAND)
        self.findings["scaled"] = scaled.code == 0
        self.line(scaled.code == 0, f"5. the real scale to zero: {scaled.text}")
        gone = self.wait_until(lambda: "No resources found" in self.production("get", "pods").text, GONE_SECONDS)
        self.findings["gone_after"] = None if gone is None else self.clock() - enter
        self.line(gone is not None, f"5. no pods left in production, {seconds(self.findings['gone_after'])} after Enter")

        self.findings["conflict_at_zero"] = self.conflict()
        self.out(f"     5. an apply of {REPLICAS} replicas at zero: {self.findings['conflict_at_zero'][:200] or 'no conflict'}")

        described = self.production("describe", "deploy", DEPLOYMENT).text
        event = EVENT.findall(described)
        self.findings["event"] = event[-1] if event else None
        self.line(bool(event) and event[-1] == str(REPLICAS), f"5. the events still say from {self.findings['event']} to 0")

        back = self.kubectl("scale", "deploy", "--all", f"--replicas={REPLICAS}")
        self.findings["scaled_back"] = back.code == 0
        self.line(back.code == 0, f"5. the real scale back: {back.text}")
        ready_after = self.wait_until(lambda: self.wanted_and_ready()[1] == REPLICAS, READY_SECONDS)
        self.findings["ready_after"] = None if ready_after is None else self.clock() - enter
        self.line(ready_after is not None,
                  f"5. {REPLICAS} of {REPLICAS} ready, {seconds(self.findings['ready_after'])} after Enter")
        self.findings["conflict_at_three"] = self.conflict()
        self.line(not self.findings["conflict_at_three"], f"5. an apply of {REPLICAS} replicas now: "
                  f"{self.findings['conflict_at_three'][:200] or 'no conflict, so the next release can deploy'}")

        self.read_calls(calls.result(), enter)

    def read_calls(self, reply, enter):
        match = re.search(r"^== summary (\{.*\})$", reply.text, re.M)
        summary = json.loads(match.group(1)) if match else {}
        self.findings["calls"] = summary
        failed = summary.get("failed", 0)
        first, last = summary.get("first_failed"), summary.get("last_failed")
        said = "Answering again" in reply.text
        ok = bool(failed) and first is not None and first >= enter and said
        self.line(ok, f"5. the calls: {failed} of {summary.get('answered', 0) + failed} failed, "
                  f"none before Enter: {first is not None and first >= enter}, and the window says they're back: {said}")
        if first is not None:
            self.findings["first_failed_after"] = first - enter
            self.findings["back_after"] = last - enter

    def clean_up(self):
        """Whatever happened."""
        self.delete_policy(CHECK_POLICY)
        self.delete_policy(POLICY)
        wanted, _ = self.wanted_and_ready()
        if wanted is not None and wanted < REPLICAS:
            self.production("scale", "deploy", DEPLOYMENT, f"--replicas={REPLICAS}")
            self.out(f"     catalog back on {REPLICAS} pods.")
        self.kubectl("config", "use-context", "staging")

    def brackets(self):
        found = self.findings

        def rounded(key):
            return seconds(found.get(key))

        self.out("== What fills the script's brackets. The take's own numbers replace these.")
        self.out(f"   the policy is live after the apply: {rounded('live_after')}. The take holds 5 s, off camera a dry run first.")
        self.out(f"   Enter to the refusal, through the tunnel: {rounded('refusal_seconds')}")
        self.out(f"   Enter to the first failed call: {rounded('first_failed_after')}")
        self.out(f"   Enter to no pods: {rounded('gone_after')}")
        self.out(f"   Enter to the last failed call: {rounded('back_after')}")
        self.out(f"   Enter to all ready: {rounded('ready_after')}")
        self.out(f"   failed calls: {found.get('calls', {}).get('failed', 'not read')}")
        self.out(f"   a rule with only deployments lets kubectl scale past: {found.get('only_deployments_walks_past', 'not read')}")

    def verdict(self):
        """(exit code, what it means). Only the first five lines decide whether there's a video at all."""
        found = self.findings
        if found.get("no_answer") or found.get("not_ready"):
            return 2, "NOT YET: the check never reached the question. Fix what it says, and run it again."
        if found.get("no_api"):
            return 1, "THE VIDEO DOESN'T STAND: this cluster doesn't serve the policy."
        if not found.get("control"):
            return 2, ("NOT YET: the command is refused with no policy on, so a refusal would prove nothing. "
                       "Find out what refuses it.")
        if found.get("not_accepted"):
            return 2, ("NOT YET: the API server didn't accept no-zero.yaml. If it's the expression, "
                       "has(object.spec.replicas) && object.spec.replicas > 0 says the same. Fix it, and run this again.")
        if not found.get("caught"):
            if found.get("star_catches"):
                return 2, ("NOT YET: */scale catches it where deployments/scale doesn't. "
                           "Change that one word in no-zero.yaml and its test, and run it again.")
            return 1, ("THE VIDEO DOESN'T STAND: kubectl scale gets past the policy, with deployments/scale and with */scale. "
                       "Don't film kubectl patch in its place: it wouldn't be the same command, and the same command is the video.")
        if not found.get("real_refused"):
            return 1, "THE VIDEO DOESN'T STAND: the dry run was refused, and the real command wasn't."
        if not found.get("own_words"):
            return 2, ("NOT YET: it's refused, but not in the policy's own words. Fix the expression "
                       "(has(object.spec.replicas) && object.spec.replicas > 0 is the other form), and run it again.")
        if self.fails:
            return 2, "THE POLICY HOLDS, and the video can stand, but fix these before a take: " + "; ".join(self.fails)
        return 0, "THE VIDEO STANDS."

    def run(self):
        try:
            if self.preflight() and self.control():
                self.wrong_versions()
                if self.the_file():
                    self.scope()
                    self.real_run()
        finally:
            self.clean_up()
        self.brackets()
        code, words = self.verdict()
        self.out(f"== Verdict, exit {code}")
        self.out(f"   {words}")
        return code


def main():
    copy = contexts.write()
    return Check(Cluster(copy)).run()


if __name__ == "__main__":
    sys.exit(main())
