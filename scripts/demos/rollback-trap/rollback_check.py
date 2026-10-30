"""Rollback check (demo 20), after Check staging: would a rollback still work against the new schema?

    python scripts/demos/rollback-trap/rollback_check.py orders-service

The version a rollback returns to is production's live app version. Its
Lambda version in orders-service-staging, published by an earlier staging
deploy of that version, runs with staging's settings, so it reads staging's
database, which this run has just migrated. Where there is none, the check
publishes one from that version's release package in JFrog, checked against
its SHA-256, without moving the alias. It invokes that version directly with
function URL events for /health (which must report the version), / and
/api/orders, and prints one line each, ok or FAIL, as check.py does. A
handler that raises counts as 502, as its function URL would answer.

On a pass, Slack says "Rollback check: 1.6.0 passes against the new schema."
and the change's backout plan says it was verified, and when. On a failure,
"Rollback check failed: 1.6.0 fails against the new schema." and the run
stops before the change: staging stays on the new version, because the old
one can't run against its schema.
"""

import argparse
import base64
import hashlib
import json
import sys
import time

import trap
import run_state
from common import fail, get_parameter, ssm_client


def url_event(path):
    """What a function URL sends Lambda for a GET (payload format 2.0)."""
    return {"version": "2.0", "routeKey": "$default", "rawPath": path, "rawQueryString": "",
            "headers": {"user-agent": "release-demo-rollback-check"},
            "requestContext": {"http": {"method": "GET", "path": path, "protocol": "HTTP/1.1",
                                        "sourceIp": "127.0.0.1", "userAgent": "release-demo-rollback-check"}},
            "isBase64Encoded": False}


def staging_version_of(client, function, app):
    """The newest Lambda version of staging that runs this app version, or None."""
    numbers = []
    for page in client.get_paginator("list_versions_by_function").paginate(FunctionName=function):
        numbers += [v["Version"] for v in page["Versions"] if v["Version"] != "$LATEST" and v.get("Description") == app]
    return max(numbers, key=int) if numbers else None


def publish_from_release(client, function, app):
    """A staging version of app from its release package in JFrog, checked by SHA-256. The alias doesn't move."""
    package, path = trap.release_package(app)
    if package is None:
        fail(f"FAIL staging has no version of {app}, and JFrog Container Registry has no {path} to publish one from.")
    publish = trap.shared("publish")
    stored = publish.stored_sha256(get_parameter(ssm_client(), "jcr/token"), path)
    actual = hashlib.sha256(package).hexdigest()
    if stored != actual:
        fail(f"FAIL {path} has SHA-256 {actual}, but JFrog recorded {stored}: not publishing it.")
    client.update_function_code(FunctionName=function, ZipFile=package)
    client.get_waiter("function_updated_v2").wait(FunctionName=function)
    number = client.publish_version(FunctionName=function, Description=app)["Version"]
    print(f"ok   published {app} as {function} version {number} from {path} (SHA-256 verified), the alias untouched")
    return number


def logged_error(reply):
    """The handler's own "database error" line from the invocation's log tail, if any."""
    tail = base64.b64decode(reply.get("LogResult") or b"").decode(errors="replace")
    lines = [line.strip() for line in tail.splitlines() if "database error" in line]
    return lines[-1] if lines else None


def call(client, function, number, path, app):
    """(ok, line) for one path, as check.py prints it."""
    reply = client.invoke(FunctionName=function, Qualifier=number, LogType="Tail", Payload=json.dumps(url_event(path)))
    payload = json.loads(reply["Payload"].read() or b"{}")
    if reply.get("FunctionError"):
        why = logged_error(reply) or payload.get("errorType", reply["FunctionError"])
        return False, f"FAIL {path} answered 502: {why}"
    status = payload.get("statusCode", 502)
    if path == "/health":
        seen = json.loads(payload.get("body") or "{}").get("version") if status == 200 else None
        if seen != app:
            return False, f"FAIL /health reports {seen or 'nothing'}, expected {app}"
        return True, f"ok   /health reports {app}"
    return status == 200, f"{'ok  ' if status == 200 else 'FAIL'} {path} answered {status}"


def backout_plan(back, at, only_add):
    plan = (f"Automatic: if the production check fails, the pipeline moves the live alias back to {back}, checks it, "
            f"and closes this change as unsuccessful. Verified before this change: {back} answered /health, / and "
            f"/api/orders against this release's schema in staging at {trap.clock(at)}.")
    if only_add:
        plan += " The database isn't rolled back; this release's migrations only add."
    return plan


def new_migrations_only_add(state):
    """Whether this release's new migrations only add, from the migration check, or worked out here."""
    known = state.get("migrations") or {}
    if "only_add" in known:
        return known["only_add"]
    import migration_check

    files, _ = migration_check.this_package(state)
    _, base_version = trap.live_version("production")
    base, _ = trap.release_package(base_version)
    new, _ = migration_check.new_and_changed(files, trap.package_migrations(base) if base else {})
    return trap.only_add(new)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("service")
    args = parser.parse_args()
    if args.service != trap.SERVICE:
        fail(f"The rollback check knows {trap.SERVICE}, not {args.service}.")
    state = run_state.load()
    client = trap.lambda_client()
    _, back = trap.live_version("production", client)
    function = trap.environment("staging")["function"]
    number = staging_version_of(client, function, back) or publish_from_release(client, function, back)
    print(f"Running {back} ({function} version {number}) against staging's database, after this run's migrations:")
    results = [call(client, function, number, path, back) for path in trap.CHECK_PATHS]
    at = time.time()
    for _, line in results:
        print(line)
    if all(ok for ok, _ in results):
        trap.tell(f"Rollback check: {back} passes against the new schema.")
        state = run_state.load()
        state.setdefault("change_extras", {}).setdefault("fields", {})["backout_plan"] = backout_plan(
            back, at, new_migrations_only_add(state))
        state["rollback_check"] = {"version": back, "lambda_version": number, "at": at, "result": "passed"}
        run_state.save(state)
        return 0
    trap.tell(f"Rollback check failed: {back} fails against the new schema.")
    print(f"Staging stays on {state.get('version')}: {back} can't run against its schema, so moving staging back "
          "wouldn't help. Production is untouched.")
    state = run_state.load()
    state["rollback_check"] = {"version": back, "lambda_version": number, "at": at, "result": "failed"}
    run_state.save(state)
    return 1


if __name__ == "__main__":
    sys.exit(main())
