"""Put demo 4 back where a take starts: production and staging on 2.0.4, and
2.0.4 staged on main. Safe to run twice; it takes well under two minutes.

    python scripts/reset.py apis                         or the Jenkins job reset with DEMO=apis
    python scripts/reset.py apis --dry-run               every action it would take, and nothing else
    python scripts/demos/apis/reset.py --only aws,warm   some steps only

Steps:
- aws: both aliases live on the newest Lambda version that runs 2.0.4, with
  no canary weight, and no canary alarm left;
- jcr: production's contract in demos-releases/pricing-api/2.0.4/, where the
  contract check reads it (built once from demo-states), and the release
  copies of the later versions archived under demos-releases/pricing-api/takes/,
  so a take can release them again;
- github: the tags pricing-api-v2.1.x archived under takes/, with their
  GitHub releases, as the main reset does;
- jira: the versions "pricing-api 2.1.x" unreleased and ORD-52 reopened;
- servicenow: any open pricing-api change cancelled;
- stage: 2.0.4 staged on main, from a fresh clone, pushing as the GitHub App;
- warm: both APIs called once, so neither starts cold.

Like the main reset it keeps the past: earlier changes, comments, events and
releases stay. The jcr step needs JFrog Container Registry, which only the
tools host reaches (JCR_URL, by default localhost:8082), so it runs in the
reset job there. Elsewhere it says it was skipped.
"""

import argparse
import hashlib
import importlib.util
import json
import sys
import tempfile
import time
from pathlib import Path

import pricing
from common import ROOT, get_parameter, http_get, ssm_client

BASELINE = "2.0.4"
JIRA_KEY = "ORD-52"
STEPS = ["aws", "jcr", "github", "jira", "servicenow", "stage", "warm"]


def numeric(version):
    return tuple(int(part) for part in version.split("."))


def later_versions():
    """Every version in demo-states after the baseline: what a take releases."""
    states = ROOT / "demo-states" / pricing.SERVICE
    return sorted((p.name for p in states.iterdir() if p.is_dir() and numeric(p.name) > numeric(BASELINE)),
                  key=numeric)


def main_reset():
    """scripts/reset.py, with its steps pointed at pricing-api and 2.0.4. It
    imports the shared check.py, which this folder's check.py shadows by name."""
    saved = sys.modules.get("check")
    sys.modules["check"] = pricing.shared("check")
    try:
        spec = importlib.util.spec_from_file_location("shared_reset", ROOT / "scripts" / "reset.py")
        shared = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(shared)
    finally:
        if saved is None:
            sys.modules.pop("check", None)
        else:
            sys.modules["check"] = saved
    shared.SERVICE, shared.BASELINE, shared.DEMO_ISSUES = pricing.SERVICE, BASELINE, [JIRA_KEY]
    return shared


def baseline_number(client, function):
    """The newest published version that runs the baseline."""
    versions = []
    for page in client.get_paginator("list_versions_by_function").paginate(FunctionName=function):
        versions += [v["Version"] for v in page["Versions"] if v["Version"] != "$LATEST"]
    for number in sorted(versions, key=int, reverse=True):
        if pricing.app_version(client, function, number) == BASELINE:
            return number
    raise RuntimeError(f"{function} has no version that runs {BASELINE}. Apply infra/demos/4-apis first.")


def step_aws(reset, shared):
    client = pricing.lambda_client()
    for name in ("staging", "production"):
        function = pricing.environment(name)["function"]
        number = baseline_number(client, function)
        main, weights = pricing.routing(client, function)
        if main == number and not weights:
            print(f"  {function}: live on version {number} ({BASELINE}), no canary weight")
            continue
        reset.act(f"point {function}'s live alias at version {number} ({BASELINE}), with no canary weight "
                  f"(now version {main}{', weights ' + str(weights) if weights else ''})",
                  lambda function=function, number=number: pricing.route(client, function, number))
    alarms = pricing.canary_alarms()
    if alarms:
        reset.act(f"delete the leftover canary alarms {', '.join(alarms)}",
                  lambda: pricing.cloudwatch_client().delete_alarms(AlarmNames=alarms))
    else:
        print("  canary alarms: none left")


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


def upload_baseline(publish, token, base, name):
    package = baseline_package()
    sha256 = hashlib.sha256(package).hexdigest()
    publish.upload(token, f"{base}/{name}", package)
    info = {"service": pricing.SERVICE, "version": BASELINE, "commit": "baseline", "package": name,
            "sha256": sha256, "size_bytes": len(package), "jira_key": None, "build": "baseline"}
    publish.upload(token, f"{base}/build-info.json", (json.dumps(info, indent=2) + "\n").encode())


def jfrog_reachable(publish, token):
    try:
        status, _ = publish.request("GET", "api/system/ping", token)
    except OSError:
        status = 0
    return status == 200


def step_jcr(reset, shared):
    publish = pricing.shared("publish")
    token = get_parameter(ssm_client(), "jcr/token")
    if not jfrog_reachable(publish, token):
        print(f"  skipped: JFrog Container Registry doesn't answer at {publish.JCR} from here. "
              "Run the Jenkins job reset with DEMO=apis, on the tools host.")
        return
    base = f"{publish.releases_root(pricing.SERVICE)}/{BASELINE}"
    name = f"{pricing.SERVICE}-{BASELINE}.zip"
    if publish.stored_sha256(token, f"{base}/{name}"):
        print(f"  {base}/{name}: there")
    else:
        reset.act(f"upload production's {BASELINE} contract to {base}/{name}",
                  lambda: upload_baseline(publish, token, base, name))
    shared.jfrog_takes(reset)


def step_github(reset, shared):
    shared.github_takes(reset)


def step_jira(reset, shared):
    shared.jira_versions(reset)


def step_servicenow(reset, shared):
    shared.cancel_changes(reset)


def step_stage(reset, shared):
    shared.stage_baseline(reset)


def step_warm(reset, shared):
    for name in ("staging", "production"):
        url = pricing.api_url(name)
        if reset.dry_run:
            print(f"  would call {name}'s /health once")
            continue
        status, body = http_get(f"{url}/health", timeout=10)
        version = json.loads(body).get("version") if status == 200 else None
        print(f"  {name}: /health answered {status or 'nothing'}{', version ' + version if version else ''}")
        if version != BASELINE:
            raise RuntimeError(f"{name} reports {version}, expected {BASELINE}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="print every action, change nothing")
    parser.add_argument("--only", default=",".join(STEPS), help=f"comma-separated, from: {', '.join(STEPS)}")
    args = parser.parse_args()
    chosen = [s for s in STEPS if s in args.only.split(",")]

    started = time.monotonic()
    shared = main_reset()
    reset = shared.Reset(args.dry_run)
    print(f"Reset demo 4 to {pricing.SERVICE} {BASELINE}, keeping the past"
          f"{' (dry run: nothing changes)' if args.dry_run else ''}.")
    for name in chosen:
        reset.step(name, lambda name=name: globals()[f"step_{name}"](reset, shared))
    took = time.monotonic() - started
    if reset.failed:
        print(f"Not everything reset after {took:.0f}s: {', '.join(reset.failed)}. The messages above say why.")
        return 1
    if args.dry_run:
        print(f"Dry run in {took:.0f}s: {reset.actions} action(s) to take.")
    else:
        print(f"Demo 4 is back on {pricing.SERVICE} {BASELINE}, in {took:.0f}s: {reset.actions} action(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
