"""What demo 20's scripts share: orders-service's database settings, the deploy
role, the migrations in a package, a migration's statements, squawk's ignore
comments, production's release in JFrog, and the lines a stage adds to the
release's Slack message and ServiceNow change.

The scripts here run from the repository root like the shared ones, which
they import from scripts/. Their own deploy.py shadows the shared one by
name, so shared() loads a shared script under another name.
"""

import datetime
import hashlib
import importlib.util
import io
import os
import re
import subprocess
import sys
import time
import zipfile
from functools import lru_cache
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(1, str(SCRIPTS))  # after this folder

import run_state  # noqa: E402
from common import ROOT, SSM_PREFIX, aws_session, service  # noqa: E402

SERVICE = "orders-service"
MIGRATIONS = "migrations"
OWNER, APP = "orders_owner", "orders_app"
CHECK_PATHS = ["/health", "/", "/api/orders"]


def config():
    return service(SERVICE)


def environment(name):
    return config()["environments"][name]


def shared(name):
    """A shared script from scripts/ as a module, such as shared("deploy")."""
    module_name = f"shared_{name}"
    if module_name not in sys.modules:
        spec = importlib.util.spec_from_file_location(module_name, SCRIPTS / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    return sys.modules[module_name]


@lru_cache(maxsize=1)
def deploy_session():
    """A session as release-demo-deploy-orders-service: it may change only the two
    functions and their aliases, invoke staging's versions, and read the migrate-urls."""
    import boto3

    session = aws_session()
    account = session.client("sts").get_caller_identity()["Account"]
    creds = session.client("sts").assume_role(
        RoleArn=f"arn:aws:iam::{account}:role/release-demo-deploy-{SERVICE}",
        RoleSessionName=f"{SERVICE}-{int(time.time())}")["Credentials"]
    return boto3.Session(aws_access_key_id=creds["AccessKeyId"], aws_secret_access_key=creds["SecretAccessKey"],
                         aws_session_token=creds["SessionToken"], region_name=session.region_name)


def lambda_client():
    return deploy_session().client("lambda")


def has_database(environment_name):
    """The environment's functions read a database: its settings name the parameter."""
    return bool((environment(environment_name).get("settings") or {}).get("DATABASE_URL_PARAMETER"))


def migrate_url(environment_name, client=None):
    """orders_owner's direct connection string, read with the deploy role; None while it is unset."""
    client = client or deploy_session().client("ssm")
    name = f"{SSM_PREFIX}{SERVICE}/{environment_name}/migrate-url"
    try:
        value = client.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]
    except client.exceptions.ParameterNotFound:
        return None
    return None if value == "unset" else value


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def package_migrations(package):
    """{file name: content} of the migrations/*.sql in a package zip."""
    with zipfile.ZipFile(io.BytesIO(package)) as archive:
        return {Path(name).name: archive.read(name) for name in sorted(archive.namelist())
                if name.startswith(f"{MIGRATIONS}/") and name.endswith(".sql")}


def folder_migrations(directory):
    folder = Path(directory) / MIGRATIONS
    return {path.name: path.read_bytes() for path in sorted(folder.glob("*.sql"))} if folder.is_dir() else {}


def statements(sql):
    """A migration's statements, comments left out, in order. Quotes, quoted names
    and dollar quoting are kept whole, so a function body is one statement."""
    found, current, i, n = [], [], 0, len(sql)
    while i < n:
        if sql.startswith("--", i):
            end = sql.find("\n", i)
            i = n if end < 0 else end
            continue
        if sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        char = sql[i]
        if char in "'\"":
            end = i + 1
            while end < n and not (sql[end] == char and sql[end + 1:end + 2] != char):
                end += 2 if sql[end] == char else 1
            current.append(sql[i:end + 1])
            i = end + 1
            continue
        tag = re.match(r"\$[A-Za-z_]*\$", sql[i:]) if char == "$" else None
        if tag:
            end = sql.find(tag.group(0), i + len(tag.group(0)))
            end = n if end < 0 else end + len(tag.group(0))
            current.append(sql[i:end])
            i = end
            continue
        if char == ";":
            text = " ".join("".join(current).split())
            if text:
                found.append(text)
            current = []
        else:
            current.append(char)
        i += 1
    text = " ".join("".join(current).split())
    if text:
        found.append(text)
    return found


# What a statement does to the schema the version before reads: adds to it,
# leaves it as it is, or anything else (drops, renames, type changes, deletes).
ADDITIONS = [r"ALTER TABLE (IF EXISTS )?(ONLY )?\S+ ADD COLUMN\b", r"CREATE TABLE\b", r"CREATE (UNIQUE )?INDEX\b",
             r"CREATE (OR REPLACE )?FUNCTION\b", r"CREATE (OR REPLACE )?(CONSTRAINT )?TRIGGER\b", r"INSERT\b",
             r"UPDATE\b"]
NEUTRAL = [r"SET\b", r"RESET\b", r"GRANT\b", r"COMMENT ON\b"]
NOT_AN_ADDITION = re.compile(r"\b(DROP|RENAME|ALTER COLUMN|TYPE)\b")


def effect(statement):
    """add, none or change."""
    text = " ".join(statement.split()).upper()
    if any(re.match(pattern, text) for pattern in NEUTRAL):
        return "none"
    if text.startswith("ALTER TABLE") and NOT_AN_ADDITION.search(text):
        return "change"
    return "add" if any(re.match(pattern, text) for pattern in ADDITIONS) else "change"


def only_add(files):
    """True when every statement of these migrations adds to the schema, or leaves it as it is."""
    return all(effect(statement) != "change" for data in files.values() for statement in statements(data.decode()))


IGNORE = re.compile(r"^\s*--\s*squawk-ignore(?:-file)?\s+([a-z0-9, -]+?)\s*$", re.MULTILINE)


def ignored_rules(sql):
    """The squawk rules a migration's ignore comments switch off, in order."""
    rules = []
    for match in IGNORE.finditer(sql):
        rules += [rule for rule in re.split(r"[,\s]+", match.group(1)) if rule]
    return list(dict.fromkeys(rules))


def release_package(version):
    """Production's release of a version in JFrog Container Registry, or None."""
    publish = shared("publish")
    from common import get_parameter, ssm_client

    path = f"{publish.releases_root(SERVICE)}/{version}/{SERVICE}-{version}.zip"
    status, body = publish.request("GET", path, get_parameter(ssm_client(), "jcr/token"))
    return (body, path) if status == 200 else (None, path)


def live_version(environment_name, client=None):
    """(Lambda version, app version) the alias live points at."""
    client = client or lambda_client()
    function = environment(environment_name)["function"]
    number = client.get_alias(FunctionName=function, Name="live")["FunctionVersion"]
    return number, shared("deploy").app_version(client, function, number)


def clock(seconds=None):
    """HH:MM:SS in Europe/Amsterdam, where the demo runs."""
    moment = datetime.datetime.fromtimestamp(seconds or time.time(), datetime.timezone.utc)
    try:
        from zoneinfo import ZoneInfo

        return moment.astimezone(ZoneInfo("Europe/Amsterdam")).strftime("%H:%M:%S")
    except Exception:
        return moment.strftime("%H:%M:%S UTC")


def workspace():
    """The Jenkins workspace, or the repository when run by hand: where dist/ and .release/ live."""
    return Path(os.environ.get("WORKSPACE", ROOT))


def tell(text):
    """One line for the release's Slack message and a work note on its change.
    Outside a release (a run from the Mac without Slack) it only prints."""
    print(text, flush=True)
    state = run_state.load()
    python = sys.executable or "python3"
    if state.get("slack", {}).get("ts"):
        subprocess.run([python, str(SCRIPTS / "notify.py"), "line", text], cwd=ROOT, check=False)
    if state.get("change"):
        subprocess.run([python, str(SCRIPTS / "change.py"), "note", text], cwd=ROOT, check=False)
