"""Put demo 3 back to its starting state: all four servers on 2.7.3, Java 8
planted on inv-dc-01 again. Safe to run twice; about a minute.

    python scripts/demos/linux/reset.py              (scripts/reset.py linux)
    python scripts/demos/linux/reset.py --no-drift   Java 11 on inv-dc-01 too, so 2.8.0 goes through

Runs from the Mac (the data center through multipass exec) or the tools host
(through the Jenkins job inventory-dc-wave). The servers keep their release
folders: going back to 2.7.3 is the symlink and a Tomcat restart, and a
server without the 2.7.3 folder gets it from demo-states. Then, where those
accounts are set up: ORD-44 reopened with "inventory 2.8.0" unreleased, and
any inventory change still open cancelled. main must hold 2.8.0 for the take:
this says so if it doesn't.
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import fleet  # noqa: E402
from common import ROOT, service, version_of  # noqa: E402
from demo import STARTING, activate, starting_package  # noqa: E402

TAKE = "2.8.0"
JIRA_KEY = "ORD-44"


def cancel_open_changes():
    import change

    sn, _ = change.connect()
    rows = sn.call("GET", "/api/now/table/change_request", sysparm_fields="sys_id,number,type",
                   sysparm_query="short_descriptionSTARTSWITHRelease inventory ^stateNOT IN3,4,7")
    for row in rows:
        kind = row.get("type") or "normal"
        sn.call("PATCH", f"/api/sn_chg_rest/change/{kind}/{row['sys_id']}",
                {"state": "4", "close_notes": "Cancelled by reset.py linux before a take."})
        print(f"  {row['number']}: cancelled")
    if not rows:
        print("  no open inventory changes")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--no-drift", action="store_true", help="Java 11 on inv-dc-01 too")
    parser.add_argument("--servers-only", action="store_true", help="skip Jira and ServiceNow")
    args = parser.parse_args()
    started = time.monotonic()

    print(f"Java: 11 on every server{'' if args.no_drift else ', 8 on ' + fleet.DRIFT_SERVER + ' (the planted drift)'}")
    baseline_tags_only(not args.no_drift)
    print(f"Every server to {STARTING}")
    ok, results = activate(STARTING, starting_package())
    for row in results:
        print(f"  {row['server']:<14} {row['result']}: {row['detail']}")
    if not ok or len(results) != len(fleet.servers()):
        sys.exit("Not every server is on 2.7.3: see above")

    if not args.servers_only:
        print(f"Jira: {JIRA_KEY} reopened, inventory {TAKE} unreleased")
        subprocess.run([sys.executable, str(ROOT / "scripts" / "jira.py"), "reopen", JIRA_KEY, f"inventory {TAKE}"])
        print("ServiceNow: open inventory changes")
        try:
            cancel_open_changes()
        except SystemExit as error:
            print(f"  skipped: {error}")

    staged = version_of(service(fleet.SERVICE)["dir"])
    if staged != TAKE:
        print(f"main holds inventory {staged}; the take releases {TAKE}: scripts/stage.py inventory {TAKE}")
    print(f"Reset in {time.monotonic() - started:.0f}s")


def baseline_tags_only(drift):
    """Only the java tag of the baseline: the drift, planted or removed."""
    extra = {"java_drift_servers": [fleet.DRIFT_SERVER] if drift else [], "rollout_serial": 4}
    from demo import dc_secrets

    ok, _ = fleet.run("baseline.yml", extra, tags="java", stop_on_failure=False, secrets=dc_secrets())
    if not ok:
        sys.exit("Couldn't set Java on every server: see above")


if __name__ == "__main__":
    main()
