"""Put video 24 back where a take starts: production and staging on 1.12.0, 1.12.1 on main, the switch on.

    python scripts/reset.py patch-clock              from the Mac it runs itself on the tools host
    python scripts/reset.py patch-clock --dry-run    every action it would take, and nothing else
    or the Jenkins job reset with DEMO=patch-clock

The main reset's steps, as reset.py does them (stop the take's runs, cancel open changes, archive the take's
tags and release copies, un-release the Jira versions, warm the Slack handler, the other demos' switches
off), with 1.12.0's release copy kept and no stop on 1.3.0 in between: a second run finds nothing to do. Then:

1. the seed job, if orders-service-candidates/1.12.0/seed/ is empty;
2. the seed's zip, build-info.json, SBOM and sbom-info.json copied to orders-service-releases/1.12.0/;
3. both aliases moved to the Lambda version described 1.12.0 whose CodeSha256 is the seed's, and each function called once;
4. 1.12.1 staged on main;
5. ORD-[f] and ORD-[p] made if they don't exist yet, as setup.py makes them, and reopened;
6. backfill.py for pricing-api and quote-service, which released before SBOMs travelled with packages;
7. `grype db update` once, so no take waits for a download;
8. open clock files removed, and the switch on.

About 3 minutes, 2 more the first time, while the seed runs. The main reset afterwards puts production and
staging back on 1.3.0 and the switch off, and brotli is no longer live.
"""

import argparse
import importlib.util
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
SCRIPTS = HERE.parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(1, str(SCRIPTS))

import backfill  # noqa: E402
import clock  # noqa: E402
import host  # noqa: E402
import live as readers  # noqa: E402
import publish  # noqa: E402
import run_state  # noqa: E402
import seed  # noqa: E402
import setup  # noqa: E402
import tools  # noqa: E402
from common import fail, get_parameter, http_get, service, ssm_client  # noqa: E402
from switch import SWITCH  # noqa: E402

SERVICE, VERSION = seed.SERVICE, seed.VERSION
STAGED = "1.12.1"
BACKFILLED = ["pricing-api", "quote-service"]
RELEASE = f"{publish.releases_root(SERVICE)}/{VERSION}"


def shared_reset():
    """scripts/reset.py as a module, as demos/flaky/reset.py uses it: this file has its name. Its
    steps free every version after the baseline but the seed's, whose release copy the take needs."""
    spec = importlib.util.spec_from_file_location("shared_reset", SCRIPTS / "reset.py")
    shared = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(shared)
    after_baseline = shared.after_baseline
    shared.after_baseline = lambda version: after_baseline(version) and version != VERSION
    shared.SWITCHES = [name for name in shared.SWITCHES if name != SWITCH.name]
    return shared


def jcr_token():
    return get_parameter(ssm_client(), "jcr/token")


def seed_job(reset):
    if setup.seeded():
        print(f"  {seed.SEED}/ holds the package")
        return
    reset.act(f"run the job {setup.JOB} (about 2 minutes), which puts {VERSION} on both aliases", setup.run_seed)


def release_copy(reset):
    """The seed's files in the release folder, each read back as it was stored."""
    token = jcr_token()

    def copy(name):
        publish.upload(token, f"{RELEASE}/{name}", publish.download(token, f"{seed.SEED}/{name}"))

    if reset.dry_run and not setup.seeded():
        print(f"  would copy {', '.join(seed.FILES)} from {seed.SEED}/ to {RELEASE}/")
        return
    missing = [name for name in seed.FILES if publish.stored_sha256(token, f"{seed.SEED}/{name}") != publish.stored_sha256(token, f"{RELEASE}/{name}")]
    if not missing:
        print(f"  {RELEASE}/ holds the seed's {len(seed.FILES)} files")
    for name in missing:
        reset.act(f"copy {seed.SEED}/{name} to {RELEASE}/", lambda name=name: copy(name))


def seed_sha256():
    """The package's checksum, from the seed's own build-info.json."""
    return json.loads(publish.download(jcr_token(), f"{seed.SEED}/build-info.json"))["sha256"]


def lambda_aliases(reset):
    """Both aliases on the Lambda version of 1.12.0 that holds the seed's package, and one call each."""
    from check import wait_for_version
    from deploy import lambda_client
    from evidence import base64_to_hex

    if reset.dry_run and not setup.seeded():
        print("  would move both live aliases to the version the seed publishes, and call each function once")
        return
    wanted = seed_sha256()
    client = lambda_client(SERVICE)
    urls = {}
    for environment in service(SERVICE)["environments"].values():
        function = environment["function"]
        versions = []
        for page in client.get_paginator("list_versions_by_function").paginate(FunctionName=function):
            versions += [v for v in page["Versions"] if v["Version"] != "$LATEST" and v.get("Description") == VERSION
                         and base64_to_hex(v["CodeSha256"]) == wanted]
        if not versions:
            fail(f"{function} has no Lambda version of {VERSION} with the seed's checksum. Delete {seed.SEED}/ in JFrog as admin and run the reset again.")
        target = max(versions, key=lambda v: int(v["Version"]))["Version"]
        current = client.get_alias(FunctionName=function, Name="live")["FunctionVersion"]
        if current == target:
            print(f"  {function}: live is on version {target} ({VERSION})")
        else:
            reset.act(f"move {function} live from version {current} to {target} ({VERSION})",
                      lambda function=function, target=target: client.update_alias(FunctionName=function, Name="live", FunctionVersion=target))
        urls[function] = client.get_function_url_config(FunctionName=function, Qualifier="live")["FunctionUrl"].rstrip("/")
    for function, url in urls.items():
        if reset.dry_run:
            print(f"  would call {function} once: /health, then the page")
            continue
        current, seen = wait_for_version(url, VERSION, 30)
        status, _ = http_get(url + "/", timeout=15)
        print(f"  {function}: /health reports {seen or 'nothing'}, the page answered {status or 'nothing'}")
        if not current or status != 200:
            fail(f"{function} doesn't serve {VERSION} yet.")


def stage_state(reset):
    current = host.main_version(service(SERVICE)["path"])
    if current == STAGED:
        print(f"  main has {SERVICE} {STAGED}")
        return
    reset.act(f"stage {SERVICE} {STAGED} on main (main has {current})", lambda: host.stage(SERVICE, STAGED))


def reopen_issues(reset):
    from jira import Jira

    jira = Jira()
    issues = {summary: setup.find(jira, summary) for summary in setup.summaries()}
    if not all(issues.values()):
        reset.act("make ORD-[f] and ORD-[p] in Jira, whichever is missing", setup.issues)
    for found, name in zip(issues.values(), setup.NAMES):
        if not found:
            continue  # just made, and open
        status = found["fields"]["status"]
        if status.get("statusCategory", {}).get("key") != "done":
            print(f"  {found['key']} is {name}: {status['name']}")
            continue

        def reopen(key=found["key"]):
            if not (jira.transition(key, "Backlog") or jira.transition(key, "To Do")):
                fail(f"{key} has no transition to Backlog or To Do.")

        reset.act(f"reopen {found['key']} ({status['name']})", reopen)


def grype_database(reset):
    """Not an action to undo: the newest database, so a take downloads nothing."""
    if reset.dry_run:
        print("  would run grype db update once")
        return
    print(f"  {tools.db_update() or 'updated'}")
    print(f"  built {tools.db_status().get('built')}")


def end_state(reset):
    """Open clocks gone, and the switch on."""
    for opened in clock.open_clocks():
        reset.act(f"remove the open clock of {opened['id']}", lambda name=opened["id"]: clock.path_of(name).unlink())
    if SWITCH.is_on():
        print(f"  {SWITCH.name}: on")
    else:
        reset.act(f"turn {SWITCH.name} on", lambda: SWITCH.set("on"))


def live_with_sboms():
    """How many services with live: have an SBOM stored with what is live."""
    client = readers.reader_client()
    jfrog = readers.JFrog(jcr_token())
    count = 0
    for name, block in readers.searchable().items():
        sbom, why_not = readers.stored_sbom(jfrog, name, readers.read_live(client, block))
        print(f"  {name}: {why_not or 'an SBOM stored with what is live'}")
        count += sbom is not None
    return count


def run(dry_run, slack_url):
    started = time.monotonic()
    shared = shared_reset()
    reset = shared.Reset(dry_run)
    print(f"Reset video 24: production and staging on {VERSION}, {STAGED} on main, the switch on"
          f"{' (dry run: nothing changes)' if dry_run else ''}.")
    try:
        reset.step("Release and rollback runs in Jenkins", lambda: shared.stop_runs(reset, shared.release_jobs()))
    except shared.HistoryRunning as error:
        print(f"  {error}")
        return 1
    reset.step("Open changes in ServiceNow", lambda: shared.cancel_changes(reset))
    reset.step(f"The take's tags and GitHub releases, after {shared.BASELINE}", lambda: shared.github_takes(reset))
    reset.step(f"The take's release copies in JFrog, after {shared.BASELINE}, but {VERSION}", lambda: shared.jfrog_takes(reset))
    reset.step(f"Jira versions after {shared.BASELINE}, and {' and '.join(shared.DEMO_ISSUES)}", lambda: shared.jira_versions(reset))
    reset.step("The Slack handler, one call", lambda: shared.warm_slack_handler(reset, slack_url))
    reset.step("The other demos' switches", lambda: shared.switches_off(reset))
    reset.step(f"1. The seed in {seed.SEED}/", lambda: seed_job(reset))
    reset.step(f"2. {VERSION}'s release copy", lambda: release_copy(reset))
    reset.step(f"3. Lambda aliases on {VERSION}, and one call each", lambda: lambda_aliases(reset))
    reset.step(f"4. {SERVICE} {STAGED} on main", lambda: stage_state(reset))
    reset.step("5. ORD-[f] and ORD-[p]", lambda: reopen_issues(reset))
    reset.step(f"6. SBOMs of {' and '.join(BACKFILLED)}", lambda: backfill.backfill(BACKFILLED, reset.act))
    reset.step("7. Grype's database", lambda: grype_database(reset))
    reset.step("8. Open clocks and the switch", lambda: end_state(reset))
    counted = []
    if not dry_run:
        reset.step("Live services with SBOMs", lambda: counted.append(live_with_sboms()))
    took = run_state.duration(time.monotonic() - started)
    if reset.failed:
        print(f"Finished in {took}, but these steps failed: {'; '.join(reset.failed)}. Fix them and run it again.")
        return 1
    if dry_run:
        print(f"Dry run in {took}: {reset.actions} action(s) to take.")
        return 0
    print(f"Reset in {took}: {reset.actions} action(s).")
    print(f"Ready for a take: production on {VERSION}, {STAGED} on main, {counted[0]} live services with SBOMs.")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="print every action, change nothing")
    parser.add_argument("--here", action="store_true", help="run here, not on the tools host")
    parser.add_argument("--slack-handler-url", help="the Slack handler's function URL; from the Mac, looked up")
    args = parser.parse_args()
    if not args.here and not host.on_host():
        url = args.slack_handler_url or shared_reset().slack_handler_url()
        shown = [*(["--dry-run"] if args.dry_run else [])]
        return host.hand_over("reset", "demos/patch_clock/reset.py", [*shown, *(["--slack-handler-url", url] if url else [])],
                              title=" ".join(["reset.py patch-clock", *shown]))
    sys.stdout.reconfigure(line_buffering=True)
    return run(args.dry_run, args.slack_handler_url)


if __name__ == "__main__":
    sys.exit(main())
