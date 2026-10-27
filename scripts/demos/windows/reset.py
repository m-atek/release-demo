"""Demo 1 back to its starting state, and its bootstrap.

    python scripts/demos/windows/reset.py               all six on 3.2.0, the drift planted on STORE-004
    python scripts/demos/windows/reset.py --fix-drift   restore UploadEndpoint on STORE-004, before the resume
    python scripts/demos/windows/reset.py --bootstrap   the same as reset, for a new fleet (up runs it)

The fleet part is one Run Command for all six servers (bootstrap.ps1): the
baseline from JCR, verified by the SHA-256 services.yaml pins, as the service
StoreSync, and the store's own config, without UploadEndpoint on the drifted
server. The tiles in Grafana go back to "waiting" on 3.2.0. A reset also
cancels store-sync changes still open and reopens ORD-31 with its version
unreleased. Like the main reset it keeps the past: earlier changes, notes and
events stay, and a take that releases 3.3.0 again finds its tag and its
release copy in place (the build is reproducible, so the copy matches). Safe
to run twice; under two minutes on a running fleet.
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
sys.path.insert(0, str(HERE))
import fleet_events  # noqa: E402
import waves  # noqa: E402
from common import ROOT, service  # noqa: E402
from fleet import Fleet, describe_failure  # noqa: E402

SERVICE = "store-sync"
JIRA_KEY, RELEASE = "ORD-31", "3.3.0"


def bootstrap(fleet, ids):
    """The baseline on every server in ids, the drift planted. Returns True when all are healthy."""
    env = fleet.env
    baseline = env["baseline"]
    results = fleet.run(ids, "bootstrap", f"store-sync: bootstrap {len(ids)} store servers on {baseline['version']}",
                        timeout=900, JcrUrl=fleet.jcr_url(), Servers=fleet.table(ids, drift=True),
                        BaselineVersion=baseline["version"], BaselinePath=baseline["path"],
                        BaselineSha256=baseline["sha256"])
    ok = True
    for name, result in sorted(results.items()):
        healthy = result.ok and result.result.get("ok")
        ok &= bool(healthy)
        drift = ", drift planted (no UploadEndpoint)" if result.result.get("drift") else ""
        print(f"  {name}: {'on ' + str(result.result.get('version')) + drift if healthy else 'FAILED: ' + describe_failure(result)}")
        fleet_events.tile(SERVICE, name, waves.WAITING if healthy else waves.FAILED, result.result.get("version"))
    total = len(env["servers"])
    fleet_events.progress(SERVICE, f"{len(env['waves'])} waves, not started", 0, total, RELEASE)
    return ok


def fix_drift(fleet):
    drifted = {name: fleet.running([name])[name] for name in fleet.env.get("drift", {})}
    results = fleet.run(drifted, "config", f"store-sync: restore UploadEndpoint on {', '.join(drifted)}", timeout=120,
                        Servers=fleet.table(drifted, drift=False))
    for name, result in results.items():
        print(f"  {name}: store.config has {', '.join(result.result.get('config_keys', [])) or 'nothing'}"
              if result.ok else f"  {name}: FAILED: {describe_failure(result)}")
    return all(r.ok for r in results.values())


def cancel_open_changes():
    """Cancel store-sync changes a stopped take left open, through the pipeline's own user."""
    try:
        import change
        sn, _ = change.connect()
    except SystemExit:
        print("  ServiceNow: not reachable, skipped")
        return
    rows = sn.call("GET", "/api/now/table/change_request", sysparm_fields="sys_id,number,type",
                   sysparm_query=f"short_descriptionSTARTSWITHRelease {SERVICE} ^stateNOT IN3,4,7")
    for row in rows:
        change.cancel(sn, {"change": {"sys_id": row["sys_id"], "type": row["type"] or "normal"}},
                      "Cancelled by reset.py windows: the demo went back to its starting state.")
        print(f"  ServiceNow: {row['number']} cancelled")
    if not rows:
        print("  ServiceNow: no open store-sync change")


def shared(*args):
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / args[0]), *args[1:]], capture_output=True, text=True)
    print("  " + (result.stdout.strip() or result.stderr.strip()[-200:]).replace("\n", "\n  "))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    which = parser.add_mutually_exclusive_group()
    which.add_argument("--fix-drift", action="store_true", help="restore UploadEndpoint on the drifted server")
    which.add_argument("--bootstrap", action="store_true", help="the fleet part only, for a new fleet")
    args = parser.parse_args()

    started = time.monotonic()
    fleet = Fleet(service(SERVICE))
    if args.fix_drift:
        sys.exit(0 if fix_drift(fleet) else 1)

    ids = fleet.running()
    missing = [n for n in fleet.env["servers"] if n not in ids]
    if missing:
        print(f"Not running: {', '.join(missing)}. Start the fleet: scripts/demo.sh up windows")
        sys.exit(1)
    print(f"Fleet: {fleet.env['baseline']['version']} on every server")
    ok = bootstrap(fleet, ids)
    if not args.bootstrap:
        print("Records")
        cancel_open_changes()
        shared("jira.py", "reopen", JIRA_KEY, f"{SERVICE} {RELEASE}")
    print(f"{'Done' if ok else 'Not all servers are on the baseline'} in {waves.duration(time.monotonic() - started)}.")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
