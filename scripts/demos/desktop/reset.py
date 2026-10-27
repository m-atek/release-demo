"""Put demo 6 back to its starting state, for a retake or a call. Safe to run twice.

    python scripts/demos/desktop/reset.py                                   the starting state
    python scripts/demos/desktop/reset.py --pilot-ssh admin@192.168.64.5    and run the pilot Mac's check-in over SSH
    python scripts/demos/desktop/reset.py --no-stage                        leave main as it is

The starting state: the Jamf policy Store Assistant on the 1.6.0 package and
scoped to Pilot, its logs flushed; the pilot Mac and the other managed Macs
on 1.6.0; the Windows test instance on 1.6.0 if it runs; no open change for
store-assistant; ORD-71 open with its fix versions unreleased; no
store-assistant-v1.7.x tags; and the bugged 1.7.0 staged on main.

The pilot Mac installs 1.6.0 at its next check-in. With --pilot-ssh the
check-in runs at once (pilot_setup.sh lets sudo run jamf policy without a
password); without it, run sudo jamf policy on the pilot Mac while reset
waits. 1.6.0 comes from JCR's releases, so the baseline release must have run
once: /release store-assistant ORD-50 standard with 1.6.0 staged.
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import desktop  # noqa: E402
from desktop import ROOT, service  # noqa: E402

BASELINE, STAGED, JIRA_KEY, LATER = "1.6.0", "1.7.0", "ORD-71", ["1.7.0", "1.7.1"]


class Steps:
    def __init__(self):
        self.failed = []

    def run(self, name, step):
        started = time.monotonic()
        try:
            result = step()
            print(f"ok   {name}{': ' + result if result else ''} ({time.monotonic() - started:.0f}s)", flush=True)
        except (Exception, SystemExit) as error:
            self.failed.append(name)
            print(f"FAIL {name}: {error}", flush=True)


def jamf_back(config, client):
    policy = desktop.jamf_policy(client, config)
    name = desktop.package_name(config, BASELINE)
    record = client.package(name)
    if record is None or not record.get("hashValue"):
        pkg_name, pkg_bytes = desktop.member(desktop.release_package(config["name"], BASELINE), ".pkg")
        record = desktop.ensure_jamf_package(client, config, BASELINE, pkg_name, pkg_bytes)
    client.set_policy_package(policy["id"], record["id"])
    client.set_policy_scope(policy["id"], [config["jamf"]["pilot_group"]])
    client.flush_policy_logs(policy["id"])
    if client.standin:
        app = desktop.app_config(config)
        for member in client.computer_group(config["jamf"]["all_group"])["members"]:
            if not member["name"].startswith("pilot"):
                client.add_simulated_device("computer", member["name"], [
                    {"name": app["name"], "version": BASELINE, "bundleId": app["bundle_id"],
                     "path": f"/Applications/{app['name']}.app"}])
    return f"{name}, scoped to {config['jamf']['pilot_group']}, logs flushed"


def pilot_back(config, client, ssh, wait):
    app = desktop.app_config(config)
    group = config["jamf"]["pilot_group"]
    if ssh:
        subprocess.run(["ssh", ssh, "sudo", "-n", "/usr/local/bin/jamf", "policy"], check=True)
    else:
        print(f"     Run sudo jamf policy on the pilot Mac: reset waits up to {wait}s for it to report {BASELINE}", flush=True)
    deadline = time.monotonic() + wait
    while True:
        rows = client.app_versions(group, app["bundle_id"])
        behind = [r["name"] for r in rows if r["version"] != BASELINE]
        if rows and not behind:
            return ", ".join(f"{r['name']} on {BASELINE}" for r in rows)
        if time.monotonic() > deadline:
            raise RuntimeError(f"{', '.join(behind) or group + ' has no Macs'} not on {BASELINE} yet: "
                               "it installs it at its next check-in")
        time.sleep(5)


def windows_back(config):
    session = desktop.deploy_session()
    instance = desktop.windows_instance(session, config["windows"]["test_instance"])
    if instance is None:
        return "the test instance is stopped: up installs nothing, the next run installs its own version"
    installed = desktop.windows_installed(session, instance, config["windows"]["product"])
    if installed.get("version") == BASELINE:
        return f"already on {BASELINE}"
    package = desktop.release_package(config["name"], BASELINE)
    msi_name, msi_bytes = desktop.member(package, ".msi")
    signature = desktop.windows_record(package).get("signature") or {}
    desktop.windows_install(session, instance, desktop.installer_bucket(session), msi_name, msi_bytes, BASELINE,
                            signature.get("certificate") if signature.get("test_certificate") else None)
    return f"{installed.get('version') or 'nothing'} to {BASELINE}"


def changes_cancelled(config):
    import change

    sn, _ = desktop.servicenow()
    rows = sn.call("GET", "/api/now/table/change_request", sysparm_fields="sys_id,number,type,state",
                   sysparm_query=f"cmdb_ci.name={config['servicenow']['cmdb_item']}^stateNOT IN3,4", sysparm_limit=50)
    for row in rows:
        kind = row["type"] if row["type"] in ("normal", "standard", "emergency") else "normal"
        change.cancel(sn, {"change": {"sys_id": row["sys_id"], "type": kind}}, "Cancelled by reset.py desktop: the demo starts over.")
    return f"{len(rows)} cancelled" if rows else "none open"


def script(*args):
    result = subprocess.run([sys.executable, *args], cwd=ROOT, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip().splitlines()[-1])
    return result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pilot-ssh", help="user@host of the pilot Mac, to run its check-in now")
    parser.add_argument("--wait", type=int, default=120, help="seconds to wait for the pilot Mac")
    parser.add_argument("--no-stage", action="store_true", help="don't stage 1.7.0 on main")
    args = parser.parse_args()

    started = time.monotonic()
    config = service("store-assistant")
    client = desktop.jamf(config)
    steps = Steps()
    steps.run("Jamf policy back on 1.6.0", lambda: jamf_back(config, client))
    steps.run("Pilot Mac on 1.6.0", lambda: pilot_back(config, client, args.pilot_ssh, args.wait))
    if "windows" in config["desktop"]["tracks"]:
        steps.run("Windows test instance on 1.6.0", lambda: windows_back(config))
    steps.run("Open store-assistant changes", lambda: changes_cancelled(config))
    for version in LATER:
        steps.run(f"Jira {JIRA_KEY}, store-assistant {version}",
                  lambda version=version: script("scripts/jira.py", "reopen", JIRA_KEY, f"store-assistant {version}"))
        steps.run(f"Tag store-assistant-v{version}",
                  lambda version=version: script("scripts/tag.py", "--delete", f"store-assistant-v{version}"))
    if not args.no_stage:
        steps.run(f"Stage {STAGED} on main", lambda: script("scripts/stage.py", "store-assistant", STAGED))
    took = time.monotonic() - started
    if steps.failed:
        raise SystemExit(f"Reset incomplete after {took:.0f}s: {', '.join(steps.failed)}")
    print(f"Reset in {took:.0f}s: ready for /release store-assistant {JIRA_KEY}")


if __name__ == "__main__":
    main()
