"""Put video 10 back where a take starts.

    python scripts/reset.py leaked-token              from the Mac: runs on the tools host, prints its log
    python scripts/reset.py leaked-token --dry-run    every action it would take, and nothing else

1. The main reset: production and staging on orders-service 1.3.0, earlier
   takes archived, ORD-12 reopened (scripts/reset.py).
2. orders-service 1.4.0 staged on main, for the release on the new token.
3. JFrog: the token in SSM (jcr/token) must work; a take's token left live
   by a take that never rotated is revoked, so release-pipeline holds the
   pipeline's token only.
4. ServiceNow: a leak incident a take left open is resolved with the note
   "Resolved by reset.py before the next take."

Safe to run twice: the second run finds nothing to do. It needs Jenkins and
JFrog, which answer only on the tools host, so from the Mac it hands itself
over as the main reset does.
"""

import argparse
import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import take  # noqa: E402
from common import ROOT, ssm_client  # noqa: E402

NOTE = "Resolved by reset.py before the next take."
VERSION = "1.4.0"


def shared_check():
    """This folder's check.py would shadow scripts/check.py, which the main reset
    imports as check: Python puts a script's own folder first on its path."""
    spec = importlib.util.spec_from_file_location("check", ROOT / "scripts" / "check.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["check"] = module
    spec.loader.exec_module(module)


def main_reset():
    shared_check()
    spec = importlib.util.spec_from_file_location("shared_reset", ROOT / "scripts" / "reset.py")
    shared = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(shared)
    return shared


def step_stage(reset, shared):
    import host

    current = host.main_version(shared.service(shared.SERVICE)["path"])
    if current == VERSION:
        print(f"  main has {shared.SERVICE} {VERSION} already")
        return
    reset.act(f"stage {shared.SERVICE} {VERSION} on main (main has {current})", lambda: host.stage(shared.SERVICE, VERSION))


def step_jfrog(reset, client=None, ssm=None):
    from common import get_parameter
    from jcr_setup import token_works
    from rotators import jwt_claims, short_id

    ssm = ssm or ssm_client()
    client = client or take.admin_client(ssm)
    current = get_parameter(ssm, "jcr/token")
    if not token_works(client, current):
        raise RuntimeError("The JFrog token in SSM doesn't work. Give the pipeline a new one: "
                           "scripts/rotate.py jcr --reason \"reset found it dead\"")
    keep = (jwt_claims(current) or {}).get("jti")
    print(f"  jcr/token works ({take.describe(current)})")
    strays = [t["token_id"] for t in take.user_tokens(client) if t["token_id"] != keep]
    if not strays:
        print(f"  {take.USER} holds no other token")
    for token_id in strays:
        reset.act(f"revoke a take's token of {take.USER} still live ({short_id(token_id)})",
                  lambda token_id=token_id: take.revoke(client, token_id))


def step_incidents(reset, sn=None):
    from rotators import records

    if sn is None:
        from change import connect

        sn, _ = connect()
    open_ones = records.open_leak_incidents(sn)
    if not open_ones:
        print("  no leak incident open")
    for incident in open_ones:
        reset.act(f"resolve {incident['number']} ({incident['short_description']})",
                  lambda incident=incident: records.resolve(sn, incident, NOTE))


def run(dry_run, slack_url=None):
    """The main reset's steps, with 1.4.0 staged in place of 1.3.0, then video 10's own."""
    shared = main_reset()
    reset = shared.Reset(dry_run)
    service = shared.SERVICE
    print(f"Reset video 10: production on {service} {shared.BASELINE}, {VERSION} staged, keeping the past"
          f"{' (dry run: nothing changes)' if dry_run else ''}.")
    try:
        reset.step("1. Release and rollback runs in Jenkins", lambda: shared.stop_runs(reset, shared.release_jobs()))
    except shared.HistoryRunning as error:
        print(f"  {error}")
        return 1
    reset.step("2. Open changes in ServiceNow", lambda: shared.cancel_changes(reset))
    reset.step(f"3. The take's tags and GitHub releases, after {shared.BASELINE}", lambda: shared.github_takes(reset))
    reset.step(f"4. The take's release copies in JFrog, after {shared.BASELINE}", lambda: shared.jfrog_takes(reset))
    reset.step(f"5. Jira versions after {shared.BASELINE}, and {' and '.join(shared.DEMO_ISSUES)}",
               lambda: shared.jira_versions(reset))
    reset.step(f"6. {service} {VERSION} staged on main", lambda: step_stage(reset, shared))
    reset.step("7. Lambda aliases, and one call each", lambda: shared.lambda_aliases(reset))
    reset.step("8. The Slack handler, one call", lambda: shared.warm_slack_handler(reset, slack_url))
    reset.step(f"9. JFrog tokens of {take.USER}", lambda: step_jfrog(reset))
    reset.step("10. Leak incidents in ServiceNow", lambda: step_incidents(reset))
    if reset.failed:
        print(f"Not ready: {'; '.join(reset.failed)}. The lines above say why.")
        return 1
    print("Dry run: " + f"{reset.actions} action(s) to take." if dry_run else
          f"Ready for a take, {reset.actions} action(s): production on {shared.BASELINE}, {VERSION} staged, "
          "one working token, no leak incident open.")
    return 0


def main():
    import host

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--here", action="store_true", help="run here, not on the tools host")
    parser.add_argument("--slack-handler-url")
    args = parser.parse_args()
    if not args.here and not host.on_host():
        shared = main_reset()
        url = args.slack_handler_url or shared.slack_handler_url()
        shown = ["--dry-run"] if args.dry_run else []
        return host.hand_over("reset", "demos/leaked-token/reset.py", [*shown, *(["--slack-handler-url", url] if url else [])],
                              title=" ".join(["reset.py leaked-token", *shown]))
    sys.stdout.reconfigure(line_buffering=True)
    return run(args.dry_run, args.slack_handler_url)


if __name__ == "__main__":
    sys.exit(main())
