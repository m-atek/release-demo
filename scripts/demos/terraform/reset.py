"""Put demo 2 back where a take starts, in under two minutes, safe to run twice.

    python scripts/demos/terraform/reset.py             reset
    python scripts/demos/terraform/reset.py --dry-run   say what it would do and show the plan, change nothing

1. Closes the demo's open pull requests: ORD-22 in the title, or from the demo branch.
2. Resets the branch demo-2/store-exports to main.
3. Removes exports.tf from main with one commit, through the GitHub App, which
   the ruleset on main lets past its required checks.
4. Cancels the network-stack changes still open in ServiceNow, and resolves an
   open drift incident.
5. Applies main's stack as the apply role: the bucket and queue go, and so
   does any rule added by hand, so the stack is back on its baseline.

Runs Terraform from the Mac (brew install hashicorp/tap/terraform), or in the
toolchain image wherever terraform is on the path.
"""

import argparse
import subprocess
import sys
import time

import stack
import github
from common import ROOT, fail

COMMIT_MESSAGE = "demo: reset network-stack to its baseline [skip ci]"


def pulls(gh, config, dry_run):
    """Close the demo's open pull requests, and point its branch back at main."""
    main = gh.branch_sha("main")
    for pull in gh.open_pulls():
        if config["demo_branch"] == pull["head"]["ref"] or "ORD-22" in pull["title"]:
            print(f"  close pull request #{pull['number']}: {pull['title']}")
            if not dry_run:
                gh.close_pull(pull["number"], "Closed by reset.py terraform, to start the demo again.")
    branch = gh.branch_sha(config["demo_branch"])
    if branch and branch != main:
        print(f"  reset {config['demo_branch']} to main")
        if not dry_run:
            gh.set_branch(config["demo_branch"], main, force=True)
    if gh.file_at(config["demo_file"], main) is not None:
        print(f"  remove {config['demo_file']} from main: \"{COMMIT_MESSAGE}\"")
        if not dry_run:
            commit = gh.commit_files(main, {config["demo_file"]: None}, COMMIT_MESSAGE)
            gh.set_branch("main", commit)
    else:
        print(f"  main has no {config['demo_file']}")


def servicenow(dry_run):
    """Cancel open network-stack changes; resolve an open drift incident. ServiceNow
    asleep or unreachable doesn't stop the reset: it says so and goes on."""
    from change import CANCELED, connect
    from drift import INCIDENT_TITLE

    try:
        sn, _ = connect()
        item = sn.find("cmdb_ci_appl", "name=network-stack", "sys_id")
        if not item:
            print("  no CMDB item network-stack yet: nothing to cancel")
            return
        changes = sn.call("GET", "/api/now/table/change_request", sysparm_query=f"cmdb_ci={item['sys_id']}^active=true",
                          sysparm_fields="sys_id,number,type")
        for change in changes:
            print(f"  cancel {change['number']}")
            if not dry_run:
                sn.call("PATCH", f"/api/sn_chg_rest/change/{change['type'] or 'normal'}/{change['sys_id']}",
                        {"state": CANCELED, "close_notes": "Cancelled by reset.py terraform, to start the demo again."})
        incidents = sn.call("GET", "/api/now/table/incident", sysparm_fields="sys_id,number",
                            sysparm_query=f"active=true^short_description={INCIDENT_TITLE}")
        for incident in incidents:
            print(f"  resolve {incident['number']}")
            if not dry_run:
                sn.call("PATCH", f"/api/now/table/incident/{incident['sys_id']}", {
                    "state": "6", "close_code": "Solution provided",
                    "close_notes": "reset.py terraform applied main again, which removed the change made by hand."})
    except SystemExit:
        print("  ServiceNow didn't answer (asleep?): open changes and incidents stay as they are")


def baseline(dry_run):
    """main's stack, applied as the apply role. Fetched first, so the reset applies what
    main has on GitHub now, not an older checkout."""
    folder = ROOT / ".local" / "demo-2" / "reset"
    path = stack.relative(stack.STACK)
    fetched = subprocess.run(["git", "fetch", "--quiet", "origin", "main"], cwd=ROOT, capture_output=True)
    ref = "FETCH_HEAD" if fetched.returncode == 0 else "HEAD"
    if ref == "HEAD":
        print("  couldn't fetch main from GitHub: applying this checkout's stack instead")
    subprocess.run(["rm", "-rf", str(folder / "code")], check=True)
    (folder / "code").mkdir(parents=True)
    archive = subprocess.run(["git", "archive", "--format=tar", ref, "--", path], cwd=ROOT, capture_output=True,
                             check=True)
    subprocess.run(["tar", "-x", "-C", str(folder / "code")], input=archive.stdout, check=True)
    code = folder / "code" / path
    if (code / "exports.tf").exists() and not dry_run:
        fail("main still has exports.tf: the reset commit didn't land. Run reset.py again.")

    stack.write_backend()
    stack.write_credentials("apply")
    stack.init(folder / "data", stack.PROFILES["apply"], cwd=code)
    if dry_run:
        stack.terraform("plan", "-input=false", data_dir=folder / "data", cwd=code, profile=stack.PROFILES["apply"])
        return
    stack.terraform("apply", "-input=false", "-auto-approve", data_dir=folder / "data", cwd=code,
                    profile=stack.PROFILES["apply"])


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="say what it would do, and plan only")
    args = parser.parse_args()
    started = time.time()
    config = stack.settings()

    print("GitHub")
    pulls(github.GitHub(), config, args.dry_run)
    print("ServiceNow")
    servicenow(args.dry_run)
    print("The stack")
    baseline(args.dry_run)
    print(f"{'Dry run' if args.dry_run else 'Reset'} done in {stack.run_state.duration(time.time() - started)}.")


if __name__ == "__main__":
    sys.exit(main())
