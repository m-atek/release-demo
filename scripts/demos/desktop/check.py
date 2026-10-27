"""Stages 7 and 11 for store-assistant: is the new version where it should be?

    python scripts/demos/desktop/check.py store-assistant staging --version 1.7.0 --url <policy URL>
    python scripts/demos/desktop/check.py store-assistant production --version 1.7.0 --url <policy URL>

Jamf: the policy installs StoreAssistant-<version>.pkg and is scoped to the
environment's group (Pilot, then All managed Macs), and every Mac in the
environment's report groups reports the version in its inventory. A Mac
updates at its next check-in, every 15 minutes, or at once with
sudo jamf policy, so the check waits up to the environment's timeout. After a
rollback (the version is the one the run rolled back to) the scope isn't
checked: a rollback leaves it as it was.

Windows, in staging: the test instance's registry reports the version, and
the installed program's signature is valid.

In staging, the check closes the pilot change with its result. Either way it
keeps its lines in the run state as check.notes, for the change. Prints one
line per check, ok or FAIL, and exits 1 when one fails.
"""

import argparse
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import desktop  # noqa: E402
from desktop import run_state, service  # noqa: E402


class Checks:
    def __init__(self):
        self.failed = []
        self.lines = []

    def report(self, ok, text):
        print(f"{'ok  ' if ok else 'FAIL'} {text}", flush=True)
        self.lines.append(f"{'ok' if ok else 'FAIL'}: {text}")
        if not ok:
            self.failed.append(text)
        return ok


def wait_for_versions(client, groups, bundle_id, version, timeout, checks, app_name):
    """Every member of the groups reports the version, within the timeout."""
    started = time.monotonic()
    announced = last_progress = 0
    while True:
        rows = {row["name"]: row for group in groups for row in client.app_versions(group, bundle_id)}
        behind = [r for r in rows.values() if r["version"] != version]
        if rows and not behind:
            took = run_state.duration(time.monotonic() - started)
            for row in sorted(rows.values(), key=lambda r: r["name"]):
                checks.report(True, f"{row['name']} reports {app_name} {version} (after {took})")
            return True
        elapsed = time.monotonic() - started
        if not rows or elapsed > timeout:
            if not rows:
                checks.report(False, f"{', '.join(groups)} has no computers")
            for row in behind:
                checks.report(False, f"{row['name']} still reports {row['version'] or 'no ' + app_name} "
                                     f"after {run_state.duration(elapsed)}, expected {version}")
            return False
        if not announced:
            print(f"waiting for {', '.join(r['name'] for r in behind)} to check in and report {version}", flush=True)
            announced = last_progress = elapsed or 0.001
        elif elapsed - last_progress >= 60:
            print(f"  still waiting after {run_state.duration(elapsed)}", flush=True)
            last_progress = elapsed
        time.sleep(5)


def check_windows(config, version, checks):
    product = config["windows"]["product"]
    session = desktop.deploy_session()
    instance = desktop.windows_instance(session, config["windows"]["test_instance"])
    if not checks.report(instance is not None, f"Windows test instance {config['windows']['test_instance']} is running"):
        return None
    installed = desktop.windows_installed(session, instance, product)
    if "error" in installed:
        checks.report(False, f"Windows test instance: {installed['error']}")
        return None
    checks.report(installed.get("version") == version,
                  f"Windows test instance reports {product['name']} {installed.get('version') or 'not installed'} in the registry")
    checks.report(installed.get("signature") == "Valid",
                  f"installed StoreAssistant.exe signed by {installed.get('signer')}: {installed.get('signature')}")
    return installed


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("service")
    parser.add_argument("environment", choices=["staging", "production"])
    parser.add_argument("--version", required=True)
    parser.add_argument("--url", default="", help="the policy's page; the check reads Jamf through its API")
    args = parser.parse_args()

    config = service(args.service)
    env = config["environments"][args.environment]
    state = run_state.load()
    deploy = state.get("deploys", {}).get(args.environment, {})
    rollback = deploy.get("rolled_back_to") == args.version
    app = desktop.app_config(config)
    client = desktop.jamf(config)
    checks = Checks()

    policy = desktop.jamf_policy(client, config)
    wanted = desktop.package_name(config, args.version)
    names = [p["name"] for p in policy["packages"]]
    checks.report(names == [wanted], f"Jamf policy {policy['name']} installs {', '.join(names) or 'nothing'}")
    if not rollback:
        scope = policy["scope"]["groups"]
        checks.report(scope == [env["jamf_scope"]] and not policy["scope"]["all"],
                      f"Jamf policy {policy['name']} is scoped to {', '.join(scope) or 'nobody'} only")
    groups = env["check"]["report_groups"] if not rollback else policy["scope"]["groups"]
    if not checks.failed:
        wait_for_versions(client, groups, app["bundle_id"], args.version, env["check"]["timeout_seconds"],
                          checks, app["name"])
    for group in env["check"].get("show_groups", []):
        rows = client.app_versions(group, app["bundle_id"])
        on = sum(r["version"] == args.version for r in rows)
        print(f"     {group}: {on} of {len(rows)} on {args.version}")

    if args.environment == "staging" and not rollback and "windows" in config["desktop"]["tracks"]:
        installed = check_windows(config, args.version, checks)
        if installed and not checks.failed:
            desktop.notify(f"Windows installer signed by {installed.get('signer')}: signature {installed.get('signature')}. "
                           f"Test instance on {installed.get('version')}.")

    passed = not checks.failed
    # What the check found, for the change: the pilot's result goes into the rollout change.
    state.setdefault("check", {})["notes"] = f"{env.get('label', args.environment)} check: " + "; ".join(checks.lines) + "."
    run_state.save(state)
    if args.environment == "staging" and not rollback:
        summary = "\n".join(checks.lines)
        desktop.close_pilot_change(state, "successful" if passed else "unsuccessful",
                                   f"Pilot check {'passed' if passed else 'failed'}:\n{summary}")
    label = env.get("label", args.environment)
    print(f"{label} check {'passed' if passed else 'failed'}")
    if not passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
