"""Video 30's package age: how old is each package going into the zip? The first
stage of the release pipeline, "Package age", asks pip what the build would
install and PyPI when each of those files was uploaded, and refuses a release
when one is younger than the rule, before anything is built. Only the versions
services.yaml's package_age.versions names have the stage (run_state.stages_for).

    python3 scripts/demos/young_dependency/package_age.py orders-service        the stage: exit 0 passes, exit 3 refuses
    python3 scripts/demos/young_dependency/package_age.py orders-service --no-reading "<reason>"
    python3 scripts/demos/young_dependency/package_age.py approved              after Alex's approval, on an override run
    python scripts/demos/young_dependency/package_age.py status [--service orders-service] [--requirements <file>]
                                                                                the table and the decision, writing nothing

The stage, in order:

1. What would go in the zip: pip with build.py's flags, --dry-run --report, so a
   package nobody pinned counts too, and nothing is installed.
2. When each of those exact files was uploaded: PyPI's JSON, read now, matched by
   the file's SHA-256. Nothing is cached. A file PyPI added to an old version
   later counts with its own, later time.
3. Age is now minus that time. The rule is services.yaml's package_age.min_hours.
4. All old enough: the lines, the report on the change, a work note, pass.
5. Too young, and override asked: the lines, the change's description and the
   report, pass. The change is Normal because override isn't a standard option,
   so Alex decides, and an approval with no comment stops it (approved below).
6. Too young otherwise: refused, naming the newest version of each that passes.
   stopped() then says "Nothing reached production."

If pip or PyPI can't answer, the release isn't refused and doesn't pass quietly:
"no reading", force_normal, and Alex decides. /rollback has no such stage, because
going back always lowers risk. It says nothing about whether a package is good:
a scan looks up what has been reported, and this only asks how long anyone has
had to report it. Nothing here calls a model, and it needs no AWS right of its own.
"""

import argparse
import datetime
import json
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(1, str(SCRIPTS))  # after this folder

import yaml  # noqa: E402

import run_state  # noqa: E402
from build import wheel_flags  # noqa: E402
from common import ROOT, people, service, write_json  # noqa: E402

STAGE = "Package age"
REFUSED = 3
OVERRIDE = "override"
REPORT = "package-age.json"
INDEX = "https://pypi.org/pypi"
USER_AGENT = "release-demo-package-age"
READ_TIMEOUT = 15
READ_TRIES = 2
PIP_TIMEOUT = 120
# A version with one of these isn't what pip takes from a plain pin's "newest".
PRE_RELEASE = re.compile(r"(a|b|c|rc|alpha|beta|pre|preview|dev)\d*", re.I)
MANYLINUX = re.compile(r"manylinux(1|2010|2014|_2_(\d+))_x86_64")
MANYLINUX2014_GLIBC_MINOR = 17


class NoReading(Exception):
    """pip or PyPI didn't answer, or didn't say what the stage needs. The words go in Slack's line."""


@dataclass(frozen=True)
class Resolved:
    package: str
    version: str
    file: str
    sha256: str


@dataclass(frozen=True)
class Row:
    package: str
    version: str
    file: str
    sha256: str
    uploaded: float
    age: float
    too_young: bool


# The words.

def age_text(seconds):
    """1h 08m below the rule's own 72 hours, then 3d 01h. Up to the next minute or hour, never down."""
    minutes = int(-(-seconds // 60))
    if minutes < 72 * 60:
        return f"{minutes // 60}h {minutes % 60:02d}m"
    hours = minutes // 60 + (1 if minutes % 60 else 0)
    return f"{hours // 24}d {hours % 24:02d}h"


def published_line(row):
    return f"{row.package} {row.version} was published {age_text(row.age)} ago."


def rule_line(settings):
    return f"The rule is {settings['min_hours']} hours."


def passing_line(passing):
    """Newest that passes: boto3 1.43.121, botocore 1.43.121."""
    named = [f"{package} {version}" for package, (version, _) in passing.items() if version]
    missing = [package for package, (version, _) in passing.items() if not version]
    lines = []
    if named:
        lines.append(f"Newest that passes: {', '.join(named)}.")
    if missing:
        lines.append(f"No version of {', '.join(missing)} passes yet.")
    return "\n".join(lines)


def ask_line(state, approver):
    return f"To ask {approver}: /release {state['service']} {state['jira_key']} {OVERRIDE}"


def approver_name():
    """Alex, from people.yaml's approver: true: the first word of the display name."""
    everyone = yaml.safe_load((ROOT / "people.yaml").read_text())
    return next(person["display_name"] for person in everyone.values() if person.get("approver")).split()[0]


def asked_by(state):
    """Who asked, by name: Sam Developer. Never a Slack member id."""
    try:
        for person in people().values():
            if state.get("requested_by") and person.get("slack_user_id") == state["requested_by"]:
                return person["display_name"]
    except (Exception, SystemExit):
        pass
    return "the person who asked"


def utc(seconds):
    return datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc)


def table(rows, settings, now):
    """The log's table, for Blue Ocean's narrow pane: at most 66 characters a line."""
    lines = [f"Read from {INDEX} at {utc(now):%d %b %H:%M:%S} UTC.",
             f"The rule: {settings['min_hours']} hours.",
             f"{'package':<17}{'version':<13}{'uploaded, UTC':<18}{'age':<9}"]
    for row in sorted(rows, key=lambda row: row.age):
        lines.append(f"{row.package:<17}{row.version:<13}{utc(row.uploaded):%Y-%m-%d %H:%M}  {age_text(row.age):<9}"
                     f"{'too young' if row.too_young else 'ok'}")
    return "\n".join(lines)


# Reading from outside.

def http_json(url):
    """GET a JSON page, with one more try, or NoReading. Nothing is kept between runs."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    last = None
    for attempt in range(READ_TRIES):
        try:
            with urllib.request.urlopen(request, timeout=READ_TIMEOUT) as response:
                return json.loads(response.read())
        except (urllib.error.URLError, TimeoutError, ValueError) as error:
            last = error
            time.sleep(attempt)
    print(f"Package age: {url} not read ({last.__class__.__name__}: {str(last)[:200]})")
    raise NoReading("PyPI didn't answer")


def seconds_of(stamp):
    """2026-11-05T19:21:33.566146Z, PyPI's upload_time_iso_8601, as epoch seconds."""
    return datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()


def resolve(requirements, runtime):
    """[Resolved]: what the build would install, from the same flags, with nothing installed."""
    with tempfile.TemporaryDirectory(prefix="package-age-") as temp:
        report = Path(temp) / "report.json"
        command = [sys.executable, "-m", "pip", "install", "--dry-run", "--ignore-installed", "--quiet",
                   "--disable-pip-version-check", "--report", str(report), "--requirement", str(requirements),
                   "--target", str(Path(temp) / "nothing"), *wheel_flags(runtime)]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=PIP_TIMEOUT)
        except (OSError, subprocess.SubprocessError) as error:
            print(f"Package age: pip didn't run ({error.__class__.__name__})")
            raise NoReading("pip didn't run") from error
        if result.returncode != 0:
            print(f"Package age: pip exited {result.returncode}: {result.stderr.strip()[-400:]}")
            raise NoReading("pip couldn't resolve the packages")
        try:
            items = json.loads(report.read_text())["install"]
            return [Resolved(item["metadata"]["name"], item["metadata"]["version"],
                             item["download_info"]["url"].rsplit("/", 1)[-1],
                             item["download_info"]["archive_info"]["hashes"]["sha256"]) for item in items]
        except (OSError, ValueError, KeyError, TypeError) as error:
            print(f"Package age: pip's report not read ({error.__class__.__name__}: {error})")
            raise NoReading("pip's report can't be read") from error


def uploaded_at(item, read=None):
    """When PyPI received this exact file, found by its SHA-256 among the version's files."""
    read = read or http_json
    page = read(f"{INDEX}/{item.package}/{item.version}/json")
    for entry in page.get("urls", []):
        if entry.get("digests", {}).get("sha256") == item.sha256:
            return seconds_of(entry["upload_time_iso_8601"])
    print(f"Package age: PyPI lists no file of {item.package} {item.version} with the checksum pip chose")
    raise NoReading("PyPI doesn't list that file")


def rows_for(items, settings, now, read=None):
    """A row per package, every one read now. One unreadable package means no reading at all."""
    rows = []
    for item in items:
        uploaded = uploaded_at(item, read)
        age = now - uploaded
        rows.append(Row(item.package, item.version, item.file, item.sha256, uploaded, age, age < settings["min_hours"] * 3600))
    return rows


def wheel_fits(name, runtime):
    """Would build.py's flags take this wheel? py3 or this Python, abi3 from an older one; any, or manylinux up to 2014's."""
    if not name.endswith(".whl"):
        return False
    python, abi, platform = name[:-4].split("-")[-3:]
    minor = int(runtime.removeprefix("python").split(".")[1])
    tags = python.split(".")
    if not (any(tag in ("py3", f"cp3{minor}") for tag in tags)
            or (abi == "abi3" and any(re.fullmatch(r"cp3\d+", tag) and int(tag[3:]) <= minor for tag in tags))):
        return False
    if platform == "any":
        return True
    for each in platform.split("."):
        found = MANYLINUX.fullmatch(each)
        if found and (found.group(2) is None or int(found.group(2)) <= MANYLINUX2014_GLIBC_MINOR):
            return True
    return False


def releases_of(page, runtime):
    """[(uploaded, version)], newest first: final versions with a wheel the build could take, not yanked.
    Uploaded is its wheel's time, the latest if there are several."""
    found = []
    for version, files in page.get("releases", {}).items():
        if PRE_RELEASE.search(version):
            continue
        wheels = [entry for entry in files if wheel_fits(entry["filename"], runtime) and not entry.get("yanked")]
        if wheels:
            found.append((max(seconds_of(entry["upload_time_iso_8601"]) for entry in wheels), version))
    return sorted(found, reverse=True)


def newest_that_passes(package, settings, runtime, now, read=None):
    """(version, age), or (None, None) if none is old enough. For the line only: it decides nothing."""
    read = read or http_json
    for uploaded, version in releases_of(read(f"{INDEX}/{package}/json"), runtime):
        if now - uploaded >= settings["min_hours"] * 3600:
            return version, now - uploaded
    return None, None


# Writing it down.

def report_path():
    return run_state.REPORTS / REPORT


def row_body(row):
    return {"package": row.package, "version": row.version, "file": row.file, "sha256": row.sha256,
            "uploaded": run_state.iso(row.uploaded), "age_seconds": round(row.age), "age": age_text(row.age),
            "result": "too young" if row.too_young else "ok"}


def record(state, settings, result, blocks, rows=(), now=None, reason=None, passing=None):
    """The report, the run state and Slack. Slack only prints on an error: what the stage decided stands either way."""
    from notify import post

    write_json(report_path(), {
        "service": state["service"], "version": state["version"], "build": (state.get("build") or {}).get("number"),
        "jira_key": state.get("jira_key"), "option": state.get("option"), "result": result, "reason": reason,
        "rule_hours": settings["min_hours"], "index": INDEX, "read_at": run_state.iso(now) if now else None,
        "packages": [row_body(row) for row in sorted(rows, key=lambda row: row.age)],
        "newest_that_passes": {package: version for package, (version, _) in (passing or {}).items()},
    })
    state["package_age"] = {**(state.get("package_age") or {}), "result": result}
    state.setdefault("lines", []).extend(blocks)
    run_state.save(state)
    for block in blocks:
        print(block)
    quietly("Slack", lambda: post(state))


def quietly(name, write):
    try:
        write()
    except BaseException as error:  # fail() exits: still, nothing here undoes the decision
        print(f"Package age: {name} not written ({error.__class__.__name__}: {str(error)[:200]})")


def no_reading(state, settings, reason):
    """Toward a person, never silent: the release carries on to a Normal change."""
    line = f"Package age: no reading ({reason}). {approver_name()} decides."
    state["force_normal"] = True
    state.setdefault("change_details", []).append(line)
    record(state, settings, "no-reading", [line], reason=reason)
    return 0


def run_stage(state, service_name, now=None, read=None, resolver=None):
    config = service(service_name)
    settings = config["package_age"]
    now = now or run_state.now()
    requirements = Path(config["dir"]) / "requirements.txt"
    try:
        items = (resolver or resolve)(requirements, config["runtime"]) if requirements.exists() else []
        if not items:
            record(state, settings, "no-packages", ["Package age: no third-party packages."], now=now)
            return 0
        rows = rows_for(items, settings, now, read)
    except NoReading as error:
        return no_reading(state, settings, str(error))
    print(table(rows, settings, now))

    young = [row for row in rows if row.too_young]
    if not young:
        youngest = min(rows, key=lambda row: row.age)
        state.setdefault("change_attachments", []).append(str(report_path()))
        state.setdefault("change_work_notes", []).append(
            f"Package age: {len(rows)} packages read from PyPI at {utc(now):%H:%M} UTC. Youngest: {youngest.package} "
            f"{youngest.version}, {age_text(youngest.age)} old. {rule_line(settings)}")
        record(state, settings, "passed",
               [f"{len(rows)} packages read from PyPI. {rule_line(settings)}\n"
                f"Youngest: {youngest.package} {youngest.version}, {age_text(youngest.age)} old."], rows, now)
        return 0

    approver = approver_name()
    published = "\n".join(published_line(row) for row in young)
    if state.get("option") == OVERRIDE:
        state["package_age"] = {"override": True, "young": [f"{row.package} {row.version}, {age_text(row.age)} old" for row in young]}
        state.setdefault("change_attachments", []).append(str(report_path()))
        state.setdefault("change_details", []).extend([
            f"Package age: {' '.join(published_line(row) for row in young)} {rule_line(settings)}",
            f"Source: PyPI's upload times, read at {utc(now):%H:%M} UTC, every package in {REPORT}.",
            f"Override asked by {asked_by(state)}. While a package is younger than the rule, nothing else releases "
            "without this approval."])
        record(state, settings, "override-asked",
               [f"{published}\n{rule_line(settings)}", f"Override asked: {approver} decides in ServiceNow."], rows, now)
        return 0

    try:
        passing = {row.package: newest_that_passes(row.package, settings, config["runtime"], now, read) for row in young}
    except NoReading:
        passing = {}
    blocks = [f"{published}\n{rule_line(settings)}"]
    named = passing_line(passing)
    blocks.append("\n".join(filter(None, [named, ask_line(state, approver)])))
    record(state, settings, "refused", blocks, rows, now, passing=passing)
    return REFUSED


def approved(state):
    """After the approved line, on an override run only: the approver's comment goes on the record, or the release stops."""
    age = state.get("package_age") or {}
    if not age.get("override"):
        return 0
    import change as change_module
    from notify import post

    name = (state.get("change") or {}).get("approved_by") or approver_name()
    try:
        sn, _ = change_module.connect()
        comment = change_module.approved_comment(sn, state["change"])
    except (Exception, SystemExit) as error:
        print(f"Package age: the approval's comment not read ({error.__class__.__name__})")
        comment = ""
    if not comment:
        state.setdefault("lines", []).append(f"Override refused: no reason in {name}'s approval.")
        run_state.save(state)
        quietly("Slack", lambda: post(state))
        return REFUSED
    limit = change_module.REJECTION_COMMENT_LIMIT
    shown = comment if len(comment) <= limit else comment[:limit - 3].rstrip() + "..."
    state.setdefault("lines", []).append(f"Override reason: {shown}")
    state["close_notes_extra"] = (f"Released with {'; '.join(age.get('young', []))}, younger than the rule. "
                                  f"Override approved by {name}: {comment}")
    run_state.save(state)
    quietly("Slack", lambda: post(state))
    return 0


def status(service_name, requirements=None, out=print, now=None, read=None, resolver=None):
    """The table and the decision for what's in this checkout, or a file you name. Writes nothing."""
    config = service(service_name)
    settings = config["package_age"]
    now = now or time.time()
    path = Path(requirements) if requirements else Path(config["dir"]) / "requirements.txt"
    rows = rows_for((resolver or resolve)(path, config["runtime"]), settings, now, read)
    out(table(rows, settings, now))
    young = [row for row in rows if row.too_young]
    if young:
        for row in young:
            out(published_line(row))
        out(rule_line(settings))
    out(f"Decision: {'refused' if young else 'passes'}.")
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    command = argv[0] if argv else ""
    if command == "approved":
        return approved(run_state.load())
    if command == "status":
        parser = argparse.ArgumentParser(description="The table and the decision, writing nothing")
        parser.add_argument("--service", default="orders-service")
        parser.add_argument("--requirements", help="a requirements file; default the checkout's")
        args = parser.parse_args(argv[1:])
        return status(args.service, args.requirements)
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("service")
    parser.add_argument("--no-reading", metavar="REASON", help="record that there is no reading, and why")
    args = parser.parse_args(argv)
    state = run_state.load()
    if args.no_reading:
        return no_reading(state, service(args.service)["package_age"], args.no_reading)
    return run_stage(state, args.service)


if __name__ == "__main__":
    sys.exit(main())
