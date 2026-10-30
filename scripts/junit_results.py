"""Per-test results from a run's JUnit files (demo 15), for events.py tests and flaky.py.

    import junit_results
    results = junit_results.read()          # .release/reports/junit.xml and junit-attempts.xml

scripts/tests_run.py writes both files. junit.xml holds each test's final
result, so Jenkins' junit step doesn't turn a flaky pass into an unstable
release; its <testsuites> time is the first pass in seconds, start to end.
junit-attempts.xml exists only when a failed test was rerun: the suite "first
pass" holds those tests' first attempts, the suite "rerun" their second, with
the rerun's seconds as its time. A quarantined test carries the properties
quarantine (its Jira issue) and quarantine_until. Each test's outcome is one of
passed, failed, flaky, quarantined-passed, quarantined-failed or skipped.

Standard library only: it runs in the toolchain container and on the host.
"""

import xml.etree.ElementTree as ET
from pathlib import Path

FINAL, ATTEMPTS = "junit.xml", "junit-attempts.xml"


def node_id(case):
    """pytest's node id from an xunit1 testcase: tests/test_handler.py::test_x."""
    file, name, classname = case.get("file", ""), case.get("name", ""), case.get("classname", "")
    if not file:
        return f"{classname.replace('.', '/')}.py::{name}"
    module = file.removesuffix(".py").replace("/", ".")
    owner = classname[len(module) + 1:] if classname.startswith(module + ".") else ""
    return f"{file}::{owner}::{name}" if owner else f"{file}::{name}"


def short(test):
    """The function's name, as Slack shows a test: test_delivery_estimate_timeout."""
    return test.rsplit("::", 1)[-1]


def status(case):
    """passed, failed, xfailed (a quarantined test that failed) or skipped."""
    if case.find("failure") is not None or case.find("error") is not None:
        return "failed"
    skipped = case.find("skipped")
    if skipped is not None:
        return "xfailed" if skipped.get("type") == "pytest.xfail" else "skipped"
    return "passed"


def properties(case):
    return {p.get("name"): p.get("value") for p in case.iter("property")}


def cases(root):
    return [case for case in root.iter("testcase")]


def seconds(element):
    try:
        return round(float(element.get("time") or 0), 3)
    except ValueError:
        return 0.0


def read(reports):
    """{"tests": [{test, outcome, attempts, seconds, quarantine, quarantine_until}],
    "first_pass_seconds", "rerun_seconds", "reran"}. Empty tests when junit.xml is missing."""
    final_path, attempts_path = Path(reports) / FINAL, Path(reports) / ATTEMPTS
    if not final_path.exists():
        return {"tests": [], "first_pass_seconds": 0.0, "rerun_seconds": 0.0, "reran": False}
    final = ET.parse(final_path).getroot()
    first_pass = seconds(final) or sum(seconds(suite) for suite in final.iter("testsuite"))
    first_tries, reruns, rerun_seconds = {}, {}, 0.0
    if attempts_path.exists():
        for suite in ET.parse(attempts_path).getroot().iter("testsuite"):
            target = reruns if suite.get("name") == "rerun" else first_tries
            if suite.get("name") == "rerun":
                rerun_seconds = seconds(suite)
            for case in cases(suite):
                target[node_id(case)] = case
    tests = []
    for case in cases(final):
        test, marks = node_id(case), properties(case)
        quarantined = bool(marks.get("quarantine"))
        result = status(case)
        attempts = 2 if test in reruns else 1
        took = seconds(case) + (seconds(first_tries[test]) if test in first_tries else 0.0)
        if quarantined:
            outcome = "quarantined-failed" if result in ("xfailed", "failed") else "quarantined-passed"
        elif attempts == 2:
            outcome = "flaky" if result == "passed" else "failed"
        else:
            outcome = {"xfailed": "failed"}.get(result, result)
        tests.append({"test": test, "outcome": outcome, "attempts": attempts, "seconds": round(took, 3),
                      "quarantine": marks.get("quarantine"), "quarantine_until": marks.get("quarantine_until")})
    return {"tests": tests, "first_pass_seconds": round(first_pass, 3), "rerun_seconds": round(rerun_seconds, 3),
            "reran": bool(reruns)}


def summary(results, build_seconds=0.0):
    """One run in numbers. A run in which one in-place rerun turned every failed test
    into a pass avoided a rerun: without it, it would have been a red build to start
    again. What that rerun would have repeated at the least is the run's own Build
    seconds plus its first Test pass; the in-place rerun's seconds are subtracted."""
    tests = results["tests"]
    flaky = sum(1 for t in tests if t["outcome"] == "flaky")
    failed = sum(1 for t in tests if t["outcome"] == "failed")
    failed_twice = sum(1 for t in tests if t["outcome"] == "failed" and t["attempts"] == 2)
    avoided = int(flaky > 0 and failed == 0)
    saved = (build_seconds + results["first_pass_seconds"] - results["rerun_seconds"]) if avoided else 0.0
    return {"tests": len(tests), "flaky": flaky, "failed": failed, "failed_twice": failed_twice,
            "build_seconds": round(build_seconds, 1), "first_pass_seconds": results["first_pass_seconds"],
            "rerun_seconds": results["rerun_seconds"], "avoided": avoided, "saved_seconds": round(max(saved, 0.0), 1)}


def lines(results):
    """What a release's Slack message says about this run's tests, if anything."""
    tests = results["tests"]
    flaky = [short(t["test"]) for t in tests if t["outcome"] == "flaky"]
    twice = [short(t["test"]) for t in tests if t["outcome"] == "failed" and t["attempts"] == 2]
    said = []
    if flaky:
        said.append(f"Flaky: {', '.join(flaky)} failed, then passed on rerun.")
    if twice:
        said.append(f"Failed twice: {', '.join(twice)}.")
    return said
