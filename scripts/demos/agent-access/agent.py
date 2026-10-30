"""Video 9's coding agent: Claude Code in its container, on its own AWS role.

    python scripts/demos/agent-access/agent.py run          agent.sh: a fresh workspace and session, then Claude Code
    python scripts/demos/agent-access/agent.py check        check.sh: acceptance checks 1 to 5, no model call
    python scripts/demos/agent-access/agent.py check --expired   check 8, in the container check started an hour earlier
    python scripts/demos/agent-access/agent.py build        build the image (scripts/demo.sh up agent-access)
    python scripts/demos/agent-access/agent.py status       the role, the image's Claude Code, production's live alias
    python scripts/demos/agent-access/agent.py trail        CloudTrail's record of the session coding-agent's refused calls

Runs on the Mac with your Identity Center session. Each run makes a fresh
workspace in a temporary folder: orders-service 1.4.0 from demo-states at
/work/orders-service and its package, as make build packages it, at
/work/dist/. Nothing else from the repository goes in. It assumes
release-demo-coding-agent with the session name coding-agent for one hour,
reads the Claude credential from SSM, and starts the container with only
the three AWS session variables, AWS_REGION and that credential. Nothing
from ~/.aws, ~/.ssh or the keychain reaches it. It prints one line, the
role and when its session ends, and never a credential.

Before Claude Code starts, the workspace's take.json records where
orders-service-production's live alias pointed, so scripts/reset.py
agent-access can check that it still does.
"""

import argparse
import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(1, str(SCRIPTS))

from common import ROOT, aws_session, fail, get_parameter, service, ssm_client  # noqa: E402

ROLE = "release-demo-coding-agent"
SESSION = "coding-agent"
SESSION_SECONDS = 3600
VERSION = "1.4.0"
SERVICE = "orders-service"
REGION = "eu-west-1"
CREDENTIAL = "anthropic/api-key"
DOCKERFILE = ROOT / "jenkins" / "toolchain" / "coding-agent.Dockerfile"
CONTEXT = ROOT / "jenkins" / "toolchain" / "coding-agent"
WORKSPACE_PREFIX = "release-demo-agent-"
EXPIRY_CONTAINER = "release-demo-agent-expiry"
AMSTERDAM = ZoneInfo("Europe/Amsterdam")
# Exactly what reaches the container's environment from outside.
PASSED = ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_REGION", "ANTHROPIC_API_KEY"]
# What the container's shell and Docker add by themselves.
RUNTIME = ["HOME", "HOSTNAME", "PATH", "PWD", "SHLVL", "TERM", "_", "OLDPWD", "LC_CTYPE"]


def image_tag():
    """release-demo/coding-agent:<hash>, from the Dockerfile and its context, as toolchain.image() tags."""
    digest = hashlib.sha256(DOCKERFILE.read_bytes())
    for path in sorted(p for p in CONTEXT.rglob("*") if p.is_file()):
        digest.update(path.relative_to(CONTEXT).as_posix().encode() + b"\0" + path.read_bytes())
    return f"release-demo/coding-agent:{digest.hexdigest()[:12]}"


def docker(*args, check=True, capture=True):
    result = subprocess.run(["docker", *args], capture_output=capture, text=True)
    if check and result.returncode != 0:
        fail(f"docker {args[0]} failed: {(result.stderr or '').strip()[:300]}")
    return result


def image_exists(tag):
    return docker("image", "inspect", tag, check=False).returncode == 0


def build():
    tag = image_tag()
    if image_exists(tag):
        print(f"{tag}: built already")
        return tag
    print(f"Building {tag} (Claude Code, AWS CLI v2, Python 3.13) ...")
    result = subprocess.run(["docker", "build", "-t", tag, "-f", str(DOCKERFILE), str(CONTEXT)])
    if result.returncode != 0:
        fail("The image didn't build. The lines above say why.")
    return tag


def required_image():
    tag = image_tag()
    if not image_exists(tag):
        fail(f"No image {tag} yet. Build it first: scripts/demo.sh up agent-access")
    return tag


def image_env_names(tag):
    """The ENV names the image itself sets, such as PATH and AWS_PAGER."""
    reply = json.loads(docker("image", "inspect", tag, "--format", "{{json .Config.Env}}").stdout)
    return sorted({entry.split("=", 1)[0] for entry in reply or []})


# The workspace.

def package(source, dist):
    """orders-service's package exactly as make build makes it: scripts/build.py's file list and zip."""
    import build as shared_build

    staged = dist.parent / ".package"
    staged.mkdir()
    for path, relative in shared_build.package_files(source):
        (staged / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, staged / relative)
    (staged / "build_info.json").write_text(json.dumps(
        {"service": SERVICE, "version": VERSION, "commit": "demo-states/orders-service/1.4.0"}, indent=2) + "\n")
    name = f"{SERVICE}-{VERSION}.zip"
    shared_build.write_zip(staged, dist / name)
    shutil.rmtree(staged)
    sha256 = shared_build.sha256(dist / name)
    (dist / "build-info.json").write_text(json.dumps(
        {"service": SERVICE, "version": VERSION, "package": name, "sha256": sha256,
         "size_bytes": (dist / name).stat().st_size}, indent=2) + "\n")
    return name, sha256


def workspace(base=None):
    """A fresh temporary folder: orders-service/ at 1.4.0 and dist/ with its package. Nothing else."""
    folder = Path(tempfile.mkdtemp(prefix=WORKSPACE_PREFIX, dir=base))
    source = ROOT / "demo-states" / SERVICE / VERSION
    shutil.copytree(source, folder / SERVICE, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    (folder / "dist").mkdir()
    package(folder / SERVICE, folder / "dist")
    for path in folder.rglob("*"):
        path.chmod(0o777 if path.is_dir() else 0o666)  # the container's user agent edits them
    folder.chmod(0o755)
    return folder


def zip_names(path):
    with zipfile.ZipFile(path) as archive:
        return sorted(archive.namelist())


# AWS.

def account():
    return aws_session().client("sts").get_caller_identity()["Account"]


def assume(session=None):
    """An hour as release-demo-coding-agent, with the session name coding-agent."""
    session = session or aws_session()
    sts = session.client("sts")
    arn = f"arn:aws:iam::{sts.get_caller_identity()['Account']}:role/{ROLE}"
    try:
        reply = sts.assume_role(RoleArn=arn, RoleSessionName=SESSION, DurationSeconds=SESSION_SECONDS)
    except Exception as error:
        fail(f"Couldn't assume {ROLE} ({error.__class__.__name__}). Apply its root first: "
             "scripts/demo.sh up agent-access. On the Mac, sign in: aws sso login --profile release-demo")
    creds = reply["Credentials"]
    return {"AWS_ACCESS_KEY_ID": creds["AccessKeyId"], "AWS_SECRET_ACCESS_KEY": creds["SecretAccessKey"],
            "AWS_SESSION_TOKEN": creds["SessionToken"]}, creds["Expiration"]


def role_line(expires, width=None):
    """One line; in a terminal narrower than it (the take's has 56 columns), it
    breaks after a comma rather than in the middle of a word."""
    local = expires.astimezone(AMSTERDAM)
    parts = [f"AWS role: {ROLE},", f"session {SESSION},", f"until {local:%H:%M}"]
    width = width or shutil.get_terminal_size((200, 24)).columns
    lines = [parts[0]]
    for part in parts[1:]:
        if len(lines[-1]) + 1 + len(part) <= width:
            lines[-1] += " " + part
        else:
            lines.append(part)
    return "\n".join(lines)


def production_live(session=None):
    """orders-service-production's live alias: the version it points at, and that version's app version."""
    client = (session or aws_session()).client("lambda")
    function = service(SERVICE)["environments"]["production"]["function"]
    number = client.get_alias(FunctionName=function, Name="live")["FunctionVersion"]
    description = client.get_function_configuration(FunctionName=function, Qualifier=number)["Description"]
    return {"function": function, "version": number, "app_version": description}


def container_env(credentials, key):
    """The docker client's own environment: enough to find Docker, plus the values
    the container receives by name, so no value is ever on a command line."""
    keep = {name: os.environ[name] for name in ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG")
            if name in os.environ}
    return {**keep, **credentials, "AWS_REGION": REGION, "ANTHROPIC_API_KEY": key}


def env_flags():
    return [flag for name in PASSED for flag in ("-e", name)]


def run():
    tag = required_image()
    folder = workspace()
    live = production_live()
    credentials, expires = assume()
    key = get_parameter(ssm_client(), CREDENTIAL)
    name = f"release-demo-coding-agent-{int(time.time())}"
    (folder / "take.json").write_text(json.dumps({
        "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "session_ends": expires.isoformat(), "container": name, "image": tag, "production": live}, indent=2) + "\n")
    print(role_line(expires), flush=True)
    args = ["docker", "run", "--rm", "-it", "--name", name, *env_flags(),
            "-v", f"{folder / SERVICE}:/work/{SERVICE}", "-v", f"{folder / 'dist'}:/work/dist",
            "-w", "/work", tag, "claude"]
    os.execvpe("docker", args, container_env(credentials, key))


# The checks.

def check(expired=False):
    tag = required_image()
    if expired:
        return check_expired()
    credentials, expires = assume()
    allowed = sorted(set(PASSED) | set(RUNTIME) | set(image_env_names(tag)))
    checks = HERE / "checks.py"
    result = subprocess.run(["docker", "run", "--rm", *env_flags(), "-v", f"{checks}:/checks/checks.py:ro",
                             tag, "python3", "/checks/checks.py", "--account", account(),
                             "--allowed-env", ",".join(allowed)],
                            env=container_env(credentials, "not-a-key-checks-make-no-model-call"))
    start_expiry(tag)
    return result.returncode


def start_expiry(tag):
    """A container that keeps a session from now, for check 8 an hour later."""
    running = docker("ps", "--filter", f"name=^{EXPIRY_CONTAINER}$", "--format", "{{.Names}}", check=False).stdout
    if running.strip():
        return
    credentials, expires = assume()
    subprocess.run(["docker", "run", "-d", "--name", EXPIRY_CONTAINER, *env_flags(), "--entrypoint", "sleep",
                    tag, "7200"], env=container_env(credentials, "not-a-key"), capture_output=True, check=True)
    local = expires.astimezone(AMSTERDAM)
    print(f"For check 8: {EXPIRY_CONTAINER} keeps this session. After {local:%H:%M}, run check.sh --expired.")


def check_expired():
    created = docker("inspect", "--format", "{{.Created}}", EXPIRY_CONTAINER, check=False)
    if created.returncode != 0:
        print(f"FAIL 8. no container {EXPIRY_CONTAINER}: run check.sh first, then --expired an hour later")
        return 1
    started = datetime.datetime.fromisoformat(created.stdout.strip()[:19] + "+00:00")
    waited = (datetime.datetime.now(datetime.timezone.utc) - started).total_seconds()
    if waited < SESSION_SECONDS:
        print(f"8. {EXPIRY_CONTAINER} started {int(waited // 60)} minutes ago: "
              f"try again in {int((SESSION_SECONDS - waited) // 60) + 1} minutes")
        return 2
    result = subprocess.run(["docker", "exec", EXPIRY_CONTAINER, "aws", "sts", "get-caller-identity"],
                            capture_output=True, text=True)
    expired_ok = result.returncode != 0 and "ExpiredToken" in result.stderr
    print(f"{'ok  ' if expired_ok else 'FAIL'} 8. {int(waited // 60)} minutes after its start, "
          f"{'any call fails with ExpiredToken' if expired_ok else 'a call did not fail with ExpiredToken'}")
    docker("rm", "-f", EXPIRY_CONTAINER, check=False)
    return 0 if expired_ok else 1


# Status and CloudTrail.

def status():
    session = aws_session()
    iam = session.client("iam")
    try:
        role = iam.get_role(RoleName=ROLE)["Role"]
        print(f"  role {ROLE}: there, sessions up to {role['MaxSessionDuration'] // 60} minutes")
    except iam.exceptions.NoSuchEntityException:
        print(f"  role {ROLE}: not created (scripts/demo.sh up agent-access)")
    tag = image_tag()
    if image_exists(tag):
        version = docker("run", "--rm", "--entrypoint", "claude", tag, "--version", check=False).stdout.strip()
        print(f"  image {tag}: Claude Code {version or 'unknown'}")
    else:
        print(f"  image {tag}: not built (scripts/demo.sh up agent-access)")
    live = production_live(session)
    print(f"  {live['function']}: live on version {live['version']} ({live['app_version']})")
    print("  cost: nothing by the hour (an IAM role)")


def trail(hours=24):
    """The session coding-agent's refused calls in CloudTrail's event history (acceptance check 7)."""
    client = aws_session().client("cloudtrail")
    since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)
    found = 0
    for page in client.get_paginator("lookup_events").paginate(
            LookupAttributes=[{"AttributeKey": "Username", "AttributeValue": SESSION}], StartTime=since):
        for event in page["Events"]:
            detail = json.loads(event["CloudTrailEvent"])
            if not detail.get("errorCode"):
                continue
            found += 1
            when = event["EventTime"].astimezone(AMSTERDAM)
            print(f"  {when:%H:%M:%S}  {event['EventName']:<32} {detail['errorCode']}")
    print(f"{found} refused call(s) by the session {SESSION} in the last {hours} hours. "
          "CloudTrail shows a call some minutes after it happens.")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run")
    checker = sub.add_parser("check")
    checker.add_argument("--expired", action="store_true", help="check 8, an hour after check started its container")
    sub.add_parser("build")
    sub.add_parser("status")
    trailer = sub.add_parser("trail")
    trailer.add_argument("--hours", type=int, default=24)
    args = parser.parse_args()
    if args.command == "run":
        run()
    elif args.command == "check":
        return check(args.expired)
    elif args.command == "build":
        build()
    elif args.command == "status":
        status()
    else:
        trail(args.hours)
    return 0


if __name__ == "__main__":
    sys.exit(main())
