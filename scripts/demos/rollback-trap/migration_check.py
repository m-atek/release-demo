"""Migration check (demo 20), after SBOM and scan: does a new migration break what runs now?

    python scripts/demos/rollback-trap/migration_check.py orders-service
    SQUAWK=/path/to/squawk python ...      a local squawk instead of the pinned image

The migrations new since production's release are the files in this run's
package that production's release package in JFrog Container Registry
(<releases>/<version>/, the version the alias live points at) doesn't have.
squawk runs over them with Postgres 17 as the target and each file in a
transaction of its own, as the deploy applies them. Any finding fails the
stage, before anything is published, with squawk's lines in the log and one
in Slack: "Migration check failed: renaming-column in 0003_rename_delivery_days.sql."
A rule switched off by a "-- squawk-ignore" comment passes and is printed. A
migration production ran whose file has changed since fails by name.

squawk runs in the pinned image of jenkins/toolchain/squawk.Dockerfile, with
no network.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import trap
import run_state
from common import ROOT, fail

DOCKERFILE = ROOT / "jenkins" / "toolchain" / "squawk.Dockerfile"
PG_VERSION = "17.0"


def image():
    """The squawk image, built once per change of its Dockerfile."""
    tag = "release-demo/squawk:" + hashlib.sha256(DOCKERFILE.read_bytes()).hexdigest()[:12]
    if subprocess.run(["docker", "image", "inspect", tag], capture_output=True).returncode != 0:
        subprocess.run(["docker", "build", "-q", "-t", tag, "-f", str(DOCKERFILE), str(DOCKERFILE.parent)],
                       check=True, capture_output=True)
    return tag


def squawk(files):
    """squawk's findings for these files, as dicts with file (its name), line and column (from 1), rule_name, message."""
    with tempfile.TemporaryDirectory() as temp:
        work = Path(temp)
        for name, content in files.items():
            (work / name).write_bytes(content)
        work.chmod(0o755)
        if os.environ.get("SQUAWK"):
            command, inside = [os.environ["SQUAWK"]], str(work)
        else:
            command, inside = ["docker", "run", "--rm", "--network", "none", "-v", f"{work}:/work:ro", image()], "/work"
        result = subprocess.run([*command, f"--pg-version={PG_VERSION}", "--assume-in-transaction", "--reporter",
                                 "json", *(f"{inside}/{name}" for name in sorted(files))],
                                capture_output=True, text=True, env={**os.environ, "NO_COLOR": "1"})
    if result.returncode not in (0, 1):
        fail(f"FAIL squawk couldn't read the migrations: {(result.stderr or result.stdout).strip()[:300]}")
    found = json.loads(result.stdout or "[]")
    return [{**f, "file": Path(f["file"]).name, "line": f["line"] + 1, "column": f["column"] + 1} for f in found]


def new_and_changed(files, base_files):
    new = {name: content for name, content in files.items() if name not in base_files}
    changed = [name for name, content in files.items() if name in base_files and base_files[name] != content]
    return new, changed


def summary(findings):
    """The rules each file breaks, for Slack: "renaming-column in 0003_rename_delivery_days.sql"."""
    by_file = {}
    for finding in findings:
        rules = by_file.setdefault(finding["file"], [])
        if finding["rule_name"] not in rules:
            rules.append(finding["rule_name"])
    return ", ".join(f"{', '.join(rules)} in {name}" for name, rules in by_file.items())


def this_package(state):
    """This run's migrations: from its package in dist/, or the service folder before a build."""
    package = trap.workspace() / "dist" / trap.SERVICE / f"{trap.SERVICE}-{state.get('version')}.zip"
    if package.exists():
        return trap.package_migrations(package.read_bytes()), str(package.relative_to(trap.workspace()))
    return trap.folder_migrations(trap.config()["dir"]), trap.config()["path"]


def check(files, base_files, base_version):
    """Exit status, after printing the lines and telling Slack when it fails."""
    new, changed = new_and_changed(files, base_files)
    state = run_state.load()
    if changed:
        line = "Migration check failed: " + ", ".join(f"{name} changed after production ran it" for name in changed) + "."
        for name in changed:
            print(f"FAIL {name}: production ran it from {base_version}'s release, and its file has changed since")
        trap.tell(line)
        return 1
    if not new:
        print(f"ok   no migration that production's {base_version} hasn't run")
        return 0
    print(f"ok   new since production's {base_version}: {', '.join(sorted(new))}")
    for name, content in sorted(new.items()):
        for rule in trap.ignored_rules(content.decode()):
            print(f"squawk-ignore {rule} in {name}")
    findings = squawk(new)
    for finding in findings:
        print(f"FAIL {finding['file']}:{finding['line']}:{finding['column']}: {finding['rule_name']}: {finding['message']}")
    state["migrations"] = {"base_version": base_version, "new": sorted(new), "only_add": trap.only_add(new),
                           "findings": len(findings)}
    run_state.save(state)
    if findings:
        trap.tell(f"Migration check failed: {summary(findings)}.")
        return 1
    print(f"ok   squawk {PG_VERSION}: nothing in {', '.join(sorted(new))}")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("service")
    args = parser.parse_args()
    if args.service != trap.SERVICE:
        fail(f"The migration check knows {trap.SERVICE}, not {args.service}.")
    state = run_state.load()
    files, source = this_package(state)
    print(f"ok   this release's migrations: {source}, {', '.join(files) or 'none'}")
    _, base_version = trap.live_version("production")
    base, path = trap.release_package(base_version)
    if base is None:
        fail(f"FAIL production runs {base_version}, but JFrog Container Registry has no {path}: nothing to compare "
             f"with. reset.py rollback-trap puts it back.")
    base_files = trap.package_migrations(base)
    print(f"ok   production's release: {path}, {', '.join(base_files) or 'no migrations'}")
    return check(files, base_files, base_version)


if __name__ == "__main__":
    sys.exit(main())
