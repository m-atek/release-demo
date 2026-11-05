"""Video 30: which real package is younger than the rule right now, and the pins that go with it.

    python scripts/demos/young_dependency/pick.py show             the watch list, read from PyPI now, and the choice. Writes nothing
    python scripts/demos/young_dependency/pick.py stage 1.32.0     that version on main with today's pins, as the GitHub App
    python scripts/demos/young_dependency/pick.py write 1.32.0     the same inside a fresh clone: host.stage runs this

A version's age can't be planned: it comes from the package index at the moment of the
take. So the pins are written on the day:

- 1.32.0 holds the newest version of the first package on the watch list that is younger
  than the rule by at least MARGIN_HOURS, with an older version to fall back on, nothing
  known against it on OSV, and pins pip can resolve. Everything else stays older than the rule.
- 1.32.1 holds, for each pin on main that is too young now, the newest version that passes:
  what Slack's refusal names, by the same function. Nothing else moves.

boto3 comes first: it's the only package on the list the handler really imports, and AWS
uploads one most weekdays at about 20:20 Amsterdam. The others are small packages with few
or no dependencies, there for the days it doesn't: they are declared, not imported, which is
what the script's fallback lines say. If nothing on the list qualifies, this says so and exits 1:
the take waits. Nothing here makes a package young. No index but PyPI, no clock but this machine's.
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
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(1, str(SCRIPTS))  # after this folder

import package_age  # noqa: E402
from common import ROOT, fail, git, service, version_of  # noqa: E402

SERVICE = "orders-service"
REFUSED_VERSION, PASSING_VERSION = "1.32.0", "1.32.1"
# Under the rule by this much, or the take could outlive the refusal.
MARGIN_HOURS = 4
OSV = "https://api.osv.dev/v1/query"
AMSTERDAM = ZoneInfo("Europe/Amsterdam")
DECLARED = "# Declared for this release. Nothing imports it yet."
THIS_FILE = "scripts/demos/young_dependency/pick.py"


@dataclass(frozen=True)
class Candidate:
    package: str
    released_with: tuple = ()   # same version, or pip can't resolve it: botocore with boto3
    imported: bool = False      # the handler really imports it

    @property
    def names(self):
        return (self.package, *self.released_with)


# In the order they are tried. Measured over the 180 days to 5 November 2026, weekdays 09:00 to 18:00 Amsterdam:
# boto3 alone was under 68 hours 86% of the time, 91% Tuesday to Friday; with the rest 97%, 98% Tuesday to Friday.
WATCH = (Candidate("boto3", ("botocore",), imported=True), Candidate("filelock"), Candidate("platformdirs"),
         Candidate("cachetools"), Candidate("hypothesis"), Candidate("coverage"))


@dataclass
class Survey:
    candidate: Candidate
    newest: str = None
    newest_age: float = None
    uploaded: float = None
    older: str = None
    older_age: float = None
    why_not: str = None


def settings():
    return service(SERVICE)["package_age"]


def runtime():
    return service(SERVICE)["runtime"]


def age_text(seconds):
    return package_age.age_text(seconds)


def amsterdam(seconds, form):
    return datetime.datetime.fromtimestamp(seconds, AMSTERDAM).strftime(form)


# Reading the index and OSV.

def survey(candidate, rules, python, now, read=None):
    """Its newest final version with a wheel the build could take, and the newest that passes. why_not says if it won't do."""
    read = read or package_age.http_json
    rule = rules["min_hours"] * 3600
    found = Survey(candidate)
    releases = package_age.releases_of(read(f"{package_age.INDEX}/{candidate.package}/json"), python)
    if not releases:
        found.why_not = "no wheel the build can take"
        return found
    found.uploaded, found.newest = releases[0]
    found.newest_age = now - found.uploaded
    older = next(((uploaded, version) for uploaded, version in releases if now - uploaded >= rule), None)
    if older:
        found.older, found.older_age = older[1], now - older[0]
    if found.newest_age >= rule - MARGIN_HOURS * 3600:
        found.why_not = f"newest is not {rules['min_hours'] - MARGIN_HOURS} hours under the rule"
    elif not older:
        found.why_not = "no version old enough to fall back on"
    for other in candidate.released_with:
        versions = {version for _, version in package_age.releases_of(read(f"{package_age.INDEX}/{other}/json"), python)}
        if found.newest not in versions or (found.older and found.older not in versions):
            found.why_not = found.why_not or f"{other} has no matching version"
    return found


def osv_findings(package, version):
    """OSV's ids for this version, [] if none, None if OSV can't be read: then it's for a person to look."""
    body = json.dumps({"package": {"name": package, "ecosystem": "PyPI"}, "version": version}).encode()
    request = urllib.request.Request(OSV, data=body, headers={"Content-Type": "application/json", "User-Agent": package_age.USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=package_age.READ_TIMEOUT) as response:
            return [entry["id"] for entry in json.loads(response.read()).get("vulns", [])]
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as error:
        print(f"OSV not read for {package} {version} ({error.__class__.__name__}): look by hand before saying nothing is wrong with it")
        return None


# The pins.

def rewrite(text, wanted):
    """The requirements text with these packages at these versions. A package it doesn't hold goes last, marked as declared."""
    wanted = dict(wanted)
    lines = []
    for line in text.splitlines():
        found = re.fullmatch(r"([A-Za-z0-9._-]+)==\S+", line.strip())
        if found and found.group(1).lower() in wanted:
            lines.append(f"{found.group(1)}=={wanted.pop(found.group(1).lower())}")
        else:
            lines.append(line)
    for package, version in wanted.items():
        lines += [DECLARED, f"{package}=={version}"]
    return "\n".join(lines) + "\n"


def refused_pins(found, base, primary):
    """1.32.0's text: this candidate at its newest. Should it not be the first on the list, boto3 stays older than the rule."""
    wanted = {name: found.newest for name in found.candidate.names}
    if found.candidate is not WATCH[0]:
        wanted.update({name: primary.older for name in WATCH[0].names})
    return rewrite(base, wanted)


def passing_pins(text, rules, python, now, resolver=None, read=None):
    """(the text, moved): for each pin too young now, the newest version that passes, as Slack's line names it. Nothing else moves."""
    with tempfile.TemporaryDirectory(prefix="package-age-pins-") as temp:
        path = Path(temp) / "requirements.txt"
        path.write_text(text)
        rows = package_age.rows_for((resolver or package_age.resolve)(path, python), rules, now, read)
    moved = {}
    for row in rows:
        if row.too_young:
            version, _ = package_age.newest_that_passes(row.package, rules, python, now, read)
            if not version:
                raise package_age.NoReading(f"no version of {row.package} passes yet")
            moved[row.package.lower()] = version
    return rewrite(text, moved), moved


def young_names(text, rules, python, now, resolver=None, read=None):
    """The packages too young under these pins, by the stage's own reading."""
    with tempfile.TemporaryDirectory(prefix="package-age-pins-") as temp:
        path = Path(temp) / "requirements.txt"
        path.write_text(text)
        rows = package_age.rows_for((resolver or package_age.resolve)(path, python), rules, now, read)
    return {row.package.lower() for row in rows if row.too_young}


def verdict(found, base, primary, rules, python, now, resolver=None, read=None, findings=None):
    """Why not this candidate, or None: nothing known against its newest version, and pins pip resolves that do what the take needs."""
    findings = findings or osv_findings
    if found.why_not:
        return found.why_not
    for name in found.candidate.names:
        known = findings(name, found.newest)
        if known:
            return f"{name} {found.newest} has findings on OSV: {', '.join(known)}"
    text = refused_pins(found, base, primary)
    try:
        young = young_names(text, rules, python, now, resolver, read)
        if young != {name.lower() for name in found.candidate.names}:
            return f"under these pins the young ones are {sorted(young)}, not {sorted(found.candidate.names)}"
        text, _ = passing_pins(text, rules, python, now, resolver, read)
        left = young_names(text, rules, python, now, resolver, read)
        if left:
            return f"the older pins still hold young packages: {sorted(left)}"
    except package_age.NoReading as error:
        return str(error)
    return None


def choose(base, now, read=None, resolver=None, findings=None, out=print):
    """(the first candidate that qualifies, boto3's survey, the whole list). None when nothing does: the take waits."""
    rules, python = settings(), runtime()
    surveys = []
    for candidate in WATCH:
        try:
            surveys.append(survey(candidate, rules, python, now, read))
        except package_age.NoReading as error:
            surveys.append(Survey(candidate, why_not=str(error)))
    primary = surveys[0]
    chosen = None
    for found in surveys:
        if chosen is None:
            if found.candidate is not WATCH[0] and not primary.older:
                found.why_not = found.why_not or "boto3 has no version old enough to stay on"
            found.why_not = verdict(found, base, primary, rules, python, now, resolver, read, findings)
            if not found.why_not:
                chosen = found
    for found in surveys:
        newest = f"{found.newest}, {age_text(found.newest_age)}" if found.newest else "none"
        older = f"{found.older}, {age_text(found.older_age)}" if found.older else "none"
        verdict_text = "chosen" if found is chosen else (found.why_not or "not tried: an earlier one qualified")
        out(f"{found.candidate.package:<14} newest {newest:<26} passes {older:<26} {verdict_text}")
    return chosen, primary


def describe(found, now, out=print):
    """What the take needs to know about the choice, and how long it holds."""
    rules = settings()
    names = " and ".join(f"{name} {found.newest}" for name in found.candidate.names)
    out(f"Chosen: {names}, {age_text(found.newest_age)} old. Newest that passes: {found.older}, {age_text(found.older_age)} old.")
    out("The handler imports it." if found.candidate.imported
        else "Declared, not imported: use the script's fallback lines (Rules, \"If the package isn't boto3\").")
    limit = found.uploaded + rules["min_hours"] * 3600
    out(f"Under the rule until {amsterdam(limit, '%a %d %b %H:%M')} Amsterdam. Take it before "
        f"{amsterdam(limit - MARGIN_HOURS * 3600, '%a %d %b %H:%M')}.")
    midnight = (int(now) // 86400 + 1) * 86400
    if found.uploaded >= midnight - 86400:
        out(f"PyPI's page says \"about N hours ago\" until {amsterdam(midnight, '%H:%M')} Amsterdam, then the date.")
    else:
        out("PyPI's page shows the date, not the hours: it only counts hours for a file uploaded since midnight UTC.")


def show(now=None, read=None, resolver=None, findings=None, out=print):
    now = now or time.time()
    rules = settings()
    out(f"Read from PyPI at {amsterdam(now, '%d %b %H:%M')} Amsterdam. The rule: {rules['min_hours']} hours, "
        f"so under {rules['min_hours'] - MARGIN_HOURS} for a take.")
    base = (ROOT / "demo-states" / SERVICE / REFUSED_VERSION / "requirements.txt").read_text()
    chosen, _ = choose(base, now, read, resolver, findings, out)
    if chosen is None:
        out("Nothing on the watch list qualifies right now. The take waits: boto3 uploads most weekdays at about 20:20 "
            "Amsterdam, so try after that, or tomorrow. Nothing here makes a package young.")
        return 1
    describe(chosen, now, out)
    return 0


# On main.

def stage(version):
    """From the Mac or the host: a fresh clone of main, this script in it, pushing as the GitHub App."""
    import host

    host.stage(SERVICE, version, wrapper=[THIS_FILE, "write", version])


def commit_and_push(version, what):
    config = service(SERVICE)
    git("add", "--all", "--", config["path"])
    if git("diff", "--cached", "--quiet", "--", config["path"], check=False).returncode == 0:
        print("Nothing changed: main already has these pins")
        return
    git("commit", "--quiet", "-m", f"demo: stage {SERVICE} {version}, {what} [skip ci]", "--", config["path"])
    git("push", "--quiet", "origin", "HEAD:main")
    print(f"Pushed to main: {SERVICE} {version}, {what}")


def write(version, now=None, read=None, resolver=None, findings=None):
    """In a fresh clone of main. Stages the state's files, then writes the day's pins over its requirements.txt."""
    config = service(SERVICE)
    if version not in (REFUSED_VERSION, PASSING_VERSION):
        fail(f"pick.py writes {REFUSED_VERSION} and {PASSING_VERSION}, not {version}")
    now = now or time.time()
    rules, python = settings(), runtime()
    requirements = Path(config["dir"]) / "requirements.txt"
    if version == PASSING_VERSION:
        if version_of(config["dir"]) not in (REFUSED_VERSION, PASSING_VERSION):
            fail(f"main has {version_of(config['dir'])}. Stage {REFUSED_VERSION} first: {PASSING_VERSION} moves its pins.")
        on_main = requirements.read_text()
        try:
            text, moved = passing_pins(on_main, rules, python, now, resolver, read)
        except package_age.NoReading as error:
            fail(f"Pins not written: {error}.")
        what = ", ".join(f"{package} {pin}" for package, pin in moved.items()) or "pins as they were"
    else:
        base = (ROOT / "demo-states" / SERVICE / version / "requirements.txt").read_text()
        chosen, primary = choose(base, now, read, resolver, findings)
        if chosen is None:
            fail("Nothing on the watch list qualifies right now, so nothing is staged. The take waits.")
        describe(chosen, now)
        text = refused_pins(chosen, base, primary)
        what = f"{chosen.candidate.package} {chosen.newest}"
    result = subprocess.run([sys.executable, str(SCRIPTS / "stage.py"), SERVICE, version, "--no-commit"], cwd=ROOT, capture_output=True, text=True)
    print(result.stdout.strip())
    if result.returncode != 0:
        fail(f"stage.py failed: {result.stderr.strip()[-300:]}")
    requirements.write_text(text)
    commit_and_push(version, what)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("show")
    for name in ("stage", "write"):
        one = sub.add_parser(name)
        one.add_argument("version", choices=[REFUSED_VERSION, PASSING_VERSION])
    args = parser.parse_args(argv)
    if args.command == "show":
        return show()
    if args.command == "stage":
        return stage(args.version) or 0
    return write(args.version) or 0


if __name__ == "__main__":
    sys.exit(main())
