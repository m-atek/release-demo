"""Put video 25 back where a take starts: both namespaces on catalog 1.9.1, fresh pods, no block, a version staged.

    python scripts/reset.py database-blip                   take 1: 1.10.0 staged
    python scripts/reset.py database-blip --stage 1.10.1    take 2
    python scripts/reset.py database-blip --dry-run         every action it would take, and nothing else

From the Mac it hands itself over to the tools host, like reset.py; the reset
job runs it there with DEMO=database-blip and OPTIONS. It refuses unless
catalog-postgres-0 is ready in both namespaces, with its RESTARTS at 0. Steps:

1. the take's release and rollback runs stopped;
2. block-database deleted if it's there;
3. the main reset's steps for what the takes made after 1.9.1: open catalog
   changes cancelled, tags catalog-v1.10.0 and v1.10.1 and their GitHub releases
   archived under takes/, JFrog release copies archived, Jira versions
   un-released and ORD-[b] reopened, its comments kept;
4. both namespaces on 1.9.1 through the same Helm deploy as a release, new pods
   if a pod there has restarted (reused pods would carry the back-off);
5. the version staged (stage.py);
6. the log's end marked, so the bottom window starts clean;
7. the Slack handler warmed.

Nothing here touches Slack's messages, so a reset with --stage 1.10.1 leaves the
failed run's message alone. Under two minutes, safe to run twice.
"""

import argparse
import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parents[1]

import blip_check  # noqa: E402  (it puts scripts/ and the Kubernetes demo's folder on the path)
import host  # noqa: E402
import kube  # noqa: E402
from common import ROOT, git, service  # noqa: E402

SERVICE = "catalog"
BASELINE = "1.9.1"
STAGES = ["1.10.0", "1.10.1"]
POSTGRES = "catalog-postgres-0"
RESET = str(SCRIPTS / "demos" / "kubernetes" / "reset.py")


def shared_reset():
    """scripts/reset.py as a module, its steps pointed at catalog 1.9.1: this file has its name."""
    spec = importlib.util.spec_from_file_location("shared_reset", SCRIPTS / "reset.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.SERVICE, module.BASELINE = SERVICE, BASELINE
    return module


def take_issue():
    from jira import PROJECT, Jira
    from setup import SUMMARY

    found = [i["key"] for i in Jira().search(f'project = {PROJECT} AND summary ~ "\\"{SUMMARY}\\""', fields="summary")
             if i["fields"]["summary"] == SUMMARY]
    if not found:
        raise RuntimeError(f'No Jira issue "{SUMMARY}" yet: run scripts/demos/database-blip/setup.py once.')
    return found[0]


def namespaces(config):
    return {name: env["namespace"] for name, env in config["environments"].items()}


def refusal(api, config):
    """Why a take can't start from here: the database's row must read true, 0. None when it can."""
    for namespace in namespaces(config).values():
        pod = api.get(f"/api/v1/namespaces/{namespace}/pods/{POSTGRES}")
        status = ((pod or {}).get("status", {}).get("containerStatuses") or [{}])[0]
        if not status.get("ready"):
            return f"{POSTGRES} isn't ready in {namespace}: scripts/demo.sh up database-blip"
        if status.get("restartCount"):
            return (f"{POSTGRES} in {namespace} reads RESTARTS {status['restartCount']}, and the take films it at 0: "
                    "scripts/demo.sh up database-blip gives it a new pod")
    return None


def remove_block(reset, api, namespace):
    path = f"{blip_check.policies(namespace)}/{blip_check.POLICY}"
    if api.get(path):
        reset.act(f"delete {blip_check.POLICY} in {namespace}", lambda: api.delete(path))
    else:
        print(f"  no {blip_check.POLICY} in {namespace}")


def pods_to_renew(api, config):
    """{namespace: pods with restarts}: a namespace on 1.9.1 whose pods are used. Helm replaces any others."""
    release = config["kubernetes"]["release"]
    used = {}
    for namespace in namespaces(config).values():
        pods = api.pods(namespace, release)
        restarted = [pod["metadata"]["name"] for pod in pods
                     if (pod.get("status", {}).get("containerStatuses") or [{}])[0].get("restartCount")]
        if restarted:
            used[namespace] = restarted
    return used


def on_baseline(kubeconfig, api, config):
    """The namespaces that aren't on 1.9.1 with every desired pod ready and nothing pending."""
    release = config["kubernetes"]["release"]
    elsewhere = []
    for environment, namespace in namespaces(config).items():
        revisions = kube.history(kubeconfig, release, namespace)
        current = kube.deployed(revisions)
        pods = [kube.pod_summary(pod) for pod in api.pods(namespace, release)]
        ready = [pod for pod in pods if pod[0] == BASELINE and pod[1] and not pod[2]]
        steady = (revisions and not revisions[-1]["status"].startswith("pending") and current
                  and current["app_version"] == BASELINE
                  and len(ready) == config["environments"][environment]["replicas"] == len(pods))
        print(f"  {namespace}: {'on ' + BASELINE if steady else 'going to ' + BASELINE}")
        if not steady:
            elsewhere.append(namespace)
    return elsewhere


def put_on_baseline():
    """Video 8's reset with another version: both namespaces at once, through deploy.py's Helm deploy."""
    commit = git("log", "-1", "--format=%H", "--", f"demo-states/{SERVICE}/{BASELINE}", check=False).stdout.strip()
    subprocess.run([sys.executable, RESET, "--here", "--baseline", BASELINE, "--commit", commit or "baseline"],
                   cwd=ROOT, check=True)


def renew(kubeconfig, namespace, release):
    env = {**os.environ, "KUBECONFIG": str(kubeconfig)}
    for args in (["rollout", "restart", f"deployment/{release}"], ["rollout", "status", f"deployment/{release}", "--timeout=180s"]):
        subprocess.run(["kubectl", *args, "--namespace", namespace], env=env, check=True, capture_output=True, text=True)


def cluster(reset, kubeconfig, api, config):
    elsewhere = on_baseline(kubeconfig, api, config)
    if elsewhere:
        reset.act(f"put {' and '.join(elsewhere)} on {BASELINE}, through Helm", put_on_baseline)
    used = {namespace: pods for namespace, pods in pods_to_renew(api, config).items() if namespace not in elsewhere}
    for namespace, pods in used.items():
        reset.act(f"new pods in {namespace}: {len(pods)} have restarted",
                  lambda namespace=namespace: renew(kubeconfig, namespace, config["kubernetes"]["release"]))


def stage(reset, version):
    current = host.main_version(service(SERVICE)["path"])
    if current == version:
        print(f"  main has {SERVICE} {version}")
        return
    reset.act(f"stage {SERVICE} {version} on main (main has {current})", lambda: host.stage(SERVICE, version))


def mark_log(reset):
    """The bottom window starts from the log's end: lines.py asks for it, and this marks where a take began."""
    def mark():
        log = blip_check.open_log(blip_check.LOG)
        if log:
            log.write(f"== reset {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n")
            log.close()

    reset.act(f"mark the end of {blip_check.LOG}", mark)


def run(args):
    started = time.monotonic()
    config = service(SERVICE)
    print(f"Reset video 25: {SERVICE} {BASELINE} in staging and production, {args.stage} staged"
          f"{' (dry run: nothing changes)' if args.dry_run else ''}.")
    with kube.credentials(quiet=True) as kubeconfig:
        api = kube.Api(kubeconfig)
        refused = refusal(api, config)
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
        reset.step(f"2. {blip_check.POLICY}", lambda: remove_block(reset, api, blip_check.settings(SERVICE).namespace))
        reset.step("3. Open changes in ServiceNow", lambda: shared.cancel_changes(reset))
        reset.step(f"4. The takes' tags and GitHub releases, after {BASELINE}", lambda: shared.github_takes(reset))
        reset.step(f"5. The takes' release copies in JFrog, after {BASELINE}", lambda: shared.jfrog_takes(reset))

        def jira():
            shared.DEMO_ISSUES = [take_issue()]
            shared.jira_versions(reset)

        reset.step(f"6. Jira versions after {BASELINE}, and ORD-[b], its comments kept", jira)
        reset.step(f"7. Both namespaces on {BASELINE}, fresh pods", lambda: cluster(reset, kubeconfig, api, config))
    reset.step(f"8. {SERVICE} {args.stage} on main", lambda: stage(reset, args.stage))
    reset.step("9. The bottom window's log", lambda: mark_log(reset))
    reset.step("10. The Slack handler, one call", lambda: shared.warm_slack_handler(reset, args.slack_handler_url))
    took = time.monotonic() - started
    if reset.failed:
        print(f"Finished in {took:.0f}s, but these steps failed: {'; '.join(reset.failed)}. Fix them and run it again.")
        return 1
    print(f"{'Dry run' if args.dry_run else 'Reset'} in {took:.0f}s: {reset.actions} action(s). "
          f"{'' if args.dry_run else 'Ready for a take.'}".rstrip())
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--stage", choices=STAGES, default=STAGES[0])
    parser.add_argument("--dry-run", action="store_true", help="print every action, change nothing")
    parser.add_argument("--here", action="store_true", help="run here, not on the tools host")
    parser.add_argument("--slack-handler-url", help="the Slack handler's function URL; from the Mac, looked up")
    args = parser.parse_args()
    if not args.here and not host.on_host():
        url = args.slack_handler_url or shared_reset().slack_handler_url()
        shown = ["--stage", args.stage, *(["--dry-run"] if args.dry_run else [])]
        return host.hand_over("reset", "demos/database-blip/reset.py",
                              [*shown, *(["--slack-handler-url", url] if url else [])],
                              title=" ".join(["reset.py database-blip", *shown]))
    sys.stdout.reconfigure(line_buffering=True)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
