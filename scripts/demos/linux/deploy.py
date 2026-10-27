"""Stage 10, Production: roll inventory out to its four Linux servers with Ansible.

    python scripts/demos/linux/deploy.py inventory production
    python scripts/demos/linux/deploy.py inventory production --back        the servers this run moved, back
    python scripts/demos/linux/deploy.py inventory production --previous    every server to its release before (/rollback)

One server at a time, in the order of services.yaml: inv-cloud-01,
inv-cloud-02, inv-dc-01, inv-dc-02. Each server fetches the exact package the
run published from JFrog Container Registry, checks its SHA-256, unpacks it
into its own release folder, switches the current symlink, restarts Tomcat and
is checked on its own /health. A failed check switches that server back,
restarts Tomcat, checks it again and stops the rollout: the servers after it
are never touched.

The cloud servers run from here (the tools host) through Systems Manager and
fetch over the VPC. The data-center servers run from dc-agent, as the Jenkins
job inventory-dc-wave, and fetch through artifactory-demo.es.nl; dc-agent is
checked first, so a missing agent stops the release before any server.

When the rollout stops, this posts the Slack line ("inv-dc-01 failed its
check. Rolled back to 2.7.3. Rollout stopped."), adds each server's result to
the change, and exits 1; the pipeline's own stop then closes the change and
posts its final line. Every server's steps go to Splunk and Grafana either way.

--package-file deploys a local zip instead of the published one, for tests
from the Mac before JCR has the inventory repositories. Its SHA-256 must
match the run state's, so it is still the build that was recorded.
"""

import argparse
import collections
import hashlib
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import fleet  # noqa: E402
import report  # noqa: E402
import run_state  # noqa: E402
from common import fail, get_parameter, service, ssm_client  # noqa: E402

PUBLIC_JCR = "https://artifactory-demo.es.nl/artifactory"


def package_for(site, state, cloud_jcr, package_file=None):
    if package_file:
        return {"source": "file", "file": str(package_file), "sha256": state["package"]["sha256"]}
    base = cloud_jcr if site == "cloud" else PUBLIC_JCR
    return {"source": "jcr", "url": f"{base}/{state['package']['path']}", "sha256": state["package"]["sha256"]}


def dc_secrets():
    """For runs from the Mac: what Jenkins credentials give dc-agent in the pipeline."""
    if fleet.dc_mode() != "multipass":
        return None
    ssm = ssm_client()
    secrets = {"INVENTORY_APP_SECRET": get_parameter(ssm, "inventory/app-secret")}
    for env, name in [("JCR_TOKEN", "jcr/inventory-read-token"), ("CF_ACCESS_CLIENT_ID", "cloudflare/dc-agent-client-id"),
                      ("CF_ACCESS_CLIENT_SECRET", "cloudflare/dc-agent-client-secret")]:
        try:
            secrets[env] = ssm.get_parameter(Name=f"/release-demo/{name}", WithDecryption=True)["Parameter"]["Value"]
        except ssm.exceptions.ParameterNotFound:
            secrets[env] = ""
    return secrets


def preflight(names):
    """Every server reachable before the first one changes."""
    session, _ = fleet.deploy_session()
    found = fleet.instances(session)
    for server in fleet.servers():
        if server["name"] in names and server["site"] == "cloud":
            if found.get(server["name"], {}).get("state") != "running":
                fail(f"{server['name']} isn't running: scripts/demo.sh up linux. Nothing was deployed.")
    if any(s["site"] == "data-center" for s in fleet.servers() if s["name"] in names) and fleet.dc_mode() == "jenkins":
        import jenkins_job

        jenkins_job.preflight()
    return session


def rollout(extra_vars, names, secrets, package_for_site=None):
    """The cloud, then the data center, stopping at the first failed server. Returns results."""
    results = []
    for site, members in fleet.by_site(names).items():
        here = dict(extra_vars)
        if package_for_site:
            here["package"] = package_for_site(site)
        site_names = [s["name"] for s in members]
        if site == "cloud":
            code, found = fleet.run_cloud("release.yml", here, site_names)
        else:
            code, found = fleet.run_dc("release.yml", here, site_names, secrets=secrets)
        results += found
        if code != 0:
            break
    return results


def deploy(args, state, deploys, reporting):
    version = state["version"]
    names = [s["name"] for s in fleet.servers()]
    session = preflight(names)
    cloud_jcr = f"http://{fleet.tools_host_private_ip(session)}:8082/artifactory"
    if args.package_file:
        actual = hashlib.sha256(args.package_file.read_bytes()).hexdigest()
        if actual != state["package"]["sha256"]:
            fail(f"{args.package_file} has SHA-256 {actual}, the run recorded {state['package']['sha256']}.")
    print(f"Rolling out inventory {version}, one server at a time: {', '.join(names)}")
    print(f"Package {state['package']['path']}, SHA-256 {state['package']['sha256']}")

    if reporting:
        report.affected_cis(state, names)
        report.note(state, f"Production: rolling out inventory {version} to {', '.join(names)}, one at a time. "
                           "Each server is checked on its own /health; a failed check rolls that server back "
                           "and stops the rollout.")
    # Production is being touched from here on, whatever happens next.
    deploys["production"] = {"number": version, "previous_number": None, "previous_version": None, "url": "",
                             "deployed_at": run_state.now()}
    run_state.save(state)

    results = rollout({"action": "deploy", "version": version}, names, dc_secrets(),
                      lambda site: package_for(site, state, cloud_jcr, args.package_file))
    table = report.rows(fleet.servers(), results, version)
    previous = collections.Counter(r["previous"] for r in table if r.get("previous")).most_common(1)
    previous = previous[0][0] if previous else None
    deploys["production"].update({"previous_number": previous, "previous_version": previous, "servers": table})
    run_state.save(state)

    print()
    print(report.summary(table, version))
    if reporting:
        report.events(state, table)
        report.note(state, report.summary(table, version))
    if report.stopped_at(table):
        deploys["production"]["stopped_at"] = report.stopped_at(table)["server"]
        run_state.save(state)
        # How the pipeline's stop reads in Slack, after the line for the server.
        state["finish_line"] = report.finish_line(table)
        run_state.save(state)
        print(report.slack_line(table))
        if reporting:
            report.post_line(report.slack_line(table))
        sys.exit(1)


def back(args, state, deploys, reporting):
    """--back: the servers this run moved to the new version go back to where it found them.
    --previous: every server goes to the release before its current one."""
    production = deploys.setdefault("production", {})
    if args.back:
        moved = [r for r in production.get("servers", []) if r["result"] == "ok" and r.get("previous")]
        if not moved:
            print("No server moved in this run: nothing to roll back.")
            production["rolled_back_to"] = production.get("previous_version")
            run_state.save(state)
            return
        names = [r["server"] for r in moved]
        extra = {"action": "back", "previous": {r["server"]: r["previous"] for r in moved}}
    else:
        names = [s["name"] for s in fleet.servers()]
        extra = {"action": "previous"}
    preflight(names)
    # A rollback tries every server, and keeps going past one that fails.
    extra.update({"rollout_max_fail": 100})
    results = []
    for site, members in fleet.by_site(names).items():
        site_names = [s["name"] for s in members]
        runner = fleet.run_cloud if site == "cloud" else (
            lambda p, e, n: fleet.run_dc(p, e, n, secrets=dc_secrets()))
        results += runner("release.yml", extra, site_names)[1]
    table = [row for row in report.rows(fleet.servers(), results, None) if row["server"] in names]
    went_to = collections.Counter(r["running"] for r in table if r["result"] == "ok").most_common(1)
    production["rolled_back_to"] = went_to[0][0] if went_to else None
    if args.previous:
        before = collections.Counter(r["previous"] for r in table if r.get("previous")).most_common(1)
        production["previous_version"] = production["previous_number"] = before[0][0] if before else None
    production["rolled_back_servers"] = table
    run_state.save(state)
    for row in table:
        print(f"{'ok  ' if row['result'] == 'ok' else 'FAIL'} {row['server']}: {row['running'] or 'unknown'}")
    if reporting:
        report.events(state, table)
    if any(row["result"] != "ok" for row in table):
        fail("Not every server is back. Look now.")
    print(f"Back on {production['rolled_back_to']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("service")
    parser.add_argument("environment")
    how = parser.add_mutually_exclusive_group()
    how.add_argument("--back", action="store_true", help="the servers this run moved, back to where it found them")
    how.add_argument("--previous", action="store_true", help="every server to its release before the current one")
    parser.add_argument("--package-file", type=Path, help="tests only: a local zip with the run's SHA-256")
    parser.add_argument("--report", action="store_true", help="tell Slack, ServiceNow, Jira and Splunk outside Jenkins too")
    args = parser.parse_args()

    if args.service != fleet.SERVICE:
        fail(f"This deploy is for {fleet.SERVICE}, not {args.service}")
    if args.environment not in service(args.service)["environments"]:
        fail(f"{args.service} has no {args.environment} environment: its four servers are all production. "
             "The pipeline skips staging for it.")
    state = run_state.load()
    deploys = state.setdefault("deploys", {})
    reporting = args.report or bool(os.environ.get("BUILD_URL"))
    if args.back or args.previous:
        back(args, state, deploys, reporting)
    else:
        deploy(args, state, deploys, reporting)


if __name__ == "__main__":
    main()
