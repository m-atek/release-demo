"""Put demo 4 back where a take starts: production and staging on 2.0.4, and
2.0.4 staged on main. Safe to run twice; it takes well under two minutes.

    python scripts/demos/apis/reset.py                   every step below
    python scripts/demos/apis/reset.py --no-push         commit the staged 2.0.4, but don't push it
    python scripts/demos/apis/reset.py --only aws,warm   some steps only

Steps:
- aws: both aliases live on the newest Lambda version that runs 2.0.4, with
  no canary weight, and no canary alarm left;
- jcr: production's contract in demos-releases/pricing-api/2.0.4/, where the
  contract check reads it (built once from demo-states), and the release
  copies of the later versions removed, so a take can release them again;
- github: the tags pricing-api-v2.1.x and their GitHub releases removed;
- jira: the versions "pricing-api 2.1.x" unreleased and ORD-52 reopened;
- servicenow: any open pricing-api change cancelled;
- stage: 2.0.4 staged on main with stage.py, which pushes;
- warm: both APIs called once, so neither starts cold.

The jcr step needs JFrog Container Registry, which only the tools host
reaches (JCR_URL, by default localhost:8082), so it runs in the reset job
there. Elsewhere it says it was skipped, and so does any step whose system
is asleep or not set up yet. Exits 1 only when the aws or stage step fails.
"""

import argparse
import base64
import hashlib
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pricing
from common import ROOT, get_parameter, http_get, ssm_client

BASELINE = "2.0.4"
JIRA_KEY = "ORD-52"
STEPS = ["aws", "jcr", "github", "jira", "servicenow", "stage", "warm"]
MUST_WORK = {"aws", "stage"}


def numeric(version):
    return tuple(int(part) for part in version.split("."))


def later_versions():
    """Every version in demo-states after the baseline: what a take releases."""
    states = ROOT / "demo-states" / pricing.SERVICE
    return sorted((p.name for p in states.iterdir() if p.is_dir() and numeric(p.name) > numeric(BASELINE)),
                  key=numeric)


def run(*args):
    result = subprocess.run([sys.executable, *map(str, args)], cwd=ROOT, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip().splitlines()[-1])
    return result.stdout.strip()


def baseline_number(client, function):
    """The newest published version that runs the baseline."""
    versions = []
    for page in client.get_paginator("list_versions_by_function").paginate(FunctionName=function):
        versions += [v["Version"] for v in page["Versions"] if v["Version"] != "$LATEST"]
    for number in sorted(versions, key=int, reverse=True):
        if pricing.app_version(client, function, number) == BASELINE:
            return number
    raise RuntimeError(f"{function} has no version that runs {BASELINE}. Apply infra/demos/4-apis first.")


def step_aws(args):
    client = pricing.lambda_client()
    for name in ("staging", "production"):
        function = pricing.environment(name)["function"]
        number = baseline_number(client, function)
        pricing.route(client, function, number)
        print(f"  {function}: live on version {number} ({BASELINE}), no canary weight")
    alarms = pricing.canary_alarms()
    if alarms:
        pricing.cloudwatch_client().delete_alarms(AlarmNames=alarms)
    print(f"  canary alarms: {'deleted ' + ', '.join(alarms) if alarms else 'none left'}")


def baseline_package():
    """The baseline as the build step packages it, with a fixed commit, so it is the same every time."""
    build = pricing.shared("build")
    with tempfile.TemporaryDirectory() as temp:
        source = Path(temp) / "package"
        source.mkdir()
        for path, relative in build.package_files(ROOT / "demo-states" / pricing.SERVICE / BASELINE):
            (source / relative).parent.mkdir(parents=True, exist_ok=True)
            (source / relative).write_bytes(path.read_bytes())
        (source / "build_info.json").write_text(json.dumps(
            {"service": pricing.SERVICE, "version": BASELINE, "commit": "baseline"}, indent=2) + "\n")
        package = Path(temp) / "package.zip"
        build.write_zip(source, package)
        return package.read_bytes()


def step_jcr(args):
    publish = pricing.shared("publish")
    ssm = ssm_client()
    token = get_parameter(ssm, "jcr/token")
    try:
        status, _ = publish.request("GET", "api/system/ping", token)
    except OSError:
        status = 0
    if status != 200:
        raise RuntimeError(f"JFrog Container Registry doesn't answer at {publish.JCR}: run this on the tools host")

    base = f"{publish.releases_root(pricing.SERVICE)}/{BASELINE}"
    name = f"{pricing.SERVICE}-{BASELINE}.zip"
    if publish.stored_sha256(token, f"{base}/{name}"):
        print(f"  {base}/{name}: there")
    else:
        package = baseline_package()
        sha256 = hashlib.sha256(package).hexdigest()
        publish.upload(token, f"{base}/{name}", package)
        info = {"service": pricing.SERVICE, "version": BASELINE, "commit": "baseline", "package": name,
                "sha256": sha256, "size_bytes": len(package), "jira_key": None, "build": "baseline"}
        publish.upload(token, f"{base}/build-info.json", (json.dumps(info, indent=2) + "\n").encode())
        print(f"  {base}/{name}: uploaded, SHA-256 {sha256}")

    admin = "Basic " + base64.b64encode(f"admin:{get_parameter(ssm, 'jcr/admin-password')}".encode()).decode()
    removed = []
    for version in later_versions():
        status, _ = publish.request("DELETE", f"{publish.releases_root(pricing.SERVICE)}/{version}/", token,
                                    headers={"Authorization": admin})
        if status in (200, 202, 204):
            removed.append(version)
        elif status != 404:
            raise RuntimeError(f"JCR didn't remove {publish.releases_root(pricing.SERVICE)}/{version}/ ({status})")
    print(f"  release copies after {BASELINE}: {'removed ' + ', '.join(removed) if removed else 'none'}")


def step_github(args):
    for version in later_versions():
        print("  " + run(pricing.SCRIPTS / "tag.py", "--delete", f"{pricing.SERVICE}-v{version}"))


def step_jira(args):
    for version in later_versions():
        print("  " + run(pricing.SCRIPTS / "jira.py", "reopen", JIRA_KEY, f"{pricing.SERVICE} {version}"))


def step_servicenow(args):
    change = pricing.shared("change")
    sn, _ = change.connect()
    item = pricing.config()["servicenow"]["cmdb_item"]
    open_changes = sn.call("GET", "/api/now/table/change_request", sysparm_fields="sys_id,number,type",
                           sysparm_query=f"cmdb_ci.name={item}^stateNOT IN{change.CLOSED},{change.CANCELED}")
    for record in open_changes:
        sn.call("PATCH", f"/api/sn_chg_rest/change/{record['type']}/{record['sys_id']}",
                {"state": change.CANCELED, "close_notes": "Cancelled by reset.py apis, before a new take."})
        print(f"  {record['number']}: cancelled")
    if not open_changes:
        print(f"  no open {item} change")


def step_stage(args):
    print("  " + run(pricing.SCRIPTS / "stage.py", pricing.SERVICE, BASELINE,
                     *(["--no-push"] if args.no_push else [])).replace("\n", "\n  "))


def step_warm(args):
    for name in ("staging", "production"):
        url = pricing.api_url(name)
        status, body = http_get(f"{url}/health", timeout=10)
        version = json.loads(body).get("version") if status == 200 else None
        print(f"  {name}: /health answered {status or 'nothing'}{', version ' + version if version else ''}")
        if version != BASELINE:
            raise RuntimeError(f"{name} reports {version}, expected {BASELINE}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--no-push", action="store_true", help="commit the staged baseline, but don't push it")
    parser.add_argument("--only", default=",".join(STEPS), help=f"comma-separated, from: {', '.join(STEPS)}")
    args = parser.parse_args()
    chosen = [s for s in STEPS if s in args.only.split(",")]

    started = time.monotonic()
    failed = []
    for name in chosen:
        print(name)
        try:
            globals()[f"step_{name}"](args)
        except (Exception, SystemExit) as error:
            reason = str(error) if not isinstance(error, SystemExit) else "see above"
            print(f"  not done: {reason}")
            if name in MUST_WORK:
                failed.append(name)
    took = time.monotonic() - started
    if failed:
        print(f"Not reset: {', '.join(failed)} failed, after {took:.0f}s")
        raise SystemExit(1)
    print(f"Demo 4 is back on {pricing.SERVICE} {BASELINE}, in {took:.0f}s")


if __name__ == "__main__":
    sys.exit(main())
