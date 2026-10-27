"""The contract check, right after SBOM and scan: would the new version break
the apps that call production?

    python scripts/demos/apis/contract.py pricing-api
    python scripts/demos/apis/contract.py pricing-api --base old/openapi.yaml   compare with a file instead

Production's contract is the OpenAPI file in the release that production
runs, as JFrog Container Registry keeps it: <service>-releases/<version>/,
the version being the one the alias live points at. The new contract is the
file in this run's package in dist/. oasdiff breaking --fail-on ERR compares
the two. A breaking change, such as a removed response field or a new
required parameter, stops the run before anything is published, so no change
is opened, and the release's Slack message says what broke:
"Contract check failed: field currency removed".

oasdiff runs in the pinned image of jenkins/toolchain/oasdiff.Dockerfile,
with no network. OASDIFF=/path/to/oasdiff runs a local binary instead.
"""

import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import pricing
import run_state
from common import ROOT, fail, get_parameter, ssm_client

DOCKERFILE = ROOT / "jenkins" / "toolchain" / "oasdiff.Dockerfile"


def production_version():
    """The app version the alias live points at in production (a canary's extra version doesn't count)."""
    client = pricing.lambda_client()
    function = pricing.environment("production")["function"]
    live, _ = pricing.routing(client, function)
    return pricing.app_version(client, function, live)


def from_package(content, name):
    with zipfile.ZipFile(io.BytesIO(content)) as package:
        if name not in package.namelist():
            return None
        return package.read(name)


def production_contract(version, name):
    """The OpenAPI file in production's release, from JFrog Container Registry."""
    path = f"{pricing.SERVICE}-releases/{version}/{pricing.SERVICE}-{version}.zip"
    status, body = pricing.shared("publish").request("GET", path, get_parameter(ssm_client(), "jcr/token"))
    if status != 200:
        fail(f"FAIL production runs {version}, but JFrog Container Registry has no {path} ({status}): "
             f"nothing to compare with. reset.py apis puts the baseline there.")
    contract = from_package(body, name)
    if contract is None:
        fail(f"FAIL {path} has no {name}")
    return contract, path


def new_contract(state, name):
    """The OpenAPI file in this run's package, or in the service folder before a build."""
    package = pricing.workspace() / "dist" / pricing.SERVICE / f"{pricing.SERVICE}-{state.get('version')}.zip"
    if package.exists():
        contract = from_package(package.read_bytes(), name)
        if contract is not None:
            return contract, str(package.relative_to(pricing.workspace()))
    source = pricing.config()["dir"] / name
    return source.read_bytes(), str(source.relative_to(ROOT))


def oasdiff_command(work):
    """oasdiff as a local binary, or in its pinned image, reading the folder work."""
    if os.environ.get("OASDIFF"):
        return [os.environ["OASDIFF"]], str(work)
    tag = "release-demo/oasdiff:" + hashlib.sha256(DOCKERFILE.read_bytes()).hexdigest()[:12]
    if subprocess.run(["docker", "image", "inspect", tag], capture_output=True).returncode != 0:
        subprocess.run(["docker", "build", "-q", "-t", tag, "-f", str(DOCKERFILE), str(DOCKERFILE.parent)],
                       check=True, capture_output=True)
    return ["docker", "run", "--rm", "--network", "none", "-v", f"{work}:/work:ro", tag], "/work"


def breaking_changes(base, revision, fail_on):
    """oasdiff's findings as a list of dicts (id, text, level, operation, path). level 3 is ERR."""
    with tempfile.TemporaryDirectory() as temp:
        work = Path(temp)
        (work / "base.yaml").write_bytes(base)
        (work / "revision.yaml").write_bytes(revision)
        work.chmod(0o755)
        command, inside = oasdiff_command(work)
        result = subprocess.run([*command, "breaking", f"{inside}/base.yaml", f"{inside}/revision.yaml",
                                 "--fail-on", fail_on, "--format", "json", "--allow-external-refs=false"],
                                capture_output=True, text=True)
    if result.returncode not in (0, 1):
        fail(f"FAIL oasdiff couldn't compare the contracts: {result.stderr.strip()[:300]}")
    return json.loads(result.stdout or "[]")


def plain(change):
    """One breaking change in a few words, for Slack: "field currency removed"."""
    names = re.findall(r"`([^`]+)`", change.get("text", ""))
    first, last = (names[0], names[-1]) if names else ("", "")
    kind = change["id"]
    if kind.endswith("property-removed"):
        return f"field {first} removed"
    if kind == "new-required-request-parameter":
        return f"new required parameter {last}"
    if kind == "new-required-request-property":
        return f"new required field {first} in the request"
    if kind.endswith("removed-without-deprecation"):
        return f"endpoint {change.get('operation', '')} {change.get('path', '')} removed".replace("  ", " ")
    if kind.endswith("property-type-changed"):
        return f"field {first} changed type"
    return change.get("text", kind).replace("`", "")


def summary(errors):
    words = list(dict.fromkeys(plain(change) for change in errors))
    more = f" and {len(words) - 3} more" if len(words) > 3 else ""
    return "Contract check failed: " + "; ".join(words[:3]) + more


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("service")
    parser.add_argument("--base", type=Path, help="an OpenAPI file to compare with, instead of production's")
    args = parser.parse_args()

    settings = pricing.config()["contract"]
    state = run_state.load()
    if args.base:
        base, base_from, base_version = args.base.read_bytes(), str(args.base), "the given file"
    else:
        base_version = production_version()
        base, base_from = production_contract(base_version, settings["openapi"])
    revision, revision_from = new_contract(state, settings["openapi"])
    print(f"ok   production's contract: {base_from}")
    print(f"ok   this release's contract: {revision_from}")

    changes = breaking_changes(base, revision, settings["fail_on"])
    errors = [c for c in changes if c.get("level", 0) >= 3]
    for change in changes:
        label = "FAIL" if change in errors else "warn"
        print(f"{label} {change.get('operation', '')} {change.get('path', '')}: {change.get('text', '').replace('`', '')}")

    run_state.REPORTS.mkdir(parents=True, exist_ok=True)
    (run_state.REPORTS / "contract.json").write_text(json.dumps(changes, indent=2) + "\n")
    state = run_state.load()
    state["contract"] = {"base_version": base_version, "result": "failed" if errors else "passed",
                         "errors": len(errors), "warnings": len(changes) - len(errors)}
    if errors:
        state["contract"]["summary"] = summary(errors)
        run_state.save(state)
        pricing.tell(state["contract"]["summary"])
        raise SystemExit(1)
    run_state.save(state)
    print(f"ok   no breaking change against {base_version}'s contract"
          + (f" ({len(changes)} warnings)" if changes else ""))


if __name__ == "__main__":
    sys.exit(main())
