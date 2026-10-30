"""Flaky tests (demo 15): the quarantine decision before a release's tests, and Slack's lines after them.

    python scripts/flaky.py decide orders-service     at the start of Test, in release runs only
    python scripts/flaky.py lines orders-service      when Test ends, on the host

decide reads the last 14 days of per-test results (events.py tests) through
Grafana's data source query API, with the pipeline's service account. A test
of the version being released with at least after_flaky_runs flaky runs in
that window and no quarantine entry gets one, for 14 days (services.yaml,
tests.quarantine):

- a Jira issue as Release Pipeline: a Bug "Flaky test: <test> (<service>)",
  due on the until date, labelled flaky-test, with the counts, the dashboard
  and this run in its description;
- the entry, committed to main as the GitHub App with "[skip ci]", and
  written into this run's workspace, so this run's tests already use it;
- the line "Flaky: <test>, <n> of <N> runs in 14 days. Quarantined until
  <d Mon yyyy>. <issue>.", posted when the Test stage ends.

If Grafana, Jira or GitHub doesn't answer, decide says why and the release
goes on: it never stops one.

lines posts the Test stage's lines to the release's Slack message: decide's
quarantine lines, a quarantine that ended, a flaky test, a test that failed
twice. In the orders-service-tests job, which has no Slack message, it writes
the flaky line to .release/reports/unstable.txt for the job's stage instead.
"""

import argparse
import ast
import base64
import datetime
import json
import sys
import urllib.error
import urllib.request

import junit_results
import quarantine
import run_state
from common import ROOT, get_parameter, github_api, github_app_token, service, ssm_client

DASHBOARD_UID = "release-demo-flaky"
LOKI_UID = "grafanacloud-logs"
USER_AGENT = "release-demo/1.0 (+https://github.com/m-atek/release-demo)"


def version_tests(directory):
    """The node ids of a version's tests, read from its test files without running them."""
    found = []
    for path in sorted((directory / "tests").glob("test_*.py")):
        relative = path.relative_to(directory).as_posix()
        for node in ast.parse(path.read_text()).body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
                found.append(f"{relative}::{node.name}")
            elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
                found += [f"{relative}::{node.name}::{item.name}" for item in node.body
                          if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith("test")]
    return found


def counts_query(service_name, days):
    return (f'sum by (test, outcome) (count_over_time({{source="release-demo", service="{service_name}", kind="test"}} '
            f'| json event, test, outcome | event="test" [{days}d]))')


def grafana_counts(stack, token, service_name, days):
    """{test: {outcome: runs}} over the last days, through Grafana's data source query API."""
    body = {"queries": [{"refId": "A", "datasource": {"type": "loki", "uid": LOKI_UID},
                         "expr": counts_query(service_name, days), "queryType": "instant"}],
            "from": f"now-{days}d", "to": "now"}
    request = urllib.request.Request(f"{stack}/api/ds/query", data=json.dumps(body).encode(), method="POST", headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json", "User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        reply = json.loads(response.read())
    return parse_counts(reply)


def parse_counts(reply):
    counts = {}
    for frame in reply.get("results", {}).get("A", {}).get("frames", []):
        fields, values = frame["schema"]["fields"], frame["data"]["values"]
        for index, field in enumerate(fields):
            labels = field.get("labels") or {}
            if "test" in labels and "outcome" in labels and values[index]:
                runs = int(float(values[index][-1]))
                counts.setdefault(labels["test"], {})[labels["outcome"]] = runs
    return counts


def to_quarantine(tests, counts, entries, service_name, after):
    """The tests of this version that are flaky often enough and have no entry: [(test, flaky, runs)]."""
    have = {quarantine.base(entry["test"]) for entry in entries if entry["service"] == service_name}
    chosen = []
    for test in tests:
        seen = counts.get(test, {})
        flaky, runs = seen.get("flaky", 0), sum(seen.values())
        if flaky >= after and quarantine.base(test) not in have:
            chosen.append((test, flaky, runs))
    return chosen


def commit_entry(entry, relative, message):
    """Add the entry to the quarantine file on main, as the GitHub App. Returns the file as committed."""
    token, repository, _ = github_app_token(ssm_client())
    path = f"/repos/{repository}/contents/{relative}"
    try:
        current = github_api("GET", f"{path}?ref=main", token, scheme="token")
        text, sha = base64.b64decode(current["content"]).decode(), current["sha"]
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        text, sha = "", None
    entries = quarantine.load_text(text) + [entry]
    rendered = quarantine.render(entries)
    body = {"message": message, "content": base64.b64encode(rendered.encode()).decode(), "branch": "main"}
    if sha:
        body["sha"] = sha
    github_api("PUT", path, token, body, scheme="token")
    return rendered


def decide(service_name):
    config = service(service_name)
    settings = (config.get("tests") or {}).get("quarantine")
    if not settings:
        print(f"{service_name} has no quarantine in services.yaml: nothing to decide.")
        return
    state = run_state.load()
    days, window, after = int(settings.get("days", 14)), int(settings.get("window_days", 14)), int(settings["after_flaky_runs"])
    file = ROOT / settings["file"]
    tests = version_tests(config["dir"])
    ssm = ssm_client()
    stack = get_parameter(ssm, "grafana/stack-url").rstrip("/")
    try:
        counts = grafana_counts(stack, get_parameter(ssm, "grafana/service-account-token"), service_name, window)
    except Exception as error:
        print(f"Quarantine decision skipped: Grafana didn't answer ({error.__class__.__name__}: {error}). "
              "The release goes on.")
        return
    for test in tests:
        seen = counts.get(test)
        if seen:
            print(f"{test}: {seen.get('flaky', 0)} flaky in {sum(seen.values())} runs in {window} days")
    chosen = to_quarantine(tests, counts, quarantine.load(file), service_name, after)
    if not chosen:
        print(f"No test of {service_name} {state.get('version')} to quarantine: none has {after} flaky runs "
              f"in {window} days without an entry.")
        return

    from jira import Jira

    since = quarantine.today()
    until = since + datetime.timedelta(days=days)
    lines = state.setdefault("test_lines", [])
    for test, flaky, runs in chosen:
        name = junit_results.short(test)
        try:
            issue = Jira().create(
                "Bug", f"Flaky test: {name} ({service_name})",
                f"{test} failed and then passed on rerun in {flaky} of {runs} runs in the last {window} days, "
                f"on the same code. The release pipeline quarantined it until {until.isoformat()}: it still runs "
                f"and its result is recorded, but its failure doesn't stop a release. From the day after, the Test "
                f"stage fails until the test is fixed or the quarantine extended.\n"
                f"Dashboard: {stack}/d/{DASHBOARD_UID}\nRun: {state['build'].get('url', '')}",
                labels=["flaky-test"], due=until.isoformat())
            entry = {"service": service_name, "test": test, "issue": issue, "since": since, "until": until,
                     "reason": f"{flaky} of {runs} runs in {window} days failed, then passed on rerun."}
            rendered = commit_entry(entry, settings["file"],
                                    f"{issue} Quarantine {name} until {until.isoformat()} [skip ci]")
        except (Exception, SystemExit) as error:
            print(f"Quarantine of {name} skipped: {error}. The release goes on.")
            continue
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(rendered)
        line = f"Flaky: {name}, {flaky} of {runs} runs in {window} days. Quarantined until {quarantine.spoken(until)}. {issue}."
        lines.append(line)
        print(f"{issue}: quarantined {test} until {until.isoformat()}, committed to main")
    run_state.save(state)


def lines(service_name):
    """Slack's lines for the Test stage, or the tests job's unstable message."""
    state = run_state.load()
    results = junit_results.read(run_state.REPORTS)
    ended_path = run_state.REPORTS / "quarantine.json"
    ended = json.loads(ended_path.read_text()).get("ended", []) if ended_path.exists() else []
    said = [*state.get("test_lines", []), *ended, *junit_results.lines(results)]
    flaky = [line for line in junit_results.lines(results) if line.startswith("Flaky:")]
    if not state.get("slack", {}).get("ts"):
        if flaky:
            (run_state.REPORTS / "unstable.txt").write_text(flaky[0] + "\n")
        for line in said:
            print(line)
        return
    if not said:
        return
    import notify

    state.setdefault("lines", []).extend(said)
    run_state.save(state)
    notify.post(state)
    for line in said:
        print(f"Slack: {line}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["decide", "lines"])
    parser.add_argument("service")
    args = parser.parse_args()
    if args.command == "decide":
        try:
            decide(args.service)
        except (Exception, SystemExit) as error:  # never stops a release
            print(f"Quarantine decision skipped: {error}. The release goes on.")
    else:
        lines(args.service)
    return 0


if __name__ == "__main__":
    sys.exit(main())
