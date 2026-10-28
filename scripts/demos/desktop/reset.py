"""Put demo 6 back to its starting state, for a retake or a call. Safe to run twice.

    python scripts/reset.py desktop                                   the starting state
    python scripts/reset.py desktop --pilot-ssh admin@192.168.64.5    and run the pilot Mac's check-in over SSH
    python scripts/reset.py desktop --dry-run                         every action it would take, and nothing else

The starting state: the Jamf policy Store Assistant on the 1.6.0 package,
scoped to Pilot, its logs flushed; the pilot Mac and every other managed Mac
on 1.6.0; the Windows test instance on 1.6.0 when it runs; no open change
for store-assistant; ORD-71 open and the versions after 1.6.0 unreleased in
Jira; and the bugged 1.7.0 staged on main.

Like the main demo's reset (scripts/reset.py), it keeps the past: the take's
tags and GitHub releases after 1.6.0 move to takes/<tag>-<UTC time>, and its
release copies in JFrog to the releases folder's takes/ (on the tools host,
where JFrog answers; from the Mac that step is skipped, and nothing depends
on it). Those steps are the main reset's own, for store-assistant.

The pilot Mac installs 1.6.0 at its next check-in. With --pilot-ssh the
check-in runs at once (pilot_setup.sh lets sudo run jamf policy without a
password); without it, run sudo jamf policy on the pilot Mac while reset
waits. If Jamf lost the 1.6.0 package, it comes back from JFrog's releases:
the baseline release must have run once (/release store-assistant ORD-50
standard, with 1.6.0 staged).
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import desktop  # noqa: E402  (puts scripts/ first on the path)
import host  # noqa: E402
import reset as shared  # noqa: E402  (scripts/reset.py)
from desktop import fail, service  # noqa: E402

SERVICE, BASELINE, STAGED, ISSUES = "store-assistant", "1.6.0", "1.7.0", ["ORD-71"]


def jamf_back(reset, config, client):
    policy = desktop.jamf_policy(client, config)
    name = desktop.package_name(config, BASELINE)
    pilot = config["jamf"]["pilot_group"]
    record = client.package(name)
    if record is None or not record.get("hashValue"):
        def upload():
            pkg_name, pkg_bytes = desktop.member(desktop.release_package(SERVICE, BASELINE), ".pkg")
            desktop.ensure_jamf_package(client, config, BASELINE, pkg_name, pkg_bytes)
        reset.act(f"upload {name} to Jamf again, from JFrog's releases", upload)
    if [p["name"] for p in policy["packages"]] != [name]:
        reset.act(f"point the policy {policy['name']} at {name}",
                  lambda: client.set_policy_package(policy["id"], client.package(name)["id"]))
    if policy["scope"]["groups"] != [pilot] or policy["scope"]["all"]:
        reset.act(f"scope the policy to {pilot} only (it was {', '.join(policy['scope']['groups']) or 'nobody'})",
                  lambda: client.set_policy_scope(policy["id"], [pilot]))
    reset.act(f"flush the policy's logs, so the Macs in {pilot} run it again", lambda: client.flush_policy_logs(policy["id"]))
    if client.standin:
        app = desktop.app_config(config)
        baseline = [{"name": app["name"], "version": BASELINE, "bundleId": app["bundle_id"],
                     "path": f"/Applications/{app['name']}.app"}]
        for row in client.app_versions(config["jamf"]["all_group"], app["bundle_id"]):
            if not row["name"].startswith("pilot") and row["version"] != BASELINE:
                reset.act(f"put the agentless {row['name']} back on {BASELINE}",
                          lambda row=row: client.add_simulated_device("computer", row["name"], baseline))


def pilot_back(reset, config, client, ssh, wait):
    app = desktop.app_config(config)
    group = config["jamf"]["pilot_group"]
    if reset.dry_run:
        print(f"  would wait for the Macs in {group} to report {BASELINE}")
        return
    if ssh:
        reset.act(f"run sudo jamf policy on {ssh}",
                  lambda: subprocess.run(["ssh", ssh, "sudo", "-n", "/usr/local/bin/jamf", "policy"], check=True))
    else:
        print(f"  run sudo jamf policy on the pilot Mac: reset waits up to {wait}s for it to report {BASELINE}")
    deadline = time.monotonic() + wait
    while True:
        rows = client.app_versions(group, app["bundle_id"])
        behind = [r["name"] for r in rows if r["version"] != BASELINE]
        if rows and not behind:
            print(f"  {', '.join(r['name'] for r in rows)} on {BASELINE}")
            return
        if time.monotonic() > deadline:
            fail(f"{', '.join(behind) or group + ' has no Macs'} not on {BASELINE} yet: it installs it at its next check-in")
        time.sleep(5)


def windows_back(reset, config):
    session = desktop.deploy_session()
    instance = desktop.windows_instance(session, config["windows"]["test_instance"])
    if instance is None:
        print("  the test instance is stopped: the next run installs its own version")
        return
    installed = desktop.windows_installed(session, instance, config["windows"]["product"])
    if installed.get("version") == BASELINE:
        print(f"  already on {BASELINE}")
        return

    def install():
        package = desktop.release_package(SERVICE, BASELINE)
        msi_name, msi_bytes = desktop.member(package, ".msi")
        signature = desktop.windows_record(package).get("signature") or {}
        desktop.windows_install(session, instance, desktop.installer_bucket(session), msi_name, msi_bytes, BASELINE,
                                signature.get("certificate") if signature.get("test_certificate") else None)
    reset.act(f"install {BASELINE} over {installed.get('version') or 'nothing'} on the test instance", install)


def stage_take(reset):
    current = host.main_version(service(SERVICE)["path"])
    if current == STAGED:
        print(f"  main has {STAGED}")
        return
    reset.act(f"stage {SERVICE} {STAGED} on main (main has {current})", lambda: host.stage(SERVICE, STAGED))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pilot-ssh", help="user@host of the pilot Mac, to run its check-in now")
    parser.add_argument("--wait", type=int, default=120, help="seconds to wait for the pilot Mac")
    parser.add_argument("--dry-run", action="store_true", help="print every action, change nothing")
    args = parser.parse_args()

    # The main reset's steps, for this service and its baseline.
    shared.SERVICE, shared.BASELINE, shared.DEMO_ISSUES = SERVICE, BASELINE, ISSUES
    shared.NOTE = "Cancelled by reset.py desktop before the next take."
    started = time.monotonic()
    config = service(SERVICE)
    client = desktop.jamf(config)
    reset = shared.Reset(args.dry_run)
    reset.step(f"Jamf: Store Assistant back on {BASELINE}, scoped to Pilot", lambda: jamf_back(reset, config, client))
    reset.step(f"The pilot Mac on {BASELINE}", lambda: pilot_back(reset, config, client, args.pilot_ssh, args.wait))
    if "windows" in config["desktop"]["tracks"]:
        reset.step(f"The Windows test instance on {BASELINE}", lambda: windows_back(reset, config))
    reset.step("Open store-assistant changes", lambda: shared.cancel_changes(reset))
    reset.step(f"Tags and GitHub releases after {BASELINE}", lambda: shared.github_takes(reset))
    if host.on_host():
        reset.step(f"Release copies after {BASELINE} in JFrog", lambda: shared.jfrog_takes(reset))
    else:
        print(f"Release copies after {BASELINE} in JFrog\n  skipped here: JFrog answers on the tools host")
    reset.step(f"Jira versions after {BASELINE}, and {', '.join(ISSUES)}", lambda: shared.jira_versions(reset))
    reset.step(f"main on {STAGED}", lambda: stage_take(reset))

    took = f"{time.monotonic() - started:.0f}s"
    if reset.failed:
        print(f"Reset incomplete after {took}: {', '.join(reset.failed)}")
        return 1
    print(f"{'Dry run' if args.dry_run else 'Reset'} in {took}: {reset.actions} action(s). "
          f"Ready for /release {SERVICE} {ISSUES[0]}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
