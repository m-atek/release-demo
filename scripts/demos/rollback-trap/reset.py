"""Put demo 20 back where a take starts: production and staging on 1.6.0, both databases at baseline.

    python scripts/reset.py rollback-trap                              checks off, 1.6.1 staged (take A)
    python scripts/reset.py rollback-trap --checks on --stage 1.6.2    take C
    python scripts/reset.py rollback-trap --dry-run                    every action it would take, and nothing else

From the Mac it hands itself over to the tools host, like reset.py; the
reset job runs it there with DEMO=rollback-trap and OPTIONS. Steps:

1. the take's release and rollback runs stopped, open changes cancelled;
2. the main reset's archiving for whatever the takes made after 1.6.0: tags
   orders-service-v1.6.1 to v1.7.1 and their GitHub releases under takes/,
   JFrog release copies under takes/, Jira versions un-released, ORD-[t]
   reopened; and 1.6.0's own tag and release copy put back if an earlier
   reset archived them, because the video and the migration check read them;
3. Neon's staging and production branches restored from baseline, so
   schema_migrations lists 0001 and 0002 again (each take's copy of the
   database is overwritten; its Slack, ServiceNow, Jenkins and JFrog records
   stay);
4. both aliases on the 1.6.0 versions setup.py recorded;
5. the checks on or off (checks.py) and the version staged (stage.py);
6. both functions called once, so neither the function nor Neon's compute
   starts cold, and the Slack handler warmed; with the checks on, the squawk
   image built, so the first migration check doesn't wait for it.

Under two minutes, safe to run twice.
"""

import argparse
import importlib.util
import json
import sys
import time

import trap  # first: it puts scripts/ on the path, where host and common live
import host  # noqa: E402
from common import get_parameter, github_api, github_app_token, http_get, service, ssm_client  # noqa: E402

BASELINE = "1.6.0"
STAGES = ["1.6.1", "1.6.2", "1.7.0", "1.7.1"]


def shared_reset():
    """scripts/reset.py as a module, its steps pointed at 1.6.0. Its lambda_aliases
    imports the shared deploy.py, which this folder's deploy.py shadows by name."""
    saved = sys.modules.get("deploy")
    sys.modules["deploy"] = trap.shared("deploy")
    try:
        spec = importlib.util.spec_from_file_location("shared_reset", trap.SCRIPTS / "reset.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        if saved is None:
            sys.modules.pop("deploy", None)
        else:
            sys.modules["deploy"] = saved
    module.BASELINE = BASELINE
    return module


def take_issue():
    from setup import ISSUES
    from jira import PROJECT, Jira

    summary = ISSUES["t"][0]
    found = [i["key"] for i in Jira().search(f'project = {PROJECT} AND summary ~ "\\"{summary}\\""', fields="summary")
             if i["fields"]["summary"] == summary]
    if not found:
        raise RuntimeError(f'No Jira issue "{summary}" yet: run scripts/demos/rollback-trap/setup.py once.')
    return found[0]


def baseline_tag(reset):
    """orders-service-v1.6.0, which the video's git show reads: back on its commit if an earlier reset archived it."""
    token, repository, _ = github_app_token(ssm_client())
    tag = f"{trap.SERVICE}-v{BASELINE}"

    def refs(prefix):
        found = github_api("GET", f"/repos/{repository}/git/matching-refs/tags/{prefix}", token, scheme="token") or []
        return {ref["ref"].removeprefix("refs/tags/"): ref["object"]["sha"] for ref in found}

    if tag in refs(tag):
        print(f"  {tag}: there")
        return
    names = refs(f"takes/{tag}-")
    archived = sorted(names)
    if not archived:
        raise RuntimeError(f"No tag {tag}, and none archived: run scripts/demos/rollback-trap/setup.py.")
    sha = names[archived[-1]]
    reset.act(f"tag {sha[:7]} as {tag} again (from {archived[-1]})", lambda: github_api(
        "POST", f"/repos/{repository}/git/refs", token, {"ref": f"refs/tags/{tag}", "sha": sha}, scheme="token"))


def baseline_release_copy(reset, versions):
    """1.6.0's release copy in JFrog, which the migration check compares with: from the
    exact package production's 1.6.0 runs (Lambda's CodeSha256) if an earlier reset archived it."""
    import base64
    import hashlib
    import urllib.request

    publish = trap.shared("publish")
    token = get_parameter(ssm_client(), "jcr/token")
    base = f"{publish.releases_root(trap.SERVICE)}/{BASELINE}"
    name = f"{trap.SERVICE}-{BASELINE}.zip"
    if publish.stored_sha256(token, f"{base}/{name}"):
        print(f"  {base}/{name}: there")
        return
    client = trap.lambda_client()
    function = trap.environment("production")["function"]

    def upload():
        code = client.get_function(FunctionName=function, Qualifier=versions["production"])
        with urllib.request.urlopen(code["Code"]["Location"], timeout=60) as response:
            package = response.read()
        expected = base64.b64decode(code["Configuration"]["CodeSha256"]).hex()
        if hashlib.sha256(package).hexdigest() != expected:
            raise RuntimeError(f"{function} version {versions['production']}'s code doesn't match its CodeSha256.")
        publish.upload(token, f"{base}/{name}", package)

    reset.act(f"put {base}/{name} back, from {function} version {versions['production']} (SHA-256 checked)", upload)


def aliases(reset, versions):
    shared = trap.shared("deploy")
    client = trap.lambda_client()
    for name in ("staging", "production"):
        function = trap.environment(name)["function"]
        live = shared.alias_version(client, function)
        target = versions[name]
        if live == target:
            print(f"  {function}: live is on version {target} ({BASELINE})")
            continue
        reset.act(f"move {function} live from version {live} to {target} ({BASELINE})",
                  lambda function=function, target=target: shared.move_alias(client, function, target))


def warm(reset):
    from check import wait_for_version

    client = trap.lambda_client()
    for name in ("staging", "production"):
        function = trap.environment(name)["function"]
        url = client.get_function_url_config(FunctionName=function, Qualifier="live")["FunctionUrl"].rstrip("/")
        if reset.dry_run:
            print(f"  would call {function} once: /health, then the page, which wakes Neon's compute")
            continue
        live, seen = wait_for_version(url, BASELINE, 30)
        status, _ = http_get(url + "/", timeout=20)
        print(f"  {function}: /health reports {seen or 'nothing'}, the page answered {status or 'nothing'}")
        if not live or status != 200:
            raise RuntimeError(f"{function} doesn't serve {BASELINE}'s page yet.")


def run(args):
    started = time.monotonic()
    shared = shared_reset()
    reset = shared.Reset(args.dry_run)
    on = args.checks == "on"
    print(f"Reset demo 20: {trap.SERVICE} {BASELINE} in staging and production, both databases at baseline, "
          f"checks {args.checks}, {args.stage} staged{' (dry run: nothing changes)' if args.dry_run else ''}.")
    try:
        reset.step("1. Release and rollback runs in Jenkins", lambda: shared.stop_runs(reset, shared.release_jobs()))
    except shared.HistoryRunning as error:
        print(f"  {error}")
        return 1
    versions = json.loads(get_parameter(ssm_client(), f"{trap.SERVICE}/baseline-versions"))
    reset.step("2. Open changes in ServiceNow", lambda: shared.cancel_changes(reset))

    def github():
        shared.github_takes(reset)
        baseline_tag(reset)

    def jfrog():
        shared.jfrog_takes(reset)
        baseline_release_copy(reset, versions)

    reset.step(f"3. The takes' tags and GitHub releases, after {BASELINE}, and {BASELINE}'s own tag", github)
    reset.step(f"4. The takes' release copies in JFrog, after {BASELINE}, and {BASELINE}'s own", jfrog)

    def jira():
        shared.DEMO_ISSUES = [take_issue()]
        shared.jira_versions(reset)

    reset.step(f"5. Jira versions after {BASELINE}, and ORD-[t]", jira)
    reset.step("6. Lambda aliases on 1.6.0", lambda: aliases(reset, versions))
    import checks
    import neon

    reset.step("7. Neon: staging and production from baseline",
               lambda: reset.act("restore Neon's staging and production branches from baseline", neon.restore))

    reset.step(f"8. The checks {args.checks}", lambda: print(f"  {checks.switch(on, args.dry_run)}"))

    def stage():
        current = host.main_version(service(trap.SERVICE)["path"])
        if current == args.stage:
            print(f"  main has {trap.SERVICE} {args.stage}")
            return
        reset.act(f"stage {trap.SERVICE} {args.stage} on main (main has {current})",
                  lambda: host.stage(trap.SERVICE, args.stage))

    reset.step(f"9. {trap.SERVICE} {args.stage} on main", stage)
    reset.step("10. Both functions, one call each", lambda: warm(reset))
    reset.step("11. The Slack handler, one call", lambda: shared.warm_slack_handler(reset, args.slack_handler_url))
    if on:
        import migration_check

        reset.step("12. The squawk image",
                   lambda: reset.act("build the squawk image if it isn't there", migration_check.image))
    took = time.monotonic() - started
    if reset.failed:
        print(f"Finished in {took:.0f}s, but these steps failed: {'; '.join(reset.failed)}. Fix them and run it again.")
        return 1
    print(f"{'Dry run' if args.dry_run else 'Reset'} in {took:.0f}s: {reset.actions} action(s). "
          f"{'' if args.dry_run else 'Ready for a take.'}".rstrip())
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--checks", choices=["on", "off"], default="off")
    parser.add_argument("--stage", choices=STAGES, default="1.6.1")
    parser.add_argument("--dry-run", action="store_true", help="print every action, change nothing")
    parser.add_argument("--here", action="store_true", help="run here, not on the tools host")
    parser.add_argument("--slack-handler-url", help="the Slack handler's function URL; from the Mac, looked up")
    args = parser.parse_args()
    if not args.here and not host.on_host():
        shared = shared_reset()
        url = args.slack_handler_url or shared.slack_handler_url()
        shown = ["--checks", args.checks, "--stage", args.stage, *(["--dry-run"] if args.dry_run else [])]
        return host.hand_over("reset", "demos/rollback-trap/reset.py",
                              [*shown, *(["--slack-handler-url", url] if url else [])],
                              title=" ".join(["reset.py rollback-trap", *shown]))
    sys.stdout.reconfigure(line_buffering=True)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
