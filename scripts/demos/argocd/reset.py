"""Put demo 19 back where a take starts: production on the cluster host on
1.9.1 with 3 replicas, Synced, and nothing a take left open.

    python scripts/reset.py argocd               or the Jenkins job reset with DEMO=argocd
    python scripts/reset.py argocd --dry-run     every action it would take, and nothing else
    python scripts/demos/argocd/reset.py --only incidents,values

Steps, each looking before it acts, so a second run finds nothing to do:
- builds: a fix pipeline run still going is stopped, so it can't merge;
- pulls: an open fix pull request (a branch under oncall/, or one that
  changes deploy/production/catalog) is closed with a comment. Merged ones
  stay, and git history keeps every commit;
- changes: an open emergency change on catalog-production is cancelled;
- incidents: a drift incident a take left open is resolved, "Resolved by
  reset.py before the next take.";
- values: if deploy/production/catalog/values.yaml on main doesn't say 3
  replicas and 1.9.1's image digest (from its release copy in JFrog), a
  standard change from "Reset of catalog production" opens, the GitHub App
  commits "demo: catalog production back to 3 replicas [skip ci]" to main,
  Argo CD syncs it, and the change closes. Even a reset changes production
  through Git and on the record;
- sync: Argo CD Synced at main and Healthy, with every pod ready.

It runs on the tools host, where JCR and the cluster host's Argo CD answer:
from the Mac it hands itself over through SSM Run Command, as the main
reset does. Under two minutes.
"""

import argparse
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
# scripts/ first: its host.py hands a run over to the tools host; this folder's reaches the cluster host.
sys.path.insert(0, str(HERE.parents[1]))
sys.path.append(str(HERE.parent / "terraform"))  # demo 2's GitHub App client
import gitops  # noqa: E402
import host  # noqa: E402
from common import fail, get_parameter, service, ssm_client  # noqa: E402

STEPS = ["builds", "pulls", "changes", "incidents", "values", "sync"]
NOTE = "Resolved by reset.py before the next take."
CLOSED_BY_RESET = ("Closed by reset.py before the next take. Nothing was merged; to make this change, "
                   "open a new pull request.")
ACTIVE_CHANGE = "active=true^type=emergency"


class Reset:
    """The steps and their actions. A dry run prints the actions instead."""

    def __init__(self, dry_run):
        self.dry_run, self.actions, self.failed = dry_run, 0, []

    def act(self, what, do):
        self.actions += 1
        if self.dry_run:
            print(f"  would {what}")
            return None
        result = do()
        print(f"  {what}: done")
        return result

    def step(self, title, body):
        print(title)
        try:
            body()
        except SystemExit as error:
            self.failed.append(title)
            print(f"  failed: {error.code if isinstance(error.code, str) else 'see the message above'}")
        except Exception as error:
            self.failed.append(title)
            print(f"  failed: {error.__class__.__name__}: {error}")


def pinned_values(text, replicas, digest):
    """values.yaml with replicas and the image digest set, comments and order kept."""
    text = re.sub(r"(?m)^replicas: .*$", f"replicas: {replicas}", text)
    return re.sub(r'(?m)^(  digest: ).*$', rf'\g<1>"{digest}"', text)


def needs_reset(text, replicas, digest):
    import yaml

    values = yaml.safe_load(text) or {}
    return values.get("replicas") != replicas or (values.get("image") or {}).get("digest") != digest


def step_builds(reset):
    from release_job import ReleaseJob

    jenkins = ReleaseJob.as_release_bot(job=gitops.settings()["job"])
    status, _, body = jenkins.call("GET", f"/job/{jenkins.job}/api/json?tree=jobs[name,url,builds[number,building]]")
    if status == 404:
        print(f"  no job {jenkins.job} yet")
        return
    import json

    running = [(branch["name"], build["number"]) for branch in json.loads(body).get("jobs", [])
               for build in branch.get("builds", [])[:5] if build.get("building")]
    if not running:
        print("  no fix pipeline runs going")
    for branch, number in running:
        reset.act(f"stop {jenkins.job} {branch} #{number}",
                  lambda branch=branch, number=number: jenkins.call("POST", f"/job/{jenkins.job}/job/{branch}/{number}/stop"))


def step_pulls(reset):
    import github
    import pr_scope

    gh = github.GitHub()
    prefix, path = gitops.settings()["branch_prefix"], gitops.settings()["path"]
    found = [p for p in gh.open_pulls() or []
             if p["head"]["ref"].startswith(prefix) or pr_scope.touches(pr_scope.files(p["number"]), path)]
    if not found:
        print("  no open fix pull requests")
    for pull in found:
        reset.act(f"close pull request #{pull['number']} ({pull['title']})",
                  lambda pull=pull: gh.close_pull(pull["number"], CLOSED_BY_RESET))


def servicenow():
    import change

    sn, _ = change.connect()
    return sn


def step_changes(reset):
    import change

    sn = servicenow()
    item = sn.find("cmdb_ci_appl", f"name={service(gitops.SERVICE)['servicenow']['cmdb_item']}", "sys_id")
    rows = sn.call("GET", "/api/now/table/change_request", sysparm_query=f"cmdb_ci={item['sys_id']}^{ACTIVE_CHANGE}",
                   sysparm_fields="sys_id,number,short_description", sysparm_limit=50) or []
    if not rows:
        print("  no open emergency changes")
    for row in rows:
        reset.act(f"cancel {row['number']} ({row['short_description']})",
                  lambda row=row: change.cancel(sn, {"change": {"sys_id": row["sys_id"], "type": "emergency"}},
                                                "Cancelled by reset.py before the next take."))


def step_incidents(reset):
    sn = servicenow()
    rows = gitops.open_drift_incidents(sn)
    if not rows:
        print("  no open drift incidents")
    for row in rows:
        reset.act(f"resolve {row['number']} ({row['short_description']})",
                  lambda row=row: gitops.resolve(sn, row["sys_id"], NOTE))


def standard_change(sn, fields):
    config = service(gitops.SERVICE)["servicenow"]
    name = config["templates"]["reset"]
    template = sn.find("std_change_record_producer", f"name={name}^active=true", "sys_id")
    if not template:
        fail(f"ServiceNow has no active template '{name}'. Run scripts/servicenow_setup.py.")
    reply = sn.call("POST", f"/api/sn_chg_rest/change/standard/{template['sys_id']}", fields)
    return {"sys_id": reply["sys_id"]["value"], "number": reply["number"]["value"], "type": "standard"}


def step_values(reset):
    import argocd
    import change
    import github

    ssm = ssm_client()
    gh = github.GitHub(ssm)
    settings = gitops.settings()
    digest = gitops.release_digest(get_parameter(ssm, "jcr/token"))
    text = gh.file_at(gitops.values_path(), "main") or ""
    if not needs_reset(text, settings["replicas"], digest):
        print(f"  values.yaml on main: {settings['replicas']} replicas, 1.9.1's image {digest[:19]}...")
        return
    wanted = pinned_values(text, settings["replicas"], digest)
    if needs_reset(wanted, settings["replicas"], digest):
        fail("values.yaml has no replicas or image digest line to set: fix it by hand, once.")
    message = f"demo: catalog production back to {settings['replicas']} replicas [skip ci]"

    def commit():
        sn = servicenow()
        item = sn.find("cmdb_ci_appl", f"name={service(gitops.SERVICE)['servicenow']['cmdb_item']}", "sys_id")
        state = {"change": standard_change(sn, {"short_description": "Reset of catalog production",
                                                "cmdb_ci": item["sys_id"]})}
        change.implement(sn, state)
        parent = gh.branch_sha("main")
        sha = gh.commit_files(parent, {gitops.values_path(): wanted}, message)
        gh.set_branch("main", sha)
        change.note(sn, state, f"Committed {sha[:7]} to main: {message}")
        argocd.ArgoCD(ssm).wait(settings["application"], sha, settings["sync_timeout_seconds"])
        change.close(sn, state, "successful", f"Argo CD Synced catalog-production at {sha[:7]}: "
                                              f"{settings['replicas']} replicas of 1.9.1. Reset by reset.py.")
        print(f"  {state['change']['number']}: committed {sha[:7]}, Synced, closed")

    reset.act(f"commit \"{message}\" under a standard change, and wait for Argo CD", commit)


def step_sync(reset):
    import argocd
    import github

    ssm = ssm_client()
    main = github.GitHub(ssm).branch_sha("main")
    settings = gitops.settings()
    if reset.dry_run:
        print(f"  would wait for {settings['application']} Synced at {main[:7]} and Healthy")
        return
    app = argocd.ArgoCD(ssm).wait(settings["application"], main, settings["sync_timeout_seconds"])
    found = argocd.summary(app)
    print(f"  {settings['application']}: {found['sync']} at {found['revision'][:7]}, {found['health']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="print every action, change nothing")
    parser.add_argument("--only", default=",".join(STEPS), help=f"comma-separated, from: {', '.join(STEPS)}")
    parser.add_argument("--here", action="store_true", help="run here, not on the tools host")
    args = parser.parse_args()
    if not args.here and not host.on_host():
        return host.hand_over("reset-argocd", "demos/argocd/reset.py",
                              [*(["--dry-run"] if args.dry_run else []), "--only", args.only],
                              title=f"reset.py argocd{' --dry-run' if args.dry_run else ''}")
    sys.stdout.reconfigure(line_buffering=True)
    started = time.monotonic()
    reset = Reset(args.dry_run)
    print(f"Reset demo 19: catalog production on the cluster host back to 1.9.1 with "
          f"{gitops.settings()['replicas']} replicas{' (dry run: nothing changes)' if args.dry_run else ''}.")
    for name in [s for s in STEPS if s in args.only.split(",")]:
        reset.step(name, lambda name=name: globals()[f"step_{name}"](reset))
    took = time.monotonic() - started
    if reset.failed:
        print(f"Not everything reset after {took:.0f}s: {', '.join(reset.failed)}. The messages above say why.")
        return 1
    print(f"{'Dry run' if args.dry_run else 'Reset'} in {took:.0f}s: {reset.actions} action(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
