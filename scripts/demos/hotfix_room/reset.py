"""Put video 33 back where a take starts: catalog 1.12.0, forecast holding the node's memory, both fixes staged.

    python scripts/reset.py hotfix-room             from the Mac
    python scripts/reset.py hotfix-room --dry-run   every action it would take, and nothing else

From the Mac it hands itself over to the tools host, like reset.py; the reset job
runs it there with DEMO=hotfix-room. It refuses unless scripts/demo.sh up hotfix-room
has run, and while video 25's Postgres is up, whose requests leave forecast no room.
Steps:

1. the take's release and rollback runs stopped;
2. the main reset's steps for what the takes made, for each service: open changes
   cancelled, tags after the baseline and their GitHub releases archived under
   takes/, JFrog release copies archived, Jira versions un-released and ORD-[h] and
   ORD-[r] reopened, their comments kept;
3. catalog on 1.12.0 in staging and production, through the same Helm deploy as a
   release. If that has to move while forecast 2.4.0 holds the room, forecast's Helm
   release goes first: nothing else can make room, and step 4 puts it back;
4. forecast on 2.4.0, through the same Helm deploy, last, because it fills the node;
5. the node read: there must be less free to reserve than catalog's next pod asks
   for, or the hotfix would simply start;
6. catalog 1.12.1 and forecast 2.4.1 staged on main, in that order, so all three
   releases of a take build from the same commit;
7. the Slack handler warmed.

Nothing here touches Slack's messages. Under three minutes, safe to run twice.
"""

import argparse
import importlib.util
import os
import subprocess
import sys
import time
import urllib.error
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parents[1]
KUBERNETES = SCRIPTS / "demos" / "kubernetes"
sys.path.insert(0, str(HERE))
sys.path.append(str(SCRIPTS))  # scripts/, for host and common, before the Kubernetes demo's own host.py
sys.path.append(str(KUBERNETES))

import host  # noqa: E402
import kube  # noqa: E402
import room  # noqa: E402
from common import ROOT, git, service  # noqa: E402

CATALOG, FORECAST = "catalog", "forecast"
BASELINES = {CATALOG: "1.12.0", FORECAST: "2.4.0"}
TAKES = {CATALOG: "1.12.1", FORECAST: "2.4.1"}
RESET = str(KUBERNETES / "reset.py")
ADMIN = "/etc/rancher/k3s/k3s.yaml"
POSTGRES = "catalog-postgres-0"


def shared_reset(name):
    """scripts/reset.py as a module, its steps pointed at one service and its baseline: this file has its name."""
    spec = importlib.util.spec_from_file_location(f"shared_reset_{name}", SCRIPTS / "reset.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.SERVICE, module.BASELINE = name, BASELINES[name]
    return module


def take_issues():
    from jira import Jira
    from setup import HOTFIX_SUMMARY, ROOM_SUMMARY, found_issue

    jira = Jira()
    found = {CATALOG: found_issue(jira, HOTFIX_SUMMARY), FORECAST: found_issue(jira, ROOM_SUMMARY)}
    if not all(found.values()):
        raise RuntimeError("No Jira issues for the takes yet: run scripts/demos/hotfix_room/setup.py once.")
    return found


def namespaces(config):
    return {name: environment["namespace"] for name, environment in config["environments"].items()}


def refusal(api):
    """Why a take can't start from here. None when it can."""
    for namespace in namespaces(service(CATALOG)).values():
        if api.get(f"/api/v1/namespaces/{namespace}/pods/{POSTGRES}"):
            return (f"{POSTGRES} runs in {namespace}, and its 64 MiB leaves forecast's pods no room: "
                    "scripts/demo.sh down database-blip")
    try:
        api.pods(room.FORECAST, FORECAST)
    except urllib.error.HTTPError as error:
        if error.code == 403:  # no namespace, or no binding in it: the pipeline may read nothing there
            return f"The pipeline has no rights in a namespace {room.FORECAST}: scripts/demo.sh up hotfix-room"
        raise
    return None


def not_on(kubeconfig, api, name, version):
    """The namespaces of a service that aren't on this version with every desired pod ready and nothing pending."""
    config = service(name)
    release = config["kubernetes"]["release"]
    elsewhere = []
    for environment, namespace in namespaces(config).items():
        revisions = kube.history(kubeconfig, release, namespace)
        current = kube.deployed(revisions)
        pods = [kube.pod_summary(pod) for pod in api.pods(namespace, release)]
        ready = [pod for pod in pods if pod[0] == version and pod[1] and not pod[2]]
        steady = (revisions and not revisions[-1]["status"].startswith("pending") and current
                  and current["app_version"] == version
                  and len(ready) == config["environments"][environment]["replicas"] == len(pods))
        print(f"  {namespace}: {'on ' + version if steady else 'going to ' + version}")
        if not steady:
            elsewhere.append(namespace)
    return elsewhere


def put_on(name, version):
    """Video 8's reset for this service and version, through deploy.py's Helm deploy, on the host."""
    commit = git("log", "-1", "--format=%H", "--", f"demo-states/{name}/{version}", check=False).stdout.strip()
    subprocess.run([sys.executable, RESET, "--here", "--service", name, "--baseline", version, "--commit", commit or "baseline"],
                   cwd=ROOT, check=True)


def make_room(kubeconfig):
    """forecast's Helm release out of the way, so catalog can roll. Nothing in it holds data, and step 4 reinstalls it."""
    result = kube.helm(kubeconfig, "uninstall", FORECAST, "--namespace", room.FORECAST, "--wait", "--timeout", "2m")
    if result.returncode != 0:
        raise RuntimeError(f"helm uninstall {FORECAST}: {result.stderr.strip()[:200]}")


def holds_the_room(api):
    """forecast is on the version that fills the node."""
    return any(kube.pod_summary(pod)[0] == BASELINES[FORECAST] for pod in api.pods(room.FORECAST, FORECAST))


def cluster(reset, kubeconfig, api):
    catalog_moves = not_on(kubeconfig, api, CATALOG, BASELINES[CATALOG])
    forecast_moves = not_on(kubeconfig, api, FORECAST, BASELINES[FORECAST])
    if catalog_moves and holds_the_room(api):
        reset.act(f"take forecast {BASELINES[FORECAST]}'s Helm release out, so catalog can roll: the next step but one "
                  "puts it back", lambda: make_room(kubeconfig))
        forecast_moves = [room.FORECAST]
    if catalog_moves:
        reset.act(f"put {' and '.join(catalog_moves)} on catalog {BASELINES[CATALOG]}, through Helm",
                  lambda: put_on(CATALOG, BASELINES[CATALOG]))
    if forecast_moves:
        reset.act(f"put forecast on {BASELINES[FORECAST]}, through Helm, last: it fills the node",
                  lambda: put_on(FORECAST, BASELINES[FORECAST]))


def admin_json(*args):
    os.environ["KUBECONFIG"] = ADMIN
    return room.kubectl_json(*args)


def node_full(reading):
    """Fit for a take: forecast's pods all placed, and too little left for catalog's next pod. Why not, else."""
    if reading.free < 0:
        return f"more reserved than the node has: {room.size(reading.reserved)} of {room.size(reading.allocatable)}"
    if reading.fits:
        return (f"{room.size(reading.free)} is free to reserve and catalog's next pod asks for {room.size(reading.ask)}, "
                "so the hotfix would simply start. Is something missing from the node?")
    return None


def read_the_node(dry_run):
    try:
        reading = room.read(admin_json)
    except (RuntimeError, KeyError, ValueError, OSError) as error:
        print(f"  not read: {error}. scripts/demo.sh status hotfix-room reads it with root's kubeconfig.")
        return
    for line in room.frame(reading)[6:]:
        print(f"  {line}")
    problem = node_full(reading)
    if problem and not dry_run:
        raise RuntimeError(problem)


def stage(reset, name):
    version = TAKES[name]
    current = host.main_version(service(name)["path"])
    if current == version:
        print(f"  main has {name} {version}")
        return
    reset.act(f"stage {name} {version} on main (main has {current})", lambda: host.stage(name, version))


def run(args):
    started = time.monotonic()
    shared = {name: shared_reset(name) for name in (CATALOG, FORECAST)}
    reset = shared[CATALOG].Reset(args.dry_run)
    print(f"Reset video 33: catalog {BASELINES[CATALOG]}, forecast {BASELINES[FORECAST]}, {TAKES[CATALOG]} and "
          f"{TAKES[FORECAST]} staged{' (dry run: nothing changes)' if args.dry_run else ''}.")
    with kube.credentials(quiet=True) as kubeconfig:
        api = kube.Api(kubeconfig)
        refused = refusal(api)
        if refused:
            print(f"Refused: {refused}.")
            return 1
        catalog = shared[CATALOG]
        try:
            reset.step("1. Release and rollback runs in Jenkins", lambda: catalog.stop_runs(reset, catalog.release_jobs()))
        except catalog.HistoryRunning as error:
            print(f"  {error}")
            return 1

        def jira(module, key):
            module.DEMO_ISSUES = [key]
            module.jira_versions(reset)

        issues = {}
        reset.step("2. The Jira issues", lambda: issues.update(take_issues()))
        for name, module in shared.items():
            reset.step(f"2. {name}: open changes in ServiceNow", lambda module=module: module.cancel_changes(reset))
            reset.step(f"2. {name}: tags and GitHub releases after {BASELINES[name]}",
                       lambda module=module: module.github_takes(reset))
            reset.step(f"2. {name}: release copies in JFrog after {BASELINES[name]}",
                       lambda module=module: module.jfrog_takes(reset))
            if issues:
                reset.step(f"2. {name}: Jira versions after {BASELINES[name]}, and {issues[name]}, its comments kept",
                           lambda module=module, key=issues[name]: jira(module, key))
        reset.step("3 and 4. The cluster: catalog, then forecast", lambda: cluster(reset, kubeconfig, api))
    reset.step("5. The node: too little free for catalog's next pod", lambda: read_the_node(args.dry_run))
    for name in (CATALOG, FORECAST):
        reset.step(f"6. {name} {TAKES[name]} on main", lambda name=name: stage(reset, name))
    reset.step("7. The Slack handler, one call", lambda: catalog.warm_slack_handler(reset, args.slack_handler_url))
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
        url = args.slack_handler_url or shared_reset(CATALOG).slack_handler_url()
        shown = ["--dry-run"] if args.dry_run else []
        return host.hand_over("reset", "demos/hotfix_room/reset.py",
                              [*shown, *(["--slack-handler-url", url] if url else [])],
                              title=" ".join(["reset.py hotfix-room", *shown]))
    sys.stdout.reconfigure(line_buffering=True)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
