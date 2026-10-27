"""The data-center side of the inventory rollout, run on dc-agent.

    python3 scripts/demos/linux/datacenter.py --request-env REQUEST --results .release/dc-results.jsonl
    python3 scripts/demos/linux/datacenter.py --request-file request.json --results results.jsonl

The Jenkins job inventory-dc-wave runs it with REQUEST, a build parameter
(the tools host's release asks for it); from the Mac, fleet.py runs it with a
file through multipass exec. It runs Ansible over SSH to inv-dc-01 and
inv-dc-02 and writes each server's result to --results. The Jenkins
credentials (JCR token, Access service token, app secret) arrive as
environment variables and go to the servers in a root-only file.

The request is checked before anything runs: anyone who may start the job
could otherwise point the servers, and the tokens, at another address.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from common import fail  # noqa: E402
from fleet import by_site, run_dc_here  # noqa: E402

PLAYBOOKS = {"baseline.yml", "release.yml"}
ACTIONS = {"deploy", "activate", "back", "previous", "check"}
VERSION = re.compile(r"^\d+\.\d+\.\d+$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
# Packages only from JCR through Cloudflare, in the inventory repositories.
PACKAGE_URL = re.compile(r"^https://artifactory-demo\.es\.nl/artifactory/"
                         r"(demos-(candidates|releases)/inventory|inventory-(candidates|releases))/"
                         r"\d+\.\d+\.\d+/(\w+/)?inventory-\d+\.\d+\.\d+\.zip$")
TAGS = {None, "java"}


def checked(request, local_root=None):
    """The request, if every part of it is one this job may run. Fails otherwise."""
    if request.get("playbook") not in PLAYBOOKS:
        fail(f"Playbook {request.get('playbook')!r} isn't one this job runs")
    if request.get("tags") not in TAGS:
        fail(f"Tags {request.get('tags')!r} aren't allowed")
    data_center = [s["name"] for s in by_site().get("data-center", [])]
    names = request.get("names") or []
    if not names or any(name not in data_center for name in names):
        fail(f"Servers {names} aren't all data-center servers ({', '.join(data_center)})")
    extra = request.get("extra_vars") or {}
    allowed = {"rollout_action", "version", "package", "previous", "rollout_serial", "rollout_max_fail", "check_timeout",
               "java_drift_servers"}
    if set(extra) - allowed:
        fail(f"Unknown settings in the request: {', '.join(sorted(set(extra) - allowed))}")
    if request["playbook"] == "release.yml" and extra.get("rollout_action") not in ACTIONS:
        fail(f"Action {extra.get('rollout_action')!r} isn't one of {', '.join(sorted(ACTIONS))}")
    if "version" in extra and not VERSION.match(str(extra["version"])):
        fail(f"{extra['version']!r} isn't a version")
    for name, version in (extra.get("previous") or {}).items():
        if name not in data_center or not VERSION.match(str(version)):
            fail(f"previous: {name} {version} isn't valid")
    package = extra.get("package")
    if package:
        if not SHA256.match(str(package.get("sha256", ""))):
            fail("The package needs its SHA-256")
        if package.get("source") == "jcr":
            if not PACKAGE_URL.match(str(package.get("url", ""))):
                fail(f"{package.get('url')!r} isn't an inventory package on artifactory-demo.es.nl")
        elif package.get("source") == "file":
            # Only from the Mac, which copies the file next to the request.
            if not local_root or not Path(package.get("file", "")).resolve().is_relative_to(Path(local_root).resolve()):
                fail("A package file is only for runs from the Mac")
        else:
            fail(f"Package source {package.get('source')!r} isn't jcr or file")
    if any(name not in data_center for name in extra.get("java_drift_servers") or []):
        fail("java_drift_servers must be data-center servers")
    for key in ("rollout_serial", "rollout_max_fail", "check_timeout"):
        if key in extra and not isinstance(extra[key], int):
            fail(f"{key} must be a number")
    return request


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--request-env", help="environment variable holding the request as JSON")
    source.add_argument("--request-file", type=Path)
    parser.add_argument("--results", type=Path, required=True)
    args = parser.parse_args()

    if args.request_env:
        request = checked(json.loads(os.environ.get(args.request_env) or "{}"))
    else:
        request = checked(json.loads(args.request_file.read_text()), local_root=args.request_file.resolve().parent)
    args.results.parent.mkdir(parents=True, exist_ok=True)
    args.results.unlink(missing_ok=True)
    code, results = run_dc_here(request["playbook"], request["extra_vars"], request["names"], request.get("tags"),
                                results=args.results.resolve())
    for result in results:
        print(f"{result['server']}: {result['result']}, running {result['running']}")
    sys.exit(code)


if __name__ == "__main__":
    main()
