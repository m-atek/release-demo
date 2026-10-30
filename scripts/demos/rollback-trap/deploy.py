"""Staging and Production for orders-service (demo 20): its migrations first, then scripts/deploy.py.

    python scripts/demos/rollback-trap/deploy.py orders-service staging
    python scripts/demos/rollback-trap/deploy.py orders-service production --back
    python scripts/demos/rollback-trap/deploy.py orders-service production --previous

A version with migrations/, deployed to an environment with a database: the
package comes from JFrog Container Registry and is checked against its
SHA-256, and the migrations in it that the environment's schema_migrations
table doesn't list are applied in order, each in a transaction of its own, as
orders_owner, recorded with their file name, SHA-256, build and time. A file
the database ran whose SHA-256 has changed since stops the deploy before
anything is applied. Then scripts/deploy.py runs as it always does.

--back and --previous move the alias only, as scripts/deploy.py does, and
say "The database stays as it is: migrations aren't reversed." --previous
goes back to the newest version before live that reached a release (its
copy in <releases>/<version>/), so a take that was rolled back is never
where /rollback lands. A version without migrations goes straight to
scripts/deploy.py, so the main video's deploy logs don't change by a line.
"""

import argparse
import hashlib
import io
import os
import sys
import tempfile
import zipfile
from pathlib import Path

import database
import trap
import run_state
from common import fail, get_parameter, ssm_client

DATABASE_STAYS = "The database stays as it is: migrations aren't reversed."


def exec_shared():
    """scripts/deploy.py with the same arguments, in this process, so its output is all there is."""
    script = trap.SCRIPTS / "deploy.py"
    os.execv(sys.executable, [sys.executable, str(script), *sys.argv[1:]])


def run_shared():
    import subprocess

    return subprocess.run([sys.executable, str(trap.SCRIPTS / "deploy.py"), *sys.argv[1:]]).returncode


def verified_package(state):
    publish = trap.shared("publish")
    package = publish.download(get_parameter(ssm_client(), "jcr/token"), state["package"]["path"])
    actual = hashlib.sha256(package).hexdigest()
    if actual != state["package"]["sha256"]:
        fail(f"{state['package']['path']} has SHA-256 {actual}, but the build recorded {state['package']['sha256']}. "
             "Not migrating from a package that changed after it was published.")
    return package, actual


def migrate(environment_name, state):
    package, digest = verified_package(state)
    files = trap.package_migrations(package)
    if not files:
        return
    url = trap.migrate_url(environment_name)
    if not url:
        fail(f"{trap.SERVICE} {environment_name}'s database isn't set up: "
             "run scripts/demos/rollback-trap/neon.py setup.")
    print(f"Migrations in the package (SHA-256 verified: {digest}): {', '.join(files)}")
    with tempfile.TemporaryDirectory() as temp:
        zipfile.ZipFile(io.BytesIO(package)).extractall(temp)
        db = database.Database(database.connect(database.driver(Path(temp)), url))
        try:
            database.migrate(db, files, state["build"]["number"], where=f"{trap.SERVICE} {environment_name}'s database")
        except (database.ChangedMigration, database.MigrationFailed) as error:
            fail(str(error))
        finally:
            db.close()


def released(app):
    """Whether this version reached a release: JFrog has its release copy. None if JFrog doesn't answer."""
    publish = trap.shared("publish")
    path = f"{publish.releases_root(trap.SERVICE)}/{app}/{trap.SERVICE}-{app}.zip"
    try:
        return publish.stored_sha256(get_parameter(ssm_client(), "jcr/token"), path) is not None
    except OSError:
        return None


def previous_target(client, function, live, live_app):
    """(number, app) for /rollback: the newest version before live that reached a
    release, and the one scripts/deploy.py would pick (the newest with another app)."""
    shared = trap.shared("deploy")
    versions = []
    for page in client.get_paginator("list_versions_by_function").paginate(FunctionName=function):
        versions += [v["Version"] for v in page["Versions"] if v["Version"] != "$LATEST"]
    usual = None
    for number in sorted((v for v in versions if int(v) < int(live)), key=int, reverse=True):
        app = shared.app_version(client, function, number)
        if app == live_app:
            continue
        usual = usual or (number, app)
        reached = released(app)
        if reached is None:
            return usual, usual  # JFrog doesn't answer: as scripts/deploy.py would
        if reached:
            return (number, app), usual
        print(f"{function}: version {number} ({app}) never reached a release: not going back to it")
    return None, usual


def live_has_migrations(app):
    """Whether the release production runs carries migrations; False when JFrog doesn't answer."""
    try:
        package, _ = trap.release_package(app)
    except OSError:
        return False
    return bool(package and trap.package_migrations(package))


def previous(environment_name):
    shared = trap.shared("deploy")
    client = trap.lambda_client()
    function = trap.environment(environment_name)["function"]
    live = shared.alias_version(client, function)
    live_app = shared.app_version(client, function, live)
    migrations = trap.has_database(environment_name) and live_has_migrations(live_app)
    target, usual = previous_target(client, function, live, live_app)
    if target is None:
        fail(f"{function} has no earlier release to roll back to.")
    if target == usual and not migrations:
        exec_shared()
    number, app = target
    state = run_state.load()
    deploys = state.setdefault("deploys", {})
    deploys[environment_name] = {"previous_number": live, "previous_version": live_app}
    shared.move_alias(client, function, number)
    deploys[environment_name].update({"rolled_back_to": app, "rolled_back_number": number,
                                      "url": client.get_function_url_config(FunctionName=function, Qualifier="live")["FunctionUrl"]})
    run_state.save(state)
    print(f"{function}: live is back on version {number} ({app})")
    if migrations:
        print(DATABASE_STAYS)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("service")
    parser.add_argument("environment")
    back = parser.add_mutually_exclusive_group()
    back.add_argument("--back", action="store_true", help="move live back to where this run found it")
    back.add_argument("--previous", action="store_true", help="move live to the previous release")
    args = parser.parse_args()
    if args.service != trap.SERVICE:
        exec_shared()
    has_migrations = bool(trap.folder_migrations(trap.config()["dir"]))
    if args.previous:
        return previous(args.environment)
    if args.back:
        if not has_migrations:
            exec_shared()
        code = run_shared()
        if code == 0:
            print(DATABASE_STAYS)
        return code
    if not has_migrations or not trap.has_database(args.environment):
        exec_shared()
    migrate(args.environment, run_state.load())
    exec_shared()


if __name__ == "__main__":
    sys.exit(main())
