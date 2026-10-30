"""The four checks every pull request to main gets, as GitHub check runs from the pipeline's app.

    python3 scripts/demos/agent_ship/pull_requests.py next                  the pull request to check now, as JSON
    python3 scripts/demos/agent_ship/pull_requests.py start <n> <sha>       Build, Test, Quality gate, SBOM and scan: queued
    python3 scripts/demos/agent_ship/pull_requests.py check <name> <state>  in_progress, success, failure or cancelled
    python3 scripts/demos/agent_ship/pull_requests.py skip <n> <sha>        all four completed as skipped
    python3 scripts/demos/agent_ship/pull_requests.py sonar-args <service> <version> <n>   the scanner's arguments

The Jenkins job pull-requests runs every minute. While video 16's ruleset
"main: pull request and checks" is on, it takes the newest open pull
request from this repository (never a fork) whose head has no check runs
from the pipeline's app yet. A pull request that changes a service with
pull_requests.checks in services.yaml gets that service's release stages 1
to 4, from main's scripts and definitions; any other gets the four
completed as skipped, "No service with pull request checks changed", so the
ruleset never waits for checks that won't come. With the ruleset off, it
checks nothing, so videos 2 and 14 see their pull requests as before.
"""

import argparse
import json
import sys
from pathlib import Path

import agent  # noqa: F401  (puts scripts/ on the path)
from common import ROOT, get_parameter, github_api, github_app_token, service, services, ssm_client

STATE = Path(".release") / "pr-checks.json"
SKIPPED = "No service with pull request checks changed"
SONAR = "https://sonarcloud.io"


class GitHub:
    def __init__(self):
        ssm = ssm_client()
        self.token, self.repository, _ = github_app_token(ssm)
        self.app_id = int(get_parameter(ssm, "github/app-id"))
        self.ssm = ssm

    def call(self, method, path, body=None):
        return github_api(method, f"/repos/{self.repository}{path}", self.token, body, scheme="token")


def checks_wanted():
    return next(c["pull_requests"]["checks"] for c in services().values() if c.get("pull_requests"))


def ruleset_on(github, checks):
    """Is a required_status_checks rule asking for these four active on main?"""
    rules = github.call("GET", "/rules/branches/main") or []
    for rule in rules:
        if rule.get("type") == "required_status_checks":
            contexts = {c["context"] for c in rule.get("parameters", {}).get("required_status_checks", [])}
            if set(checks) <= contexts:
                return True
    return False


def changed_service(paths):
    """The first service with pull request checks whose folder the change touches, or None."""
    for name, config in services().items():
        if config.get("pull_requests") and any(p.startswith(config["path"].rstrip("/") + "/") for p in paths):
            return name
    return None


def files(github, number):
    paths, page = [], 1
    while True:
        batch = github.call("GET", f"/pulls/{number}/files?per_page=100&page={page}") or []
        paths += [f["filename"] for f in batch] + [f["previous_filename"] for f in batch if f.get("previous_filename")]
        if len(batch) < 100:
            return paths
        page += 1


def next_pull(github):
    """{"number", "sha", "service"} of the newest open pull request still without our checks, or None."""
    checks = checks_wanted()
    if not ruleset_on(github, checks):
        print("Video 16's ruleset is off: no pull request needs these checks.", file=sys.stderr)
        return None
    pulls = github.call("GET", "/pulls?state=open&sort=created&direction=desc&per_page=50") or []
    for pull in pulls:
        if (pull.get("head", {}).get("repo") or {}).get("full_name") != github.repository:
            continue  # a fork: never built
        sha = pull["head"]["sha"]
        runs = github.call("GET", f"/commits/{sha}/check-runs?app_id={github.app_id}&per_page=100") or {}
        if any(run["name"] in checks for run in runs.get("check_runs", [])):
            continue
        return {"number": pull["number"], "sha": sha, "service": changed_service(files(github, pull["number"]))}
    return None


def save(state):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2) + "\n")


def load():
    return json.loads(STATE.read_text())


def details_url():
    import os

    return os.environ.get("BUILD_URL") or None


def start(github, number, sha, conclusion=None, summary=None):
    runs = {}
    for name in checks_wanted():
        body = {"name": name, "head_sha": sha, "status": "completed" if conclusion else "queued",
                "external_id": f"pull-request-{number}"}
        if details_url():
            body["details_url"] = details_url()
        if conclusion:
            body.update(conclusion=conclusion, output={"title": summary, "summary": summary})
        runs[name] = github.call("POST", "/check-runs", body)["id"]
    save({"number": number, "sha": sha, "runs": runs})


def summary_of(name, service_name):
    """The one line each check reports, read from what its stage wrote."""
    import evidence

    reports = Path(".release") / "reports"
    if name == "Build":
        info = json.loads((Path("dist") / service_name / "build-info.json").read_text())
        return f"{service_name} {info['version']}, sha256 {info['sha256'][:12]}"
    if name == "Test":
        counts = evidence.junit_counts(reports / "junit.xml")
        return f"{counts['passed']} passed, {counts['failed'] + counts['errors']} failed"
    if name == "Quality gate":
        key = service(service_name)["pull_requests"].get("sonar_project_key") or service(service_name)["sonarqube"]["project_key"]
        result = (reports / "quality-gate.txt").read_text().strip() if (reports / "quality-gate.txt").exists() else ""
        return f"{result or 'Unknown'} on SonarQube Cloud: {SONAR}/dashboard?id={key}"
    components = evidence.sbom_components(reports / "sbom.cdx.json")
    counts = evidence.grype_counts(reports / "grype.json")
    return f"{components} components. Critical {counts['critical']}, high {counts['high']}"


def check(github, name, state, service_name=None):
    run = load()["runs"][name]
    body = {"status": "in_progress"} if state == "in_progress" else {"status": "completed", "conclusion": state}
    if state in ("success", "failure"):
        try:
            text = summary_of(name, service_name)
        except (OSError, ValueError, KeyError):
            text = "Failed before it could report." if state == "failure" else "Passed."
        body["output"] = {"title": text, "summary": text}
    elif state == "cancelled":
        body["output"] = {"title": "Not run: an earlier check failed", "summary": "Not run: an earlier check failed."}
    github.call("PATCH", f"/check-runs/{run}", body)


def sonar_args(service_name, version, number, organization):
    """The pull request's own analysis: on the Free plan, the main branch of a project for pull
    requests (pull_requests.sonar_project_key); without that key, SonarQube's pull request analysis."""
    config = service(service_name)
    path = config["path"]
    args = {"sonar.organization": organization, "sonar.projectVersion": version, "sonar.sources": path,
            "sonar.qualitygate.wait": "true", "sonar.qualitygate.timeout": "300"}
    own = config["pull_requests"].get("sonar_project_key")
    if own:
        args.update({"sonar.projectKey": own, "sonar.projectName": f"{service_name} pull requests"})
    else:
        args.update({"sonar.projectKey": config["sonarqube"]["project_key"], "sonar.pullrequest.key": str(number),
                     "sonar.pullrequest.branch": f"pull/{number}", "sonar.pullrequest.base": "main"})
    if (ROOT / path / "tests").is_dir():
        args["sonar.exclusions"] = f"{path}/tests/**"
        args["sonar.tests"] = f"{path}/tests"
    if config.get("runtime", "").startswith("python"):
        args["sonar.python.version"] = config["runtime"].removeprefix("python")
        args["sonar.python.coverage.reportPaths"] = ".release/reports/coverage.xml"
    return " ".join(f"-D{key}={value}" for key, value in args.items())


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("next")
    starter = sub.add_parser("start")
    starter.add_argument("number", type=int)
    starter.add_argument("sha")
    skipper = sub.add_parser("skip")
    skipper.add_argument("number", type=int)
    skipper.add_argument("sha")
    checker = sub.add_parser("check")
    checker.add_argument("name")
    checker.add_argument("state", choices=["in_progress", "success", "failure", "cancelled"])
    checker.add_argument("--service")
    sonar = sub.add_parser("sonar-args")
    sonar.add_argument("service")
    sonar.add_argument("version")
    sonar.add_argument("number", type=int)
    args = parser.parse_args()

    if args.command == "sonar-args":
        print(sonar_args(args.service, args.version, args.number, get_parameter(ssm_client(), "sonar/organization")))
        return 0
    github = GitHub()
    if args.command == "next":
        found = next_pull(github)
        print(json.dumps(found) if found else "")
    elif args.command == "start":
        start(github, args.number, args.sha)
    elif args.command == "skip":
        start(github, args.number, args.sha, conclusion="skipped", summary=SKIPPED)
    else:
        check(github, args.name, args.state, args.service)
    return 0


if __name__ == "__main__":
    sys.exit(main())
