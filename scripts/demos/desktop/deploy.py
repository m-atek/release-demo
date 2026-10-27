"""Stages 6 and 10 for store-assistant: the pilot, then every managed Mac.

    python scripts/demos/desktop/deploy.py store-assistant staging              the pilot
    python scripts/demos/desktop/deploy.py store-assistant production           every managed Mac
    python scripts/demos/desktop/deploy.py store-assistant staging --back       back to where this run found it
    python scripts/demos/desktop/deploy.py store-assistant production --previous   the release before (/rollback)

staging is the pilot. It downloads the run's package from JFrog Container
Registry and checks its SHA-256, opens the pre-approved standard change
"Pilot of Store Assistant", uploads the .pkg to Jamf, points the policy at
it, scopes the policy to the pilot group only and flushes its logs, so it
runs again on Macs that ran it before. In the same step it installs the .msi
on the Windows test instance through Run Command.

production runs after the rollout change is approved. It checks the package
again, checks that Jamf holds the very same .pkg (its SHA-512), and widens
the policy's scope to every managed Mac. A rejected change never gets here,
so the scope stays on the pilot group.

--back puts the policy back on the package and scope this run found, and
flushes its logs. --previous, for /rollback, points the policy at the last
release before the one it installs (from <service>-releases in JCR), uploading
it to Jamf again if Jamf lost it, and flushes the logs; the scope stays as it
is. Either way, the Macs in scope install it at their next check-in.

Jamf and the pilot change need their accounts; --no-change skips the change,
for runs from the Mac without ServiceNow.
"""

import argparse
import hashlib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import desktop  # noqa: E402
from desktop import fail, run_state, service  # noqa: E402


def deploy_pilot(config, state, client, args):
    env = config["environments"]["staging"]
    package = desktop.verified_package(state)
    pkg_name, pkg_bytes = desktop.member(package, ".pkg")
    if pkg_name is None:
        fail("The package holds no .pkg.")
    if not args.no_change:
        desktop.open_pilot_change(state, config, env["jamf_scope"])

    policy = desktop.jamf_policy(client, config)
    previous_version, previous_id = desktop.policy_version(client, policy)
    record = desktop.ensure_jamf_package(client, config, state["version"], pkg_name, pkg_bytes)
    client.set_policy_package(policy["id"], record["id"])
    client.set_policy_scope(policy["id"], [env["jamf_scope"]])
    client.flush_policy_logs(policy["id"])
    print(f"Jamf policy {policy['name']}: {pkg_name}, scoped to {env['jamf_scope']} only, logs flushed. "
          f"It was on {previous_version or 'no package'}, scoped to {', '.join(policy['scope']['groups']) or 'nobody'}.")

    deploy = {"number": str(record["id"]), "previous_number": str(previous_id) if previous_id else None,
              "previous_version": previous_version, "previous_scope": policy["scope"]["groups"],
              "scope": [env["jamf_scope"]], "url": desktop.policy_url(client, policy), "deployed_at": run_state.now()}
    if "windows" in config["desktop"]["tracks"]:
        deploy["windows"] = deploy_windows(config, state, package)
    state.setdefault("deploys", {})["staging"] = deploy
    # How Slack's last line reads if the run stops from here, a rejected rollout above all.
    state["finish_line"] = (f"Stopped after {{total}}. {config['jamf']['policy']} {state['version']} stays on the "
                            f"{env['jamf_scope']} group; every other Mac stays on {previous_version or 'what it has'}.")
    run_state.save(state)


def deploy_windows(config, state, package):
    product = config["windows"]["product"]
    session = desktop.deploy_session()
    instance = desktop.windows_instance(session, config["windows"]["test_instance"])
    if instance is None:
        fail(f"The Windows test instance {config['windows']['test_instance']} isn't running: scripts/demo.sh up desktop")
    msi_name, msi_bytes = desktop.member(package, ".msi")
    if msi_name is None:
        fail("The package holds no .msi.")
    signature = desktop.windows_record(package).get("signature") or {}
    before = desktop.windows_installed(session, instance, product)
    key = desktop.windows_install(session, instance, desktop.installer_bucket(session), msi_name, msi_bytes, state["version"],
                                  signature.get("certificate") if signature.get("test_certificate") else None)
    print(f"Windows test instance {instance}: {msi_name} installed, was {before.get('version') or 'not installed'}")
    return {"instance": instance, "previous_version": before.get("version"), "key": key}


def widen(config, state, client):
    env = config["environments"]["production"]
    package = desktop.verified_package(state)
    pkg_name, pkg_bytes = desktop.member(package, ".pkg")
    policy = desktop.jamf_policy(client, config)
    names = [p["name"] for p in policy["packages"]]
    if names != [pkg_name]:
        fail(f"The policy installs {', '.join(names) or 'nothing'}, not {pkg_name}: not widening its scope.")
    record = client.package(pkg_name)
    if record.get("hashType") == "SHA_512" and record["hashValue"].lower() != hashlib.sha512(pkg_bytes).hexdigest():
        fail(f"Jamf's {pkg_name} isn't the package in JCR (SHA-512 differs): not widening its scope.")
    print(f"ok   Jamf's {pkg_name} has the SHA-512 of the package in JCR")
    client.set_policy_scope(policy["id"], [env["jamf_scope"]])
    print(f"Jamf policy {policy['name']}: scope widened from {', '.join(policy['scope']['groups'])} to {env['jamf_scope']}")
    staging = state.get("deploys", {}).get("staging", {})
    state.setdefault("deploys", {})["production"] = {
        "number": str(record["id"]), "previous_number": staging.get("previous_number"),
        "previous_version": staging.get("previous_version"), "previous_scope": policy["scope"]["groups"],
        "scope": [env["jamf_scope"]], "url": desktop.policy_url(client, policy), "deployed_at": run_state.now()}
    state["finish_line"] = f"Stopped after {{total}}, after the scope widened to {env['jamf_scope']}. Look at Jamf now."
    run_state.save(state)


def back(config, state, client, environment):
    """Policy, scope and, for the pilot, the Windows test instance back to where this run found them."""
    deploy = state["deploys"][environment]
    policy = desktop.jamf_policy(client, config)
    if deploy.get("previous_number"):
        client.set_policy_package(policy["id"], deploy["previous_number"])
    if deploy.get("previous_scope") is not None:
        client.set_policy_scope(policy["id"], deploy["previous_scope"])
    client.flush_policy_logs(policy["id"])
    deploy.update({"rolled_back_to": deploy.get("previous_version"), "rolled_back_number": deploy.get("previous_number")})
    print(f"Jamf policy {policy['name']}: back on {deploy.get('previous_version') or 'no package'}, scoped to "
          f"{', '.join(deploy.get('previous_scope') or []) or 'nobody'}, logs flushed")
    windows = deploy.get("windows")
    if environment == "staging" and windows and windows.get("previous_version"):
        session = desktop.deploy_session()
        released = desktop.release_package(config["name"], windows["previous_version"])
        msi_name, msi_bytes = desktop.member(released, ".msi")
        signature = desktop.windows_record(released).get("signature") or {}
        desktop.windows_install(session, windows["instance"], desktop.installer_bucket(session), msi_name, msi_bytes,
                                windows["previous_version"], signature.get("certificate") if signature.get("test_certificate") else None)
        print(f"Windows test instance: back on {windows['previous_version']}")
    if environment == "staging":
        desktop.close_pilot_change(state, "unsuccessful", f"The pilot check failed. The policy is back on "
                                                          f"{deploy.get('previous_version')} and its logs are flushed.")
    run_state.save(state)


def previous(config, state, client):
    """/rollback: the release before the one the policy installs."""
    policy = desktop.jamf_policy(client, config)
    current, current_id = desktop.policy_version(client, policy)
    if current is None:
        fail(f"The policy {policy['name']} installs no package: nothing to roll back.")
    earlier = [v for v in desktop.released_versions(config["name"]) if desktop.version_key(v) < desktop.version_key(current)]
    if not earlier:
        fail(f"{config['name']}-releases has no release before {current} to roll back to.")
    target = earlier[-1]
    name = desktop.package_name(config, target)
    record = client.package(name)
    if record is None or not record.get("hashValue"):
        pkg_name, pkg_bytes = desktop.member(desktop.release_package(config["name"], target), ".pkg")
        record = desktop.ensure_jamf_package(client, config, target, pkg_name, pkg_bytes)
    client.set_policy_package(policy["id"], record["id"])
    client.flush_policy_logs(policy["id"])
    scope = ", ".join(policy["scope"]["groups"]) or "nobody"
    print(f"Jamf policy {policy['name']}: back on {name}, was {current}; still scoped to {scope}; logs flushed")
    state.setdefault("deploys", {})["production"] = {
        "previous_number": str(current_id), "previous_version": current, "rolled_back_to": target,
        "rolled_back_number": str(record["id"]), "scope": policy["scope"]["groups"],
        "url": desktop.policy_url(client, policy), "deployed_at": run_state.now()}
    run_state.save(state)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("service")
    parser.add_argument("environment", choices=["staging", "production"])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--back", action="store_true", help="back to where this run found the policy")
    mode.add_argument("--previous", action="store_true", help="the release before the live one")
    parser.add_argument("--no-change", action="store_true", help="no pilot change, for runs without ServiceNow")
    args = parser.parse_args()

    config = service(args.service)
    state = run_state.load()
    client = desktop.jamf(config)
    if args.back:
        back(config, state, client, args.environment)
    elif args.previous:
        previous(config, state, client)
    elif args.environment == "staging":
        deploy_pilot(config, state, client, args)
    else:
        widen(config, state, client)


if __name__ == "__main__":
    sys.exit(main())
