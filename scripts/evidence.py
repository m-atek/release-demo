"""The run's fact file for the AI notes (videos 12 and 13), and the evidence
readers the auditor sample (video 11) shares. Deterministic: no AI.

    python3 scripts/evidence.py --out .release/evidence.json             in the Change stage, before the change opens
    python3 scripts/evidence.py --out .release/evidence.json --change    then the change's number and type
    python3 scripts/evidence.py --states orders-service 1.4.0 1.4.1      settings, diff and findings between two demo states

evidence.json holds:

- run: service, version, commit, Jira key and summary, build URL, change number and type;
- results: the tests from junit.xml, the quality gate's stage result and its
  SonarQube project, the scan's counts by severity from grype.json, the SBOM's
  component count, the package's name and SHA-256 from build-info.json, and
  the lines of check-staging.txt;
- versions: what production runs, and the commit it was built from, read from
  that version's own package (build_info.json) through the evidence reader role;
- diff: the service's folder between that commit and this one: the files, the
  lines added and removed, and the unified text;
- settings, names only: what each environment defines (services.yaml, checked
  against the live function configuration's keys, whose values are dropped as
  they are read), what the code reads (os.environ[...] is required,
  os.environ.get and os.getenv are optional), and the names the Lambda
  runtime provides that the code reads;
- findings, the script's own conclusions: F1, F2, ... for each required name
  an environment doesn't define, with the file and line that read it, where it
  is defined, and whether the read is new in this diff.

A service without Python source gets empty settings and findings. Nothing in
the file is a setting's value. A part it can't read says why instead, and the
script still writes the rest: it never fails a release.
"""

import argparse
import ast
import base64
import difflib
import hashlib
import io
import json
import re
import subprocess
import sys
import time
import urllib.request
import xml.etree.ElementTree as ElementTree
import zipfile
from pathlib import Path

import run_state
from common import REGION, ROOT, aws_session, service, write_json

EVIDENCE_ROLE = "release-demo-evidence-reader"
SEVERITIES = ["critical", "high", "medium", "low", "negligible", "unknown"]
SKIPPED_PARTS = {"tests", "__pycache__", ".pytest_cache"}
DIFF_LIMIT = 40_000
# What the Lambda runtime sets in every function's environment, besides AWS_*.
RUNTIME_NAMES = {"_HANDLER", "_X_AMZN_TRACE_ID", "LAMBDA_TASK_ROOT", "LAMBDA_RUNTIME_DIR", "TZ", "LANG", "PATH",
                 "LD_LIBRARY_PATH", "PYTHONPATH"}


# Readers, shared with the auditor sample (video 11).

def junit_counts(path):
    """total, passed, failed, errors and skipped from a JUnit report."""
    root = ElementTree.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    count = {name: sum(int(suite.get(name, 0)) for suite in suites) for name in ("tests", "failures", "errors", "skipped")}
    return {"total": count["tests"], "passed": count["tests"] - count["failures"] - count["errors"] - count["skipped"],
            "failed": count["failures"], "errors": count["errors"], "skipped": count["skipped"]}


def grype_counts(path):
    """Grype's findings by severity, and their total."""
    matches = json.loads(Path(path).read_text()).get("matches") or []
    counts = dict.fromkeys(SEVERITIES, 0)
    for match in matches:
        severity = str(match.get("vulnerability", {}).get("severity") or "unknown").lower()
        counts[severity if severity in counts else "unknown"] += 1
    return {**counts, "total": len(matches)}


def sbom_components(path):
    """How many components a CycloneDX SBOM lists."""
    return len(json.loads(Path(path).read_text()).get("components") or [])


def base64_to_hex(value):
    """Lambda's CodeSha256 (base64) as the hex digest build-info.json records."""
    return base64.b64decode(value).hex()


def ranked(seed, keys):
    """keys in the order of SHA-256 of "<seed>:<key>": the same seed, the same order."""
    return sorted(keys, key=lambda key: hashlib.sha256(f"{seed}:{key}".encode()).hexdigest())


# What the code reads, and what each environment defines.

def env_read(node):
    """(name, required) if the node reads an environment variable by a literal name."""
    def is_environ(value):
        return (isinstance(value, ast.Attribute) and value.attr == "environ" and isinstance(value.value, ast.Name)
                and value.value.id == "os") or (isinstance(value, ast.Name) and value.id == "environ")

    def literal(value):
        return value.value if isinstance(value, ast.Constant) and isinstance(value.value, str) else None

    if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load) and is_environ(node.value):
        return literal(node.slice), True
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.args:
        func = node.func
        if func.attr in ("get", "setdefault") and is_environ(func.value):
            return literal(node.args[0]), False
        if func.attr == "getenv" and isinstance(func.value, ast.Name) and func.value.id == "os":
            return literal(node.args[0]), False
    if isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], (ast.In, ast.NotIn)) \
            and is_environ(node.comparators[0]):
        return literal(node.left), False
    return None, False


def reads_in(files):
    """Every literal environment read in {relative path: Python source}, by file and line."""
    reads = []
    for relative, text in sorted(files.items()):
        try:
            tree = ast.parse(text, filename=relative)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            name, required = env_read(node)
            if name:
                reads.append({"name": name, "file": relative, "line": node.lineno, "required": required})
    return sorted(reads, key=lambda read: (read["file"], read["line"], read["name"]))


def python_files(directory):
    """{relative path: source} of a service folder's Python files, tests left out as the package leaves them."""
    directory = Path(directory)
    return {path.relative_to(directory).as_posix(): path.read_text()
            for path in sorted(directory.rglob("*.py"))
            if not SKIPPED_PARTS & set(path.relative_to(directory).parts)}


def provided_by_runtime(name):
    return name.startswith("AWS_") or name in RUNTIME_NAMES


def defined_settings(config, live=None):
    """{environment: sorted names}, and where each came from. services.yaml gives the
    settings, plus ENVIRONMENT for a Lambda service (infra/orders_service.tf adds it);
    the live configuration's keys, where readable, are what a new version gets."""
    defined, sources, differences = {}, {}, []
    for name, environment in (config.get("environments") or {}).items():
        expected = set((environment.get("settings") or {}).keys())
        if config.get("platform") == "aws-lambda":
            expected.add("ENVIRONMENT")
        keys = (live or {}).get(name)
        if keys is None:
            defined[name], sources[name] = sorted(expected), "services.yaml"
            continue
        defined[name], sources[name] = sorted(keys), "live function configuration"
        for missing in sorted(expected - set(keys)):
            differences.append(f"{name}: services.yaml has {missing}, the live configuration doesn't")
        for extra in sorted(set(keys) - expected):
            differences.append(f"{name}: the live configuration has {extra}, services.yaml doesn't")
    return defined, sources, differences


def findings_for(reads, defined, old_reads=None):
    """F1, F2, ...: each required name an environment doesn't define, at its first read."""
    first = {}
    for read in reads:
        if read["required"] and not provided_by_runtime(read["name"]):
            first.setdefault(read["name"], read)
    old_required = {read["name"] for read in old_reads or [] if read["required"]}
    found = []
    for name in sorted(first):
        missing = [env for env, names in defined.items() if name not in names]
        if not missing:
            continue
        read = first[name]
        found.append({"id": f"F{len(found) + 1}", "setting": name, "file": read["file"], "line": read["line"],
                      "defined_in": [env for env, names in defined.items() if name in names],
                      "missing_in": missing, "new": None if old_reads is None else name not in old_required})
    return found


def settings_section(config, files, old_files=None, live=None):
    if not files:
        return {"defined": {}, "read": [], "runtime": [], "note": "no Python source"}, []
    defined, sources, differences = defined_settings(config, live)
    reads = reads_in(files)
    section = {"defined": defined, "defined_from": sources, "read": reads,
               "runtime": sorted({read["name"] for read in reads if provided_by_runtime(read["name"])})}
    if differences:
        section["differences"] = differences
    return section, findings_for(reads, defined, reads_in(old_files) if old_files is not None else None)


# The diff.

def diff_texts(old_files, new_files, limit=DIFF_LIMIT):
    """The unified diff between two {relative path: text} maps, as git prints it."""
    files, chunks, added, removed = [], [], 0, 0
    for relative in sorted(set(old_files) | set(new_files)):
        old, new = old_files.get(relative, ""), new_files.get(relative, "")
        if old == new:
            continue
        lines = list(difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                          f"a/{relative}" if relative in old_files else "/dev/null",
                                          f"b/{relative}" if relative in new_files else "/dev/null"))
        plus = sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))
        minus = sum(1 for line in lines if line.startswith("-") and not line.startswith("---"))
        files.append({"file": relative, "added": plus, "removed": minus})
        added, removed = added + plus, removed + minus
        chunks.append("".join(line if line.endswith("\n") else line + "\n" for line in lines))
    text = "".join(chunks)
    section = {"files": files, "added": added, "removed": removed, "text": text[:limit]}
    if len(text) > limit:
        section["cut_at_characters"] = limit
    return section


def git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)


def has_commit(commit):
    if not commit or not re.fullmatch(r"[0-9a-f]{7,40}", commit):
        return False
    if git("cat-file", "-e", f"{commit}^{{commit}}").returncode == 0:
        return True
    git("fetch", "--quiet", "origin", commit)
    return git("cat-file", "-e", f"{commit}^{{commit}}").returncode == 0


def text_files_at(commit, path):
    """{path relative to the service folder: text} of every file in it at a commit."""
    listing = git("ls-tree", "-r", "--name-only", commit, "--", path).stdout.split()
    files = {}
    for name in listing:
        shown = git("show", f"{commit}:{name}")
        if shown.returncode == 0:
            files[name.removeprefix(path.rstrip("/") + "/")] = shown.stdout
    return files


def python_only(files):
    return {name: text for name, text in files.items()
            if name.endswith(".py") and not SKIPPED_PARTS & set(Path(name).parts)}


# Production's version and the live configuration, through the evidence reader role.

def reader_session():
    import boto3

    session = aws_session()
    account = session.client("sts").get_caller_identity()["Account"]
    credentials = session.client("sts").assume_role(
        RoleArn=f"arn:aws:iam::{account}:role/{EVIDENCE_ROLE}",
        RoleSessionName=f"evidence-{int(time.time())}")["Credentials"]
    return boto3.Session(aws_access_key_id=credentials["AccessKeyId"], aws_secret_access_key=credentials["SecretAccessKey"],
                         aws_session_token=credentials["SessionToken"], region_name=REGION)


def live_version(client, function):
    """What the live alias runs: Lambda's version number, the app version and the commit, from its package."""
    number = client.get_alias(FunctionName=function, Name="live")["FunctionVersion"]
    location = client.get_function(FunctionName=function, Qualifier=number)["Code"]["Location"]
    with urllib.request.urlopen(location, timeout=30) as response:
        package = zipfile.ZipFile(io.BytesIO(response.read()))
    names = set(package.namelist())
    info = json.loads(package.read("build_info.json")) if "build_info.json" in names else {}
    version = package.read("VERSION").decode().strip() if "VERSION" in names else info.get("version")
    return {"function": function, "lambda_version": number, "version": version, "commit": info.get("commit")}


def live_keys(client, function):
    """The names in the function's configuration ($LATEST, what a new version gets). Values are dropped here."""
    variables = (client.get_function_configuration(FunctionName=function).get("Environment") or {}).get("Variables") or {}
    return sorted(variables)


# Run and results, from the run state and the reports.

def jira_summary(key):
    try:
        from jira import Jira

        return Jira().call("GET", f"/rest/api/3/issue/{key}?fields=summary")["fields"]["summary"]
    except BaseException as error:  # fail() exits; a summary is nice to have, never needed
        return {"unavailable": error.__class__.__name__}


def run_section(state, summary=None):
    change = state.get("change") or {}
    return {"service": state["service"], "version": state["version"], "commit": state.get("commit"),
            "jira_key": state.get("jira_key"), "jira_summary": summary, "build": state.get("build", {}).get("number"),
            "build_url": state.get("build", {}).get("url"), "change_number": change.get("number"),
            "change_sys_id": change.get("sys_id"), "change_type": change.get("type")}


def lines_of(path):
    path = Path(path)
    return [line.rstrip() for line in path.read_text().splitlines() if line.strip()] if path.exists() else None


def results_section(state, config, reports, dist):
    def read(name, reader):
        path = reports / name
        try:
            return {**reader(path), "source": name} if path.exists() else {"unavailable": f"no {name}"}
        except (ValueError, ElementTree.ParseError, OSError) as error:
            return {"unavailable": f"{name} unreadable ({error.__class__.__name__})"}

    gate = state.get("stages", {}).get("Quality gate", {})
    info_path = reports / "build-info.json" if (reports / "build-info.json").exists() else dist / "build-info.json"
    info = json.loads(info_path.read_text()) if info_path.exists() else {}
    package = state.get("package") or {}
    return {
        "tests": read("junit.xml", junit_counts),
        "quality_gate": {"result": gate.get("status"), "project": config.get("sonarqube", {}).get("project_key"),
                         "source": "SonarQube"},
        "scan": read("grype.json", grype_counts),
        "sbom": read("sbom.cdx.json", lambda path: {"components": sbom_components(path)}),
        "package": {"name": info.get("package") or package.get("name"), "sha256": info.get("sha256") or package.get("sha256"),
                    "source": "build-info.json"},
        "check_staging": {"lines": lines_of(reports.parent / "check-staging.txt"), "source": "check-staging.txt"},
    }


def collect(state, reports=None, dist=None, reader=None, summary=None):
    """evidence.json for the run in the state. reader is a boto3 session as the evidence reader."""
    config = service(state["service"])
    reports = reports or run_state.REPORTS
    dist = dist or (ROOT / "dist" / state["service"])
    evidence = {"run": run_section(state, summary), "results": results_section(state, config, reports, dist)}

    production, live = None, {}
    if config.get("platform") == "aws-lambda":
        try:
            client = (reader or reader_session()).client("lambda")
            production = live_version(client, config["environments"]["production"]["function"])
            live = {name: live_keys(client, env["function"]) for name, env in config["environments"].items()}
        except Exception as error:  # no role yet, no access: the evidence says so
            production = {"unavailable": f"the evidence reader role couldn't read production ({error.__class__.__name__})"}
    evidence["versions"] = {"this": state["version"], "production": production,
                            "staging": (state.get("deploys") or {}).get("staging", {}).get("number")}

    new_files = python_files(config["dir"])
    old_files = None
    if production and has_commit(production.get("commit")):
        all_old = text_files_at(production["commit"], config["path"])
        old_files = python_only(all_old)
        all_new = text_files_at(state["commit"], config["path"]) if has_commit(state.get("commit")) else None
        evidence["diff"] = ({"from_version": production.get("version"), "from_commit": production["commit"],
                             "to_commit": state["commit"], "path": config["path"], **diff_texts(all_old, all_new)}
                            if all_new is not None else {"unavailable": "this run's commit isn't in the checkout"})
    else:
        why = "production's version is unknown" if not production or "unavailable" in production else \
            f"production's commit {production.get('commit')!r} isn't in the repository"
        evidence["diff"] = {"unavailable": why}
    evidence["settings"], evidence["findings"] = settings_section(config, new_files, old_files, live or None)
    return evidence


def between_states(service_name, old_version, new_version):
    """Settings, diff and findings between two demo states, with services.yaml's settings."""
    config = service(service_name)
    root = ROOT / "demo-states" / service_name

    def all_files(version):
        directory = root / version
        return {path.relative_to(directory).as_posix(): path.read_text() for path in sorted(directory.rglob("*"))
                if path.is_file() and "__pycache__" not in path.parts}

    old, new = all_files(old_version), all_files(new_version)
    settings, found = settings_section(config, python_only(new), python_only(old))
    return {"diff": {"from_version": old_version, "to_version": new_version, **diff_texts(old, new)},
            "settings": settings, "findings": found}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=run_state.PATH.parent / "evidence.json")
    parser.add_argument("--change", action="store_true", help="add the change's number and type to --out")
    parser.add_argument("--states", nargs=3, metavar=("SERVICE", "FROM", "TO"), help="compare two demo states")
    args = parser.parse_args()

    if args.states:
        print(json.dumps(between_states(*args.states), indent=2))
        return 0
    state = run_state.load()
    if args.change:
        evidence = json.loads(args.out.read_text())
        evidence["run"].update({key: value for key, value in run_section(state).items() if key.startswith("change_")})
        write_json(args.out, evidence)
        print(f"evidence.json: change {evidence['run']['change_number']} ({evidence['run']['change_type']})")
        return 0
    evidence = collect(state, summary=jira_summary(state.get("jira_key")))
    write_json(args.out, evidence)
    found = ", ".join(f"{f['id']} {f['setting']} (not in {' or '.join(f['missing_in'])})" for f in evidence["findings"])
    print(f"evidence.json: {len(evidence['settings'].get('read', []))} setting reads, findings: {found or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
