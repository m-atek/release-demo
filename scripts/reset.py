"""Put a demo back to its starting state, for the next take or call.

    python scripts/reset.py                   the main demo
    python scripts/reset.py main --dry-run    every action it would take, and nothing else
    python scripts/reset.py <demo> [...]      runs scripts/demos/<demo>/reset.py with the rest

The main demo starts where history.py left it: orders-service 1.3.0 on main,
in staging and in production, and nothing released after it. In order:

1. Stop the release runs a take left going or queued, so the next take
   starts at once. It refuses while history.py runs.
2. Cancel the open orders-service changes in ServiceNow.
3. Delete the tags and GitHub releases of versions after 1.3.0.
4. Delete their copies in orders-service-releases in JFrog, with JCR's admin
   password: the pipeline's token can't delete. Candidates stay, each under
   its own build number.
5. Un-release the Jira versions after 1.3.0 and reopen their issues, and
   ORD-12 and ORD-13 whatever their fix version.
6. Stage 1.3.0 on main.
7. Move both live aliases to the newest Lambda version of 1.3.0, and call
   both functions once, so the first take doesn't start cold.

Each step looks before it acts, so a second run finds nothing to do. A step
that fails doesn't stop the others, and the exit code says so. Under two
minutes. It runs on the tools host, like history.py, because Jenkins and
JFrog answer only there: from the Mac it hands itself over through SSM Run
Command (scripts/host.py). --here runs it where you are.
"""

import argparse
import base64
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import host
import run_state
from check import wait_for_version
from common import ROOT, fail, get_parameter, github_api, github_app_token, http_get, service, ssm_client
from history import BASELINE, PLAN, SERVICE
from release_job import ReleaseJob

DEMO_ISSUES = ["ORD-12", "ORD-13"]
NOTE = "Cancelled by reset.py before the next take. Demo system, not a client's."
CHANGE_STATES = {"-5": "New", "-4": "Assess", "-3": "Authorize", "-2": "Scheduled", "-1": "Implement", "0": "Review"}
STOP_WAIT_SECONDS = 60


class HistoryRunning(Exception):
    pass


def version_key(text):
    """(1, 4, 0) for "1.4.0"; None for anything that isn't a version."""
    return tuple(int(part) for part in text.split(".")) if re.fullmatch(r"\d+\.\d+\.\d+", text or "") else None


def after_baseline(version):
    key = version_key(version)
    return key is not None and key > version_key(BASELINE)


class Reset:
    """Runs the steps and their actions. A dry run prints the actions instead."""

    def __init__(self, dry_run):
        self.dry_run = dry_run
        self.actions = 0
        self.failed = []

    def act(self, what, do):
        self.actions += 1
        if self.dry_run:
            print(f"  would {what}")
            return
        do()
        print(f"  {what}: done")

    def step(self, title, body):
        print(title)
        try:
            body()
        except HistoryRunning:
            raise
        except SystemExit as error:  # fail() from a shared script: its message is on stderr
            self.failed.append(title)
            print(f"  failed: {error.code if isinstance(error.code, str) else 'see the message above'}")
        except Exception as error:
            self.failed.append(title)
            print(f"  failed: {error.__class__.__name__}: {error}")


def stop_runs(reset, job):
    # history.py started from the Mac runs as a systemd unit; between two of
    # its releases no release run shows it.
    if host.unit_running("history"):
        raise HistoryRunning("history.py is running on the tools host. Reset after it ends.")
    history_keys = {key for _, key, _ in PLAN}
    running = [build for build in job.builds(limit=20) if build["building"]]
    queued = job.queued()
    for item in running + queued:
        if item["parameters"].get("JIRA_KEY") in history_keys:
            raise HistoryRunning(f"history.py is running ({item['parameters']['JIRA_KEY']}). Reset after it ends.")
    if not running and not queued:
        print("  no release runs going")
    for item in queued:
        reset.act(f"cancel the queued release run for {item['parameters'].get('JIRA_KEY')}",
                  lambda item=item: job.cancel(item["id"]))
    for build in running:
        reset.act(f"stop release build #{build['number']} ({build.get('displayName')})",
                  lambda build=build: job.stop(build["number"]))
    if running and not reset.dry_run:
        # A stopped run still cancels its change and finishes its Slack message.
        deadline = time.monotonic() + STOP_WAIT_SECONDS
        waiting = [build["number"] for build in running]
        while waiting and time.monotonic() < deadline:
            time.sleep(3)
            waiting = [number for number in waiting if job.build(number)["building"]]
        for number in waiting:
            print(f"  build #{number} is still stopping: carrying on")


def cancel_changes(reset):
    from change import connect

    sn, _ = connect()
    item = sn.find("cmdb_ci_appl", f"name={service(SERVICE)['servicenow']['cmdb_item']}", "sys_id")
    if not item:
        fail(f"ServiceNow has no CMDB item {SERVICE}. Run scripts/servicenow_setup.py.")
    rows = sn.call("GET", "/api/now/table/change_request", sysparm_query=f"cmdb_ci={item['sys_id']}^active=true^ORDERBYnumber",
                   sysparm_fields="sys_id,number,type,state,short_description", sysparm_limit=100) or []
    if not rows:
        print("  no open changes")
    for row in rows:
        state = CHANGE_STATES.get(row["state"], row["state"])
        reset.act(f"cancel {row['number']} ({row['short_description']}, {state})",
                  lambda row=row: sn.call("PATCH", f"/api/sn_chg_rest/change/{row['type'] or 'normal'}/{row['sys_id']}",
                                          {"state": "4", "close_notes": NOTE, "work_notes": NOTE}))


def github_releases(reset):
    token, repository, _ = github_app_token(ssm_client())

    def call(method, path):
        return github_api(method, path, token, scheme="token")

    prefix = f"{SERVICE}-v"
    releases, page = [], 1
    while True:
        batch = call("GET", f"/repos/{repository}/releases?per_page=100&page={page}") or []
        releases += batch
        if len(batch) < 100:
            break
        page += 1
    tags = [ref["ref"].removeprefix("refs/tags/") for ref in call("GET", f"/repos/{repository}/git/matching-refs/tags/{prefix}") or []]
    late_releases = [r for r in releases if r["tag_name"].startswith(prefix) and after_baseline(r["tag_name"].removeprefix(prefix))]
    late_tags = sorted((t for t in tags if after_baseline(t.removeprefix(prefix))), key=lambda t: version_key(t.removeprefix(prefix)))
    if not late_releases and not late_tags:
        print(f"  no tags or releases after {BASELINE}")
    for release in late_releases:
        reset.act(f"delete the GitHub release {release['name'] or release['tag_name']}",
                  lambda release=release: call("DELETE", f"/repos/{repository}/releases/{release['id']}"))
    for tag in late_tags:
        reset.act(f"delete the tag {tag}", lambda tag=tag: call("DELETE", f"/repos/{repository}/git/refs/tags/{tag}"))


def jcr_request(method, path, password):
    from publish import JCR

    auth = "Basic " + base64.b64encode(f"admin:{password}".encode()).decode()
    request = urllib.request.Request(f"{JCR}/artifactory/{path}", method=method, headers={"Authorization": auth})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def jfrog_copies(reset):
    password = get_parameter(ssm_client(), "jcr/admin-password")
    repository = f"{SERVICE}-releases"
    status, body = jcr_request("GET", f"api/storage/{repository}", password)
    if status == 401:
        fail("JCR refused the admin password in SSM (jcr/admin-password).")
    if status != 200:
        fail(f"JCR didn't list {repository} ({status}): {body[:200].decode(errors='replace')}")
    folders = [child["uri"].strip("/") for child in json.loads(body).get("children", []) if child.get("folder")]
    late = sorted((f for f in folders if after_baseline(f)), key=version_key)
    if not late:
        print(f"  no copies after {BASELINE} in {repository}")

    def delete(folder):
        status, body = jcr_request("DELETE", f"{repository}/{folder}", password)
        if status not in (200, 202, 204, 404):
            fail(f"JCR didn't delete {repository}/{folder} ({status}): {body[:200].decode(errors='replace')}")

    for folder in late:
        reset.act(f"delete {repository}/{folder}/ from JFrog", lambda folder=folder: delete(folder))


def jira_versions(reset):
    from jira import PROJECT, Jira

    jira = Jira()
    late = [v for v in jira.call("GET", f"/rest/api/3/project/{PROJECT}/versions")
            if v["name"].startswith(f"{SERVICE} ") and after_baseline(v["name"].rsplit(" ", 1)[1])]
    for version in late:
        if version.get("released"):
            reset.act(f"un-release the Jira version {version['name']}",
                      lambda version=version: jira.call("PUT", f"/rest/api/3/version/{version['id']}", {"released": False}))
        else:
            print(f"  {version['name']}: not released")
    clauses = [f"key in ({', '.join(DEMO_ISSUES)})"] + ([f"fixVersion in ({', '.join(v['id'] for v in late)})"] if late else [])
    jql = f"project = {PROJECT} AND ({' OR '.join(clauses)}) ORDER BY key"
    issues = jira.call("GET", f"/rest/api/3/search/jql?jql={urllib.parse.quote(jql)}&fields=status&maxResults=50")["issues"]

    def reopen(key):
        if not (jira.transition(key, "Backlog") or jira.transition(key, "To Do")):
            fail(f"{key} has no transition to Backlog or To Do.")

    for issue in issues:
        status = issue["fields"]["status"]
        if status.get("statusCategory", {}).get("key") == "done":
            reset.act(f"reopen {issue['key']} ({status['name']})", lambda key=issue["key"]: reopen(key))
        else:
            print(f"  {issue['key']}: {status['name']}")


def stage_baseline(reset):
    path = service(SERVICE)["path"]
    current = host.main_version(path)
    reset.act(f"stage {SERVICE} {BASELINE} on main (main has {current})", lambda: host.stage(SERVICE, BASELINE))


def lambda_aliases(reset):
    from deploy import lambda_client

    client = lambda_client(SERVICE)
    urls = {}
    for environment in service(SERVICE)["environments"].values():
        function = environment["function"]
        versions = []
        for page in client.get_paginator("list_versions_by_function").paginate(FunctionName=function):
            versions += [v for v in page["Versions"] if v["Version"] != "$LATEST" and v.get("Description") == BASELINE]
        if not versions:
            fail(f"{function} has no Lambda version of {BASELINE} yet. Run history.py first.")
        target = max(versions, key=lambda v: int(v["Version"]))["Version"]
        live = client.get_alias(FunctionName=function, Name="live")["FunctionVersion"]
        if live == target:
            print(f"  {function}: live is on version {target} ({BASELINE})")
        else:
            reset.act(f"move {function} live from version {live} to {target} ({BASELINE})",
                      lambda function=function, target=target: client.update_alias(
                          FunctionName=function, Name="live", FunctionVersion=target))
        urls[function] = client.get_function_url_config(FunctionName=function, Qualifier="live")["FunctionUrl"].rstrip("/")
    for function, url in urls.items():
        if reset.dry_run:
            print(f"  would call {function} once: /health, then the page")
            continue
        live, seen = wait_for_version(url, BASELINE, 30)
        status, _ = http_get(url + "/", timeout=15)
        print(f"  {function}: /health reports {seen or 'nothing'}, the page answered {status or 'nothing'}")
        if not live or status != 200:
            fail(f"{function} doesn't serve {BASELINE} yet.")


def reset_main(dry_run):
    started = time.monotonic()
    reset = Reset(dry_run)
    print(f"Reset the main demo to {SERVICE} {BASELINE}{' (dry run: nothing changes)' if dry_run else ''}.")
    try:
        reset.step("1. Release runs in Jenkins", lambda: stop_runs(reset, ReleaseJob.as_release_bot()))
    except HistoryRunning as error:
        print(f"  {error}")
        return 1
    reset.step("2. Open changes in ServiceNow", lambda: cancel_changes(reset))
    reset.step(f"3. Tags and GitHub releases after {BASELINE}", lambda: github_releases(reset))
    reset.step(f"4. Release copies in JFrog after {BASELINE}", lambda: jfrog_copies(reset))
    reset.step(f"5. Jira versions after {BASELINE} and their issues", lambda: jira_versions(reset))
    reset.step(f"6. {SERVICE} on main", lambda: stage_baseline(reset))
    reset.step("7. Lambda aliases, and one call each", lambda: lambda_aliases(reset))

    took = run_state.duration(time.monotonic() - started)
    if reset.failed:
        print(f"Finished in {took}, but these steps failed: {'; '.join(reset.failed)}. Fix them and run it again.")
        return 1
    if dry_run:
        print(f"Dry run in {took}: {reset.actions} action(s) to take.")
    else:
        print(f"Reset in {took}: {reset.actions} action(s). Ready for a take.")
    return 0


def other_demo(demo, args):
    folder = ROOT / "scripts" / "demos" / demo
    if not folder.is_dir():
        demos = sorted(p.name for p in folder.parent.iterdir() if p.is_dir()) if folder.parent.is_dir() else []
        fail(f"No demo {demo}. Demos: {', '.join(['main', *demos])}")
    script = folder / "reset.py"
    if not script.exists():
        print(f"{demo} has no reset.py: nothing to reset.")
        return 0
    return subprocess.run([sys.executable, str(script), *args], cwd=ROOT).returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("demo", nargs="?", default="main", help="main, or a folder in scripts/demos/")
    parser.add_argument("--dry-run", action="store_true", help="print every action, change nothing")
    parser.add_argument("--follow", action="store_true", help="from the Mac: show the run on the tools host")
    parser.add_argument("--here", action="store_true", help="run here, not on the tools host")
    args, rest = parser.parse_known_args()

    if args.demo != "main":
        return other_demo(args.demo, [*(["--dry-run"] if args.dry_run else []), *rest])
    if rest:
        parser.error(f"unrecognized arguments: {' '.join(rest)}")
    if not args.here and not host.on_host():
        return host.hand_over("reset", "reset.py", ["main", *(["--dry-run"] if args.dry_run else [])],
                              follow_only=args.follow)
    if args.follow:
        fail("--follow is for the Mac. Here, read /var/log/release-demo-reset.log.")
    sys.stdout.reconfigure(line_buffering=True)
    return reset_main(args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
