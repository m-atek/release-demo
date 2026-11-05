"""Put video 32 back where a take starts: no policy, catalog 1.8.2 on both namespaces, 1.9.1 staged.

    python scripts/reset.py one-wrong-context            from the Mac
    python scripts/reset.py one-wrong-context --dry-run  every action it would take, and nothing else

From the Mac it hands itself over to the tools host, like reset.py. It refuses
while production runs anything but catalog, or while video 33's forecast fills
the node: it reads those and leaves them alone. As root, for k3s's admin
kubeconfig, so not from the Jenkins reset job. Steps:

1. the take's release and rollback runs stopped;
2. the ValidatingAdmissionPolicyBinding no-zero and the ValidatingAdmissionPolicy
   no-zero deleted, if there: the only things it deletes;
3. open catalog changes cancelled, closed ones kept;
4. catalog scaled back to 3 pods in any namespace a take left short, before Helm:
   Helm 4 applies server-side, and a count kubectl set to another value is a conflict;
5. both namespaces on 1.8.2, through the same Helm deploy as a release, where they aren't;
6. 1.9.1 staged on main, the take's release;
7. the Slack handler warmed.

Like video 8's and video 26's, it doesn't reopen ORD-90 or un-release "catalog
1.9.1": the tag step says "already released" on a repeat, as build #104 did on 2
November. Nothing here touches Slack's messages. Under two minutes, safe to run twice.
"""

import argparse
import importlib.util
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parents[1]
# After the standard library: scripts/secrets.py would shadow its secrets (3a2057e).
sys.path.append(str(SCRIPTS))
sys.path.append(str(SCRIPTS / "demos" / "kubernetes"))  # last: it has its own build, deploy and check

import host  # noqa: E402
import kube  # noqa: E402
import on_host  # noqa: E402
from common import ROOT, git, service  # noqa: E402

SERVICE = "catalog"
BASELINE = "1.8.2"
TAKE = "1.9.1"
RESET = str(SCRIPTS / "demos" / "kubernetes" / "reset.py")


def shared_reset():
    """scripts/reset.py as a module, its steps pointed at catalog 1.8.2: this file has its name."""
    spec = importlib.util.spec_from_file_location("shared_reset", SCRIPTS / "reset.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.SERVICE, module.BASELINE = SERVICE, BASELINE
    return module


def remove_policy(reset):
    if on_host.policy_on() or on_host.binding_on():
        reset.act(f"delete the binding {on_host.BINDING} and the policy {on_host.POLICY}", on_host.remove_policy)
    else:
        print(f"  no policy {on_host.POLICY}")


def restore(reset):
    short = on_host.short_of_pods()
    if not short:
        print(f"  {SERVICE} has its pods in both namespaces")
    for namespace, now in short.items():
        reset.act(f"scale {SERVICE} in {namespace} from {now} to {on_host.REPLICAS[namespace]} pods",
                  lambda namespace=namespace: on_host.restore(namespace))


def elsewhere(kubeconfig, api, config):
    """The namespaces that aren't on 1.8.2 with every desired pod ready and nothing pending."""
    release = config["kubernetes"]["release"]
    found = []
    for environment, env in config["environments"].items():
        namespace = env["namespace"]
        revisions = kube.history(kubeconfig, release, namespace)
        current = kube.deployed(revisions)
        pods = [kube.pod_summary(pod) for pod in api.pods(namespace, release)]
        ready = [pod for pod in pods if pod[0] == BASELINE and pod[1] and not pod[2]]
        steady = (revisions and not revisions[-1]["status"].startswith("pending") and current
                  and current["app_version"] == BASELINE and len(ready) == env["replicas"] == len(pods))
        print(f"  {namespace}: {'on ' + BASELINE if steady else 'going to ' + BASELINE}")
        if not steady:
            found.append(namespace)
    return found


def put_on_baseline():
    """Video 8's reset: both namespaces at once, through deploy.py's Helm deploy."""
    commit = git("log", "-1", "--format=%H", "--", f"demo-states/{SERVICE}/{BASELINE}", check=False).stdout.strip()
    subprocess.run([sys.executable, RESET, "--here", "--baseline", BASELINE, "--commit", commit or "baseline"],
                   cwd=ROOT, check=True)


def baseline(reset):
    config = service(SERVICE)
    with kube.credentials(quiet=True) as kubeconfig:
        found = elsewhere(kubeconfig, kube.Api(kubeconfig), config)
    if found:
        reset.act(f"put {' and '.join(found)} on {BASELINE}, through Helm", put_on_baseline)


def stage(reset):
    current = host.main_version(service(SERVICE)["path"])
    if current == TAKE:
        print(f"  main has {SERVICE} {TAKE}")
        return
    reset.act(f"stage {SERVICE} {TAKE} on main (main has {current})", lambda: host.stage(SERVICE, TAKE))


def run(args):
    started = time.monotonic()
    print(f"Reset video 32: no policy, {SERVICE} {BASELINE} in staging and production, {TAKE} staged"
          f"{' (dry run: nothing changes)' if args.dry_run else ''}.")
    refused = on_host.refusal()
    if refused:
        print(f"Refused: {refused}.")
        return 1
    shared = shared_reset()
    reset = shared.Reset(args.dry_run)
    try:
        reset.step("1. Release and rollback runs in Jenkins", lambda: shared.stop_runs(reset, shared.release_jobs()))
    except shared.HistoryRunning as error:
        print(f"  {error}")
        return 1
    reset.step(f"2. The policy {on_host.POLICY}", lambda: remove_policy(reset))
    reset.step("3. Open changes in ServiceNow", lambda: shared.cancel_changes(reset))
    reset.step(f"4. {SERVICE}'s pods, where a take left them short", lambda: restore(reset))
    reset.step(f"5. Both namespaces on {BASELINE}", lambda: baseline(reset))
    reset.step(f"6. {SERVICE} {TAKE} on main", lambda: stage(reset))
    reset.step("7. The Slack handler, one call", lambda: shared.warm_slack_handler(reset, args.slack_handler_url))
    took = time.monotonic() - started
    if reset.failed:
        print(f"Finished in {took:.0f}s, but these steps failed: {'; '.join(reset.failed)}. Fix them and run it again.")
        return 1
    print(f"{'Dry run' if args.dry_run else 'Reset'} in {took:.0f}s: {reset.actions} action(s). "
          f"{'' if args.dry_run else 'Ready for a take.'}".rstrip())
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="print every action, change nothing")
    parser.add_argument("--here", action="store_true", help="run here, not on the tools host")
    parser.add_argument("--slack-handler-url", help="the Slack handler's function URL; from the Mac, looked up")
    args = parser.parse_args()
    if not args.here and not host.on_host():
        url = args.slack_handler_url or shared_reset().slack_handler_url()
        shown = ["--dry-run"] if args.dry_run else []
        return host.hand_over("reset", "demos/one_wrong_context/reset.py",
                              [*shown, *(["--slack-handler-url", url] if url else [])],
                              title=" ".join(["reset.py one-wrong-context", *shown]))
    sys.stdout.reconfigure(line_buffering=True)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
