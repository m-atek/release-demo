"""Stages 7 and 11, Check: is each cloud running what it should, and working?

    python scripts/demos/clouds/check.py quote-service production --version 1.2.0 --url <ignored>

One check per cloud, in the production order from services.yaml. /health
must report the expected version and the right cloud within the environment's
timeout, then every check path must answer 200.

Which version each cloud should run comes from the deploy in the run state:
the new version where it was released, the version it went back to where it
was rolled back, and the version it kept where production stopped before
reaching it. Without a deploy in the run state, every cloud should run
--version, and --url names their addresses, comma-separated, in that order.
Exits 1 if any cloud fails. The result per cloud goes into the run state.
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[2]))  # scripts/, for the shared helpers, after this folder

import run_state  # noqa: E402
from common import http_get, service  # noqa: E402


def cloud_names(config):
    return {c["key"]: c["name"] for c in config["clouds"]}


def wait_for_version(base_url, version, cloud, timeout):
    """(True, seconds) once /health reports the version from the right cloud, or (False, what it said)."""
    started = time.monotonic()
    seen = None
    while True:
        status, body = http_get(f"{base_url}/health", timeout=10)
        if status == 200:
            try:
                health = json.loads(body)
                seen = f"{health.get('version')} from {health.get('cloud')}"
                if health.get("version") == version and health.get("cloud") == cloud:
                    return True, time.monotonic() - started
            except ValueError:
                seen = "something that isn't JSON"
        elif status:
            seen = f"status {status}"
        if time.monotonic() - started >= timeout:
            return False, seen
        time.sleep(2)


def reason(body):
    """The error a failed path gives, if it says one."""
    try:
        return json.loads(body).get("error", "")[:120]
    except (ValueError, AttributeError):
        return ""


def check_cloud(base_url, cloud, cloud_name, environment, version, paths, timeout, log=print):
    """One cloud's check. Prints one line per check and returns True when all pass."""
    where = f"{cloud_name} {environment}"
    live, detail = wait_for_version(base_url, version, cloud, timeout)
    if not live:
        log(f"FAIL {where}: /health reports {detail or 'nothing'} after {timeout}s, expected {version}")
        return False
    log(f"ok   {where}: /health reports {version} after {detail:.0f}s")
    passed = True
    for path in paths:
        status, body = http_get(base_url + path, timeout=15)
        why = reason(body) if status != 200 else ""
        log(f"{'ok  ' if status == 200 else 'FAIL'} {where}: {path} answered {status or 'nothing'}"
            + (f" ({why})" if why else ""))
        passed &= status == 200
    return passed


def plan(config, environment, state, version, urls):
    """(cloud, url, expected version, note) for each cloud, in order."""
    deployed = state.get("deploys", {}).get(environment, {}).get("clouds", {})
    order = [c["key"] for c in config["clouds"] if not deployed or c["key"] in deployed]
    listed = [u.strip().rstrip("/") for u in (urls or "").split(",") if u.strip()]
    steps = []
    for index, cloud in enumerate(order):
        info = deployed.get(cloud)
        if info:
            note = {"not released": "not released in this run", "rolled back": "rolled back"}.get(info.get("status"), "")
            steps.append((cloud, info["url"], info.get("expected") or version, note))
        elif index < len(listed):
            steps.append((cloud, listed[index], version, ""))
    return steps


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("service")
    parser.add_argument("environment")
    parser.add_argument("--version", required=True)
    parser.add_argument("--url", default="", help="only without a deploy in the run state: the clouds' addresses")
    args = parser.parse_args()

    config = service(args.service)
    env = config["environments"][args.environment]
    names = cloud_names(config)
    state = run_state.load()
    steps = plan(config, args.environment, state, args.version, args.url)
    if not steps:
        print(f"FAIL nothing to check: no {args.environment} deploy in the run state and no --url")
        raise SystemExit(1)

    failed = []
    for cloud, url, expected, note in steps:
        if note:
            print(f"     {names[cloud]} {args.environment}: {note}, should run {expected}")
        if not check_cloud(url, cloud, names[cloud], args.environment, expected, env["check"]["paths"],
                           env["check"]["timeout_seconds"]):
            failed.append(cloud)

    clouds = state.get("deploys", {}).get(args.environment, {}).get("clouds", {})
    for cloud, *_ in steps:
        if cloud in clouds:
            clouds[cloud]["check"] = "failed" if cloud in failed else "ok"
    if clouds:
        run_state.save(state)

    label = args.environment.capitalize()
    if failed:
        print(f"{label} check failed in {', '.join(names[c] for c in failed)}")
        raise SystemExit(1)
    print(f"{label} check passed in {', '.join(names[c] for c, *_ in steps)}")


if __name__ == "__main__":
    main()
