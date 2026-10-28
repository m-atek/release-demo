"""Put a demo back to its starting state, for the next take or call.

    python scripts/reset.py                   the main demo
    python scripts/reset.py main --dry-run    every action it would take, and nothing else
    python scripts/reset.py <demo> [...]      runs scripts/demos/<demo>/reset.py with the rest

The main demo starts where history.py left it: orders-service 1.3.0 on main,
in staging and in production. Past data stays, because a platform with a
history looks real: everything history.py made (1.2.0 to 1.3.0), and the
changes, Jira comments, Slack messages and events of earlier takes. reset.py
only frees what a take must create again, and archives instead of deleting:

1. Stops the release and rollback runs a take left going or queued, so the
   next take starts at once. It refuses while history.py runs.
2. Cancels the open orders-service changes. Closed and cancelled ones stay.
3. Archives the take's tags, the versions after 1.3.0: each is tagged again
   as takes/<tag>-<UTC time> on the same commit, its GitHub release moves
   onto that tag as the pre-release "<name> (earlier take)", and then the
   tag's own name is free.
4. Archives the take's release copies in JFrog to <releases root>/takes/
   <version>-<UTC time>/. JCR's free edition has no move or copy API (both
   answer "available only in Artifactory Pro"), so, as admin, it copies each
   file, checks the copy's SHA-256, and only then deletes the original.
5. Un-releases the Jira versions after 1.3.0 and reopens ORD-12 and ORD-13.
6. Stages 1.3.0 on main.
7. Moves both live aliases to the newest Lambda version of 1.3.0, and calls
   both functions once, so the first take doesn't start cold.
8. Warms the Slack handler, so Sam's first /release isn't a cold start (cold
   it answers in about 2 seconds against Slack's 3): one request signed with
   Slack's signing secret, as Mateusz, who isn't a releaser, with an option
   that doesn't exist. It answers "You can't start releases here." and starts
   nothing. The tools host may not look up the handler's URL, so from the Mac
   reset.py looks it up and passes it on; without it the step is skipped.

Each step looks before it acts, so a second run finds nothing to do. A step
that fails doesn't stop the others, and the exit code says so. Under two
minutes. It runs on the tools host, like history.py, because Jenkins and
JFrog answer only there: from the Mac it hands itself over through SSM Run
Command (scripts/host.py). --here runs it where you are.
"""

import argparse
import base64
import hashlib
import hmac
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
from common import (ROOT, aws_session, fail, get_parameter, github_api, github_app_token, http_get, service,
                    ssm_client)
from history import BASELINE, PLAN, SERVICE
from release_job import ReleaseJob

DEMO_ISSUES = ["ORD-12", "ORD-13"]
NOTE = "Cancelled by reset.py before the next take. Demo system, not a client's."
CHANGE_STATES = {"-5": "New", "-4": "Assess", "-3": "Authorize", "-2": "Scheduled", "-1": "Implement", "0": "Review"}
STOP_WAIT_SECONDS = 60
JOBS = ["release", "rollback"]
SLACK_HANDLER = "release-demo-slack-handler"
NOT_A_RELEASER = "You can't start releases here."
# A releaser would get "no option warm-up" instead: nothing starts either way.
WARM_UP = {"command": "/release", "text": "orders-service ORD-12 warm-up"}


class HistoryRunning(Exception):
    pass


def version_key(text):
    """(1, 4, 0) for "1.4.0"; None for anything that isn't a version."""
    return tuple(int(part) for part in text.split(".")) if re.fullmatch(r"\d+\.\d+\.\d+", text or "") else None


def after_baseline(version):
    key = version_key(version)
    return key is not None and key > version_key(BASELINE)


class Reset:
    """Runs the steps and their actions. A dry run prints the actions instead.
    stamp names this run's archives: takes/<tag>-<stamp>, takes/<version>-<stamp>."""

    def __init__(self, dry_run, stamp=None):
        self.dry_run = dry_run
        self.stamp = stamp or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
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


def stop_runs(reset, jobs):
    # history.py started from the Mac runs as a systemd unit; between two of
    # its releases no release run shows it.
    if host.unit_running("history"):
        raise HistoryRunning("history.py is running on the tools host. Reset after it ends.")
    history_keys = {key for _, key, _ in PLAN}
    going = {}
    for job in jobs:
        running = [build for build in job.builds(limit=20) if build["building"]]
        queued = job.queued()
        for item in running + queued:
            if item["parameters"].get("JIRA_KEY") in history_keys:
                raise HistoryRunning(f"history.py is running ({item['parameters']['JIRA_KEY']}). Reset after it ends.")
        going[job] = (running, queued)
    if not any(running or queued for running, queued in going.values()):
        print("  no release or rollback runs going")
    for job, (running, queued) in going.items():
        for item in queued:
            reset.act(f"cancel the queued {job.job} run ({item['parameters'].get('JIRA_KEY') or item['parameters'].get('SERVICE')})",
                      lambda job=job, item=item: job.cancel(item["id"]))
        for build in running:
            reset.act(f"stop {job.job} build #{build['number']} ({build.get('displayName')})",
                      lambda job=job, build=build: job.stop(build["number"]))
    if reset.dry_run:
        return
    # A stopped run still cancels its change and finishes its Slack message.
    deadline = time.monotonic() + STOP_WAIT_SECONDS
    waiting = [(job, build["number"]) for job, (running, _) in going.items() for build in running]
    while waiting and time.monotonic() < deadline:
        time.sleep(3)
        waiting = [(job, number) for job, number in waiting if job.build(number)["building"]]
    for job, number in waiting:
        print(f"  {job.job} build #{number} is still stopping: carrying on")


def release_jobs():
    """The release job, and the rollback job where Jenkins has it."""
    jobs = [ReleaseJob.as_release_bot(job=name) for name in JOBS]
    return [job for job in jobs if job.job == "release" or job.exists()]


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


def github_takes(reset):
    token, repository, _ = github_app_token(ssm_client())

    def call(method, path, body=None):
        return github_api(method, path, token, body, scheme="token")

    prefix = f"{SERVICE}-v"
    releases, page = [], 1
    while True:
        batch = call("GET", f"/repos/{repository}/releases?per_page=100&page={page}") or []
        releases += batch
        if len(batch) < 100:
            break
        page += 1
    refs = call("GET", f"/repos/{repository}/git/matching-refs/tags/{prefix}") or []
    tags = {ref["ref"].removeprefix("refs/tags/"): ref["object"]["sha"] for ref in refs}
    take_tags = {tag: sha for tag, sha in tags.items() if after_baseline(tag.removeprefix(prefix))}
    take_releases = {r["tag_name"]: r for r in releases
                     if r["tag_name"].startswith(prefix) and after_baseline(r["tag_name"].removeprefix(prefix))}
    names = sorted(set(take_tags) | set(take_releases), key=lambda tag: version_key(tag.removeprefix(prefix)))
    if not names:
        print(f"  no tags or releases after {BASELINE}")
    for tag in names:
        archive = f"takes/{tag}-{reset.stamp}"
        release = take_releases.get(tag)
        # A release whose tag a cut-short reset already removed still names its commit.
        sha = take_tags.get(tag) or (release or {}).get("target_commitish", "")
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            fail(f"{tag}: no commit to archive it on (release target {sha!r}).")
        reset.act(f"tag {sha[:7]} as {archive}",
                  lambda archive=archive, sha=sha: call("POST", f"/repos/{repository}/git/refs",
                                                         {"ref": f"refs/tags/{archive}", "sha": sha}))
        if release:
            name = release["name"] or tag
            reset.act(f"move the GitHub release {name} onto {archive}, as the pre-release \"{name} (earlier take)\"",
                      lambda release=release, archive=archive, name=name: call(
                          "PATCH", f"/repos/{repository}/releases/{release['id']}",
                          {"tag_name": archive, "name": f"{name} (earlier take)", "prerelease": True,
                           "make_latest": "false"}))
        if tag in take_tags:
            reset.act(f"remove the tag {tag}", lambda tag=tag: call("DELETE", f"/repos/{repository}/git/refs/tags/{tag}"))


def jcr_request(method, path, password, data=None, headers=None):
    from publish import JCR

    auth = "Basic " + base64.b64encode(f"admin:{password}".encode()).decode()
    request = urllib.request.Request(f"{JCR}/artifactory/{path}", data=data, method=method,
                                     headers={"Authorization": auth, **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def jcr_files(password, folder):
    """Every file under a JFrog folder, as paths relative to it."""
    status, body = jcr_request("GET", f"api/storage/{folder}", password)
    if status != 200:
        fail(f"JCR didn't list {folder} ({status}): {body[:200].decode(errors='replace')}")
    files = []
    for child in json.loads(body).get("children", []):
        name = child["uri"].strip("/")
        files += [f"{name}/{inner}" for inner in jcr_files(password, f"{folder}/{name}")] if child.get("folder") else [name]
    return files


def jcr_archive(password, source, target):
    """Copy every file of source to target, check each copy's SHA-256, then
    delete source: JCR's free edition has no move or copy API."""
    for relative in jcr_files(password, source):
        status, content = jcr_request("GET", f"{source}/{relative}", password)
        if status != 200:
            fail(f"JCR didn't give {source}/{relative} ({status}).")
        digest = hashlib.sha256(content).hexdigest()
        status, body = jcr_request("PUT", f"{target}/{relative}", password, content, {
            "X-Checksum-Sha256": digest, "X-Checksum-Sha1": hashlib.sha1(content).hexdigest(),
            "Content-Type": "application/octet-stream"})
        if status not in (200, 201):
            fail(f"JCR refused {target}/{relative} ({status}): {body[:200].decode(errors='replace')}")
        status, body = jcr_request("GET", f"api/storage/{target}/{relative}", password)
        stored = json.loads(body).get("checksums", {}).get("sha256") if status == 200 else None
        if stored != digest:
            fail(f"{target}/{relative} has SHA-256 {stored}, not {digest}: {source} stays where it is.")
    status, body = jcr_request("DELETE", source, password)
    if status not in (200, 202, 204):
        fail(f"JCR didn't delete {source} after archiving it ({status}): {body[:200].decode(errors='replace')}")


def jfrog_takes(reset):
    from publish import releases_root

    password = get_parameter(ssm_client(), "jcr/admin-password")
    root = releases_root(SERVICE)
    status, body = jcr_request("GET", f"api/storage/{root}", password)
    if status == 401:
        fail("JCR refused the admin password in SSM (jcr/admin-password).")
    if status == 404 and "/" in root:
        # A service in the shared repositories gets its folder with its first release.
        print(f"  no release copies yet: {root} doesn't exist")
        return
    if status != 200:
        fail(f"JCR didn't list {root} ({status}): {body[:200].decode(errors='replace')}")
    folders = [child["uri"].strip("/") for child in json.loads(body).get("children", []) if child.get("folder")]
    late = sorted((folder for folder in folders if after_baseline(folder)), key=version_key)
    if not late:
        print(f"  no release copies after {BASELINE} in {root}")
    for version in late:
        target = f"{root}/takes/{version}-{reset.stamp}"
        reset.act(f"archive {root}/{version}/ as {target}/ (copy, check each SHA-256, delete the original)",
                  lambda version=version, target=target: jcr_archive(password, f"{root}/{version}", target))


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
    jql = f"project = {PROJECT} AND key in ({', '.join(DEMO_ISSUES)}) ORDER BY key"
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


def slack_handler_url():
    """The Slack handler's function URL, if these credentials may look it up:
    yours on the Mac can, the tools host's role can't."""
    try:
        return aws_session().client("lambda").get_function_url_config(FunctionName=SLACK_HANDLER)["FunctionUrl"]
    except Exception:
        return None


def signed_request(url, secret, form, now=None):
    """A POST the way Slack sends a slash command: the form, signed v0:<timestamp>:<body>."""
    body = urllib.parse.urlencode(form).encode()
    timestamp = str(int(now or time.time()))
    signature = "v0=" + hmac.new(secret.encode(), b"v0:" + timestamp.encode() + b":" + body, hashlib.sha256).hexdigest()
    return urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/x-www-form-urlencoded", "X-Slack-Request-Timestamp": timestamp,
        "X-Slack-Signature": signature, "User-Agent": "release-demo reset.py"})


def warm_slack_handler(reset, url):
    url = url or slack_handler_url()
    if not url:
        print("  skipped: the tools host may not look up the Slack handler's URL. "
              "Run reset.py from the Mac, which passes it on.")
        return
    if reset.dry_run:
        print("  would call the Slack handler once as Mateusz, who isn't a releaser: "
              f"it answers \"{NOT_A_RELEASER}\" and starts nothing")
        return
    ssm = ssm_client()
    form = {**WARM_UP, "user_id": get_parameter(ssm, "slack/member-id/mateusz")}
    request = signed_request(url, get_parameter(ssm, "slack/signing-secret"), form)
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            status, body = response.status, response.read().decode(errors="replace")
    except urllib.error.HTTPError as error:
        status, body = error.code, error.read().decode(errors="replace")
    took = time.monotonic() - started
    if status != 200 or NOT_A_RELEASER not in body:
        fail(f"The Slack handler answered {status}, not \"{NOT_A_RELEASER}\": {body[:200]}")
    print(f"  the Slack handler answered \"{NOT_A_RELEASER}\" in {took:.1f}s: warm")


def reset_main(dry_run, slack_url=None):
    started = time.monotonic()
    reset = Reset(dry_run)
    print(f"Reset the main demo to {SERVICE} {BASELINE}, keeping the past{' (dry run: nothing changes)' if dry_run else ''}.")
    try:
        reset.step("1. Release and rollback runs in Jenkins", lambda: stop_runs(reset, release_jobs()))
    except HistoryRunning as error:
        print(f"  {error}")
        return 1
    reset.step("2. Open changes in ServiceNow", lambda: cancel_changes(reset))
    reset.step(f"3. The take's tags and GitHub releases, after {BASELINE}", lambda: github_takes(reset))
    reset.step(f"4. The take's release copies in JFrog, after {BASELINE}", lambda: jfrog_takes(reset))
    reset.step(f"5. Jira versions after {BASELINE}, and {' and '.join(DEMO_ISSUES)}", lambda: jira_versions(reset))
    reset.step(f"6. {SERVICE} on main", lambda: stage_baseline(reset))
    reset.step("7. Lambda aliases, and one call each", lambda: lambda_aliases(reset))
    reset.step("8. The Slack handler, one call", lambda: warm_slack_handler(reset, slack_url))

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
    parser.add_argument("--slack-handler-url", help="the Slack handler's function URL; from the Mac, looked up")
    args, rest = parser.parse_known_args()

    if args.demo != "main":
        return other_demo(args.demo, [*(["--dry-run"] if args.dry_run else []), *rest])
    if rest:
        parser.error(f"unrecognized arguments: {' '.join(rest)}")
    if not args.here and not host.on_host():
        shown = ["main", *(["--dry-run"] if args.dry_run else [])]
        url = args.slack_handler_url or (None if args.follow else slack_handler_url())
        return host.hand_over("reset", "reset.py", [*shown, *(["--slack-handler-url", url] if url else [])],
                              follow_only=args.follow, title=" ".join(["reset.py", *shown]))
    if args.follow:
        fail("--follow is for the Mac. Here, read /var/log/release-demo-reset.log.")
    sys.stdout.reconfigure(line_buffering=True)
    return reset_main(args.dry_run, args.slack_handler_url)


if __name__ == "__main__":
    sys.exit(main())
