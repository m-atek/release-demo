"""orders-service's database for demo 20, on Neon Free, through Neon's API.

    python scripts/demos/rollback-trap/neon.py setup      once, from the Mac, after secrets.py set neon/api-key
    python scripts/demos/rollback-trap/neon.py restore    staging and production back to baseline (reset.py)
    python scripts/demos/rollback-trap/neon.py status     the branches, their computes and the usage; no secrets

setup makes the project release-demo-orders in AWS Europe (London), the Neon
region closest to the functions in eu-west-1, on Postgres 17, and is safe to
run again. Its root branch is baseline: 1.6.0's migrations, 0001_orders.sql
and 0002_seed_orders.sql, run there once, as the deploy runs migrations, and
nothing writes to it after. production (the default) and staging are its
children, so both start from the same orders. Two roles: orders_owner, which
Neon makes with the project and which runs the migrations, and orders_app,
made with SQL so it holds only what the migrations grant it (SELECT on their
tables); the functions use it. The four connection strings go to SSM as
SecureString and are never printed: orders-service/<environment>/database-url
(pooled, orders_app) and orders-service/<environment>/migrate-url (direct,
orders_owner). neon/project holds the project's and branches' ids.

restore puts staging and production back to baseline's data with Neon's
branch restore. Each branch keeps its compute, so the connection strings
stay. A branch with children couldn't be restored without moving them, which
is why baseline is the parent and not a child of production.

The API key comes from SSM (neon/api-key) and is never printed.
"""

import argparse
import json
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import database
import trap
from common import ROOT, fail, get_parameter, put_parameter, ssm_client

API = "https://console.neon.tech/api/v2"
PROJECT = "release-demo-orders"
REGION = "aws-eu-west-2"  # Europe (London)
PG_VERSION = 17
DATABASE = "orders"
BRANCHES = ["baseline", "production", "staging"]
ENVIRONMENTS = ["staging", "production"]
BASELINE_STATE = ROOT / "demo-states" / trap.SERVICE / "1.6.0"
USER_AGENT = "release-demo/1.0 (+https://github.com/m-atek/release-demo)"


class Neon:
    def __init__(self, key):
        self.key = key

    def call(self, method, path, body=None, **query):
        url = API + path + ("?" + urllib.parse.urlencode(query) if query else "")
        request = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                         headers={"Authorization": f"Bearer {self.key}", "Accept": "application/json",
                                                  "Content-Type": "application/json", "User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace")[:300]
            fail(f"Neon {method} {path}: {error.code} {detail}")

    def wait(self, project_id, operations, timeout=300):
        """Until every operation has finished; fails on one that failed."""
        deadline = time.monotonic() + timeout
        pending = [op["id"] for op in operations or []]
        while pending:
            if time.monotonic() > deadline:
                fail(f"Neon's operations didn't finish in {timeout}s: {', '.join(pending)}")
            time.sleep(2)
            still = []
            for op_id in pending:
                status = self.call("GET", f"/projects/{project_id}/operations/{op_id}")["operation"]["status"]
                if status in ("failed", "error"):
                    fail(f"Neon operation {op_id} {status}.")
                if status not in ("finished", "skipped", "cancelled"):
                    still.append(op_id)
            pending = still

    def organization(self):
        """The organization the project lives in. A personal API key must name it
        to list or make projects: Neon answers 400 "org_id is required" otherwise."""
        found = self.call("GET", "/users/me/organizations").get("organizations", [])
        if len(found) != 1:
            names = ", ".join(f"{org.get('name')} ({org['id']})" for org in found) or "none"
            fail(f"Neon's API key reaches {len(found)} organizations ({names}); setup needs exactly one.")
        return found[0]["id"]

    def project(self, org_id):
        for project in self.call("GET", "/projects", search=PROJECT, org_id=org_id).get("projects", []):
            if project["name"] == PROJECT:
                return project
        return None

    def branches(self, project_id):
        return {branch["name"]: branch for branch in self.call("GET", f"/projects/{project_id}/branches")["branches"]}

    def uri(self, project_id, branch_id, role, pooled):
        return self.call("GET", f"/projects/{project_id}/connection_uri", branch_id=branch_id, database_name=DATABASE,
                         role_name=role, pooled=str(pooled).lower())["uri"]


def with_user(uri, user, password):
    """The same connection string as another role."""
    parts = urllib.parse.urlsplit(uri)
    netloc = f"{urllib.parse.quote(user)}:{urllib.parse.quote(password, safe='')}@{parts.hostname}"
    if parts.port:
        netloc += f":{parts.port}"
    return urllib.parse.urlunsplit(parts._replace(netloc=netloc))


def neon():
    return Neon(get_parameter(ssm_client(), "neon/api-key"))


def driver():
    """pg8000, installed from 1.6.0's requirements.txt into a folder of its own, as the build vendors it."""
    folder = Path(tempfile.mkdtemp(prefix="release-demo-pg8000-"))
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "--target",
                    str(folder), "-r", str(BASELINE_STATE / "requirements.txt")], check=True)
    return database.driver(folder)


def app_role(connection, password):
    """orders_app, made with SQL so it gets only what the migrations grant. The
    password comes from secrets.token_urlsafe, so it holds no quote."""
    exists = connection.run("SELECT 1 FROM pg_roles WHERE rolname = :name", name=trap.APP)
    verb = "ALTER" if exists else "CREATE"
    connection.run(f"{verb} ROLE {trap.APP} WITH LOGIN PASSWORD '{password}'")


def setup():
    api = neon()
    org_id = api.organization()
    project = api.project(org_id)
    if project is None:
        print(f"Making the Neon project {PROJECT}: {REGION}, Postgres {PG_VERSION}, root branch baseline")
        made = api.call("POST", "/projects", {"project": {
            "name": PROJECT, "region_id": REGION, "pg_version": PG_VERSION, "org_id": org_id,
            "branch": {"name": "baseline", "role_name": trap.OWNER, "database_name": DATABASE}}})
        project = made["project"]
        api.wait(project["id"], made.get("operations"))
    else:
        print(f"Neon project {PROJECT}: there already ({project['region_id']}, Postgres {project['pg_version']})")
    project_id = project["id"]
    branches = api.branches(project_id)
    if "baseline" not in branches:
        fail(f"{PROJECT} has no branch baseline. It was made by hand: delete it in Neon's console and run setup again.")
    native = driver()
    password = secrets.token_urlsafe(24)

    baseline_url = api.uri(project_id, branches["baseline"]["id"], trap.OWNER, pooled=False)
    connection = database.connect(native, baseline_url)
    app_role(connection, password)
    files = trap.folder_migrations(BASELINE_STATE)
    database.migrate(database.Database(connection), files, "setup", where="baseline")
    connection.close()

    for name in ("production", "staging"):
        if name in branches:
            print(f"Branch {name}: there already")
            continue
        made = api.call("POST", f"/projects/{project_id}/branches", {
            "branch": {"name": name, "parent_id": branches["baseline"]["id"]}, "endpoints": [{"type": "read_write"}]})
        api.wait(project_id, made.get("operations"))
        print(f"Branch {name}: made from baseline, with a compute of its own")
    branches = api.branches(project_id)
    if not branches["production"].get("default"):
        api.wait(project_id, api.call("POST", f"/projects/{project_id}/branches/{branches['production']['id']}/set_as_default")
                 .get("operations"))
        print("Branch production: now the default")

    ssm = ssm_client()
    for name in ENVIRONMENTS:
        branch_id = branches[name]["id"]
        owner = api.uri(project_id, branch_id, trap.OWNER, pooled=False)
        connection = database.connect(native, owner)
        app_role(connection, password)  # a child copied baseline's roles; this keeps the password the same everywhere
        connection.close()
        pooled = with_user(api.uri(project_id, branch_id, trap.OWNER, pooled=True), trap.APP, password)
        put_parameter(ssm, f"{trap.SERVICE}/{name}/database-url", pooled)
        put_parameter(ssm, f"{trap.SERVICE}/{name}/migrate-url", owner)
        print(f"SSM: {trap.SERVICE}/{name}/database-url (pooled, {trap.APP}) and migrate-url (direct, {trap.OWNER})")
    put_parameter(ssm, "neon/project", json.dumps({"project_id": project_id, "branches": {
        name: branches[name]["id"] for name in BRANCHES}}), secure=False)
    print(f"SSM: neon/project. Done: {PROJECT} has baseline (1.6.0's schema and orders), production and staging.")


def project_ids():
    try:
        return json.loads(get_parameter(ssm_client(), "neon/project"))
    except (SystemExit, ValueError):
        fail("No Neon project in SSM yet (neon/project): run scripts/demos/rollback-trap/neon.py setup.")


def restore(names=ENVIRONMENTS, dry_run=False, out=print):
    """Each branch back to baseline's data, keeping its compute and its connection strings."""
    ids = project_ids()
    api = neon()
    for name in names:
        if dry_run:
            out(f"  would restore Neon branch {name} from baseline")
            continue
        reply = api.call("POST", f"/projects/{ids['project_id']}/branches/{ids['branches'][name]}/restore",
                         {"source_branch_id": ids["branches"]["baseline"]})
        api.wait(ids["project_id"], reply.get("operations"))
        out(f"  Neon branch {name}: restored from baseline")


def status():
    ids = project_ids()
    api = neon()
    project = api.call("GET", f"/projects/{ids['project_id']}")["project"]
    print(f"{project['name']}: {project['region_id']}, Postgres {project['pg_version']}")
    for name, branch in api.branches(ids["project_id"]).items():
        print(f"  branch {name}: {branch.get('current_state')}{', default' if branch.get('default') else ''}")
    for endpoint in api.call("GET", f"/projects/{ids['project_id']}/endpoints")["endpoints"]:
        branch = next((n for n, i in ids["branches"].items() if i == endpoint["branch_id"]), endpoint["branch_id"])
        print(f"  compute of {branch}: {endpoint.get('current_state')}, "
              f"{endpoint.get('autoscaling_limit_min_cu')} to {endpoint.get('autoscaling_limit_max_cu')} CU")
    used = {key: project.get(key) for key in ("compute_time_seconds", "active_time_seconds", "synthetic_storage_size")}
    print("  usage this period: " + ", ".join(f"{key} {value}" for key, value in used.items() if value is not None))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["setup", "restore", "status"])
    args = parser.parse_args()
    {"setup": setup, "restore": restore, "status": status}[args.command]()
    return 0


if __name__ == "__main__":
    sys.exit(main())
